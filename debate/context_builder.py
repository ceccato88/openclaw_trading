from __future__ import annotations

from typing import Any, Dict, List, Sequence

from hl_client import logger
from skills.portfolio import get_portfolio_status
from skills.risk_manager import check_daily_drawdown
from skills.scanner import run_opportunity_scanner
from skills.signals import (
    evaluate_higher_timeframe_context_from_candles,
    evaluate_pullback_entry_from_candles,
    fetch_candles,
    get_market_regime,
    score_opportunity,
)
from skills.support import get_asset_context, get_meta_context


FALLBACK_SYMBOLS = ["BTC", "ETH", "SOL"]


def _build_asset_lookup() -> Dict[str, Dict[str, Any]]:
    universe, asset_contexts = get_meta_context()
    lookup: Dict[str, Dict[str, Any]] = {}
    for idx, asset in enumerate(universe):
        lookup[asset["name"]] = {
            "asset": asset,
            "context": asset_contexts[idx],
        }
    return lookup


def _pick_default_symbols(scanner_results: Sequence[Dict[str, Any]], lookup: Dict[str, Dict[str, Any]], limit: int = 3) -> List[str]:
    symbols: List[str] = []
    for candidate in scanner_results:
        coin = candidate.get("coin")
        if coin and coin not in symbols:
            symbols.append(coin)
        if len(symbols) >= limit:
            return symbols

    for coin in FALLBACK_SYMBOLS:
        if coin in lookup and coin not in symbols:
            symbols.append(coin)
        if len(symbols) >= limit:
            break
    return symbols


def _build_symbol_context(symbol: str, regime: Dict[str, Any], lookup: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    asset_row = lookup.get(symbol)
    if asset_row is None:
        raise RuntimeError(f"Símbolo {symbol} não encontrado no universo da Hyperliquid.")

    _current_price, _asset_info, asset_context = get_asset_context(symbol)
    candles_15m = fetch_candles(symbol, interval="15m", lookback=60)
    candles_1h = fetch_candles(symbol, interval="1h", lookback=120)
    scored = score_opportunity(
        coin=symbol,
        candles=candles_15m,
        funding_rate=float(asset_context.get("funding") or 0.0),
        volume_24h=float(asset_context["dayNtlVlm"]),
        prev_day_px=float(asset_context.get("prevDayPx") or candles_15m[-1]["c"]),
        regime=regime["regime"],
    )

    suggested_side = scored.get("suggested_side", "LONG")
    entry_setup = evaluate_pullback_entry_from_candles(suggested_side, candles_15m[-40:])
    higher_timeframe_context = evaluate_higher_timeframe_context_from_candles(suggested_side, candles_1h)
    entry_ready = bool(entry_setup.get("triggered")) and bool(higher_timeframe_context.get("context_ok"))
    if entry_setup.get("triggered") and not higher_timeframe_context.get("context_ok"):
        entry_setup = dict(entry_setup)
        entry_setup["triggered"] = False
        entry_setup["reason"] = higher_timeframe_context.get("reason", "higher_timeframe_context_filter")

    return {
        "symbol": symbol,
        "market_price": float(asset_context.get("markPx") or asset_context.get("midPx") or asset_context.get("oraclePx") or 0.0),
        "volume_24h_usd": float(asset_context["dayNtlVlm"]),
        "funding_rate": float(asset_context.get("funding") or 0.0),
        "score": scored,
        "entry_setup": entry_setup,
        "higher_timeframe_context": higher_timeframe_context,
        "entry_ready": entry_ready,
    }


def build_market_context(symbols: Sequence[str] | None = None, max_candidates: int = 5) -> Dict[str, Any]:
    logger.info("A construir contexto de mercado para debate...")
    account_snapshot = get_portfolio_status()
    risk_status = check_daily_drawdown()
    market_regime = get_market_regime()
    scanner_results = run_opportunity_scanner(max_results=max_candidates, candidate_pool_size=max(20, max_candidates * 4))
    lookup = _build_asset_lookup()

    selected_symbols = [symbol.upper() for symbol in symbols or [] if symbol]
    if not selected_symbols:
        scanner_candidates = [item for item in scanner_results if item.get("coin")]
        selected_symbols = _pick_default_symbols(scanner_candidates, lookup, limit=max(3, max_candidates))

    symbol_contexts = []
    errors = []
    for symbol in selected_symbols:
        try:
            symbol_contexts.append(_build_symbol_context(symbol, market_regime, lookup))
        except Exception as exc:  # noqa: BLE001
            errors.append({"symbol": symbol, "error": str(exc)})

    return {
        "account": account_snapshot,
        "risk": risk_status,
        "market_regime": market_regime,
        "scanner": scanner_results,
        "symbols": symbol_contexts,
        "errors": errors,
    }
