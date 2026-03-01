from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from hl_client import get_info, logger
from skills.signals import (
    INTERVAL_TO_MS,
    calculate_atr,
    calculate_dynamic_stop_price,
    candle_closes,
)
from skills.signals import (
    determine_market_regime_from_prices,
    evaluate_higher_timeframe_context_from_candles,
    evaluate_pullback_entry_from_candles,
    score_opportunity,
)
from skills.support import calculate_risk_based_notional


WARMUP_HOURS = 450
DEFAULT_FEE_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 2.0
DEFAULT_PENDING_TTL_CANDLES = 4


@dataclass
class BacktestTrade:
    coin: str
    side: str
    regime: str
    opened_at: str
    closed_at: str
    entry_price: float
    exit_price: float
    size: float
    notional_usd: float
    pnl_usd: float
    pnl_pct: float
    fees_usd: float
    score: float
    entry_reason: str
    exit_reason: str
    bars_held: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _ms_to_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _parse_datetime_to_ms(value: str) -> int:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _dedupe_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[int, Dict[str, Any]] = {}
    for candle in candles:
        deduped[int(candle["t"])] = candle
    return [deduped[key] for key in sorted(deduped)]


def _fetch_candle_range(coin: str, interval: str, start_ms: int, end_ms: int, batch_size: int = 500) -> List[Dict[str, Any]]:
    info = get_info()
    step_ms = INTERVAL_TO_MS[interval] * batch_size
    cursor = start_ms
    candles: List[Dict[str, Any]] = []
    while cursor < end_ms:
        batch_end = min(cursor + step_ms, end_ms)
        batch = info.candles_snapshot(coin, interval, cursor, batch_end)
        candles.extend(batch)
        if batch_end >= end_ms:
            break
        cursor = batch_end + INTERVAL_TO_MS[interval]
    return _dedupe_candles(sorted(candles, key=lambda candle: candle["t"]))


def _fetch_funding_range(coin: str, start_ms: int, end_ms: int, batch_days: int = 7) -> List[Dict[str, Any]]:
    info = get_info()
    batch_ms = batch_days * 24 * 60 * 60 * 1000
    cursor = start_ms
    rows: List[Dict[str, Any]] = []
    while cursor < end_ms:
        batch_end = min(cursor + batch_ms, end_ms)
        rows.extend(info.funding_history(coin, cursor, batch_end))
        if batch_end >= end_ms:
            break
        cursor = batch_end + 1
    deduped: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        deduped[int(row["time"])] = row
    return [deduped[key] for key in sorted(deduped)]


def _latest_funding_rate(funding_rows: List[Dict[str, Any]], funding_times: List[int], current_time_ms: int) -> float:
    idx = bisect_right(funding_times, current_time_ms) - 1
    if idx < 0:
        return 0.0
    return float(funding_rows[idx].get("fundingRate") or 0.0)


def _rolling_prev_day_px(candles_15m: List[Dict[str, Any]], idx: int) -> float:
    day_lookback = 96
    if idx >= day_lookback:
        return float(candles_15m[idx - day_lookback]["c"])
    return float(candles_15m[max(idx - 1, 0)]["c"])


def _rolling_volume_usd(candles_15m: List[Dict[str, Any]], idx: int) -> float:
    window = candles_15m[max(0, idx - 95) : idx + 1]
    total = 0.0
    for candle in window:
        total += float(candle["v"]) * float(candle["c"])
    return total


def _limit_fill_price(side: str, limit_price: float, candle: Dict[str, Any]) -> Optional[float]:
    high = float(candle["h"])
    low = float(candle["l"])
    candle_open = float(candle["o"])
    if side == "LONG":
        if candle_open <= limit_price:
            return candle_open
        if low <= limit_price <= high:
            return limit_price
        return None

    if candle_open >= limit_price:
        return candle_open
    if low <= limit_price <= high:
        return limit_price
    return None


def _exit_price_from_candle(side: str, stop_price: float, take_profit: float, candle: Dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
    high = float(candle["h"])
    low = float(candle["l"])

    if side == "LONG":
        stop_hit = low <= stop_price
        tp_hit = high >= take_profit
        if stop_hit and tp_hit:
            return stop_price, "stop_loss"
        if stop_hit:
            return stop_price, "stop_loss"
        if tp_hit:
            return take_profit, "take_profit"
        return None, None

    stop_hit = high >= stop_price
    tp_hit = low <= take_profit
    if stop_hit and tp_hit:
        return stop_price, "stop_loss"
    if stop_hit:
        return stop_price, "stop_loss"
    if tp_hit:
        return take_profit, "take_profit"
    return None, None


def _fee_amount(notional_usd: float, fee_bps: float) -> float:
    return notional_usd * (fee_bps / 10_000)


def _trade_pnl(side: str, size: float, entry: float, exit_px: float) -> float:
    if side == "LONG":
        return (exit_px - entry) * size
    return (entry - exit_px) * size


def _max_drawdown(curve: List[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, ((peak - equity) / peak) * 100)
    return max_dd


def run_backtest(
    coin: str,
    start: str,
    end: str,
    starting_equity: float = 1000.0,
    risk_pct: float = 2.0,
    reward_pct: float = 4.0,
    account_risk_pct: float = 1.0,
    fee_bps: float = DEFAULT_FEE_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    pending_ttl_candles: int = DEFAULT_PENDING_TTL_CANDLES,
) -> Dict[str, Any]:
    coin = coin.upper()
    start_ms = _parse_datetime_to_ms(start)
    end_ms = _parse_datetime_to_ms(end)
    if end_ms <= start_ms:
        raise ValueError("O parâmetro 'end' deve ser maior do que 'start'.")

    warmup_start_ms = start_ms - (WARMUP_HOURS * 60 * 60 * 1000)
    logger.info("Backtest %s | %s -> %s", coin, start, end)

    candles_15m = _fetch_candle_range(coin, "15m", warmup_start_ms, end_ms)
    candles_1h = _fetch_candle_range(coin, "1h", warmup_start_ms, end_ms)
    btc_1h = candles_1h if coin == "BTC" else _fetch_candle_range("BTC", "1h", warmup_start_ms, end_ms)
    funding_rows = _fetch_funding_range(coin, warmup_start_ms, end_ms)
    funding_times = [int(row["time"]) for row in funding_rows]

    candles_15m = [candle for candle in candles_15m if int(candle["t"]) <= end_ms]
    candles_1h = [candle for candle in candles_1h if int(candle["t"]) <= end_ms]
    btc_1h = [candle for candle in btc_1h if int(candle["t"]) <= end_ms]
    if len(candles_15m) < 250 or len(candles_1h) < 220 or len(btc_1h) < 220:
        raise RuntimeError("Histórico insuficiente para rodar o backtest com warmup adequado.")

    one_h_times = [int(candle["t"]) for candle in candles_1h]
    btc_one_h_times = [int(candle["t"]) for candle in btc_1h]

    equity = starting_equity
    equity_curve: List[float] = [equity]
    trades: List[BacktestTrade] = []
    pending_order: Optional[Dict[str, Any]] = None
    open_position: Optional[Dict[str, Any]] = None

    for idx in range(len(candles_15m) - 1):
        signal_candle = candles_15m[idx]
        current_time_ms = int(signal_candle["t"])
        if current_time_ms < start_ms:
            continue

        next_candle = candles_15m[idx + 1]
        next_time_ms = int(next_candle["t"])

        if pending_order:
            fill_price = _limit_fill_price(pending_order["side"], pending_order["limit_price"], next_candle)
            if fill_price is not None:
                adjusted_entry = fill_price * (1 + (slippage_bps / 10_000)) if pending_order["side"] == "LONG" else fill_price * (1 - (slippage_bps / 10_000))
                notional = pending_order["notional_usd"]
                size = notional / adjusted_entry if adjusted_entry else 0.0
                entry_fee = _fee_amount(notional, fee_bps)
                equity -= entry_fee
                open_position = {
                    **pending_order,
                    "opened_idx": idx + 1,
                    "opened_at_ms": next_time_ms,
                    "entry_price": adjusted_entry,
                    "size": size,
                    "entry_fee": entry_fee,
                    "sl": pending_order["sl"],
                    "tp": pending_order["tp"],
                    "management_stage": "initial_risk",
                }
                pending_order = None
            else:
                pending_order["ttl_left"] -= 1
                if pending_order["ttl_left"] <= 0:
                    pending_order = None

        if open_position:
            if idx + 1 > open_position["opened_idx"]:
                exit_price, exit_reason = _exit_price_from_candle(
                    open_position["side"],
                    open_position["sl"],
                    open_position["tp"],
                    next_candle,
                )
                if exit_price is not None:
                    adjusted_exit = exit_price * (1 - (slippage_bps / 10_000)) if open_position["side"] == "LONG" else exit_price * (1 + (slippage_bps / 10_000))
                    pnl = _trade_pnl(open_position["side"], open_position["size"], open_position["entry_price"], adjusted_exit)
                    exit_fee = _fee_amount(open_position["size"] * adjusted_exit, fee_bps)
                    equity += pnl - exit_fee
                    trades.append(
                        BacktestTrade(
                            coin=coin,
                            side=open_position["side"],
                            regime=open_position["regime"],
                            opened_at=_ms_to_iso(open_position["opened_at_ms"]),
                            closed_at=_ms_to_iso(next_time_ms),
                            entry_price=round(open_position["entry_price"], 6),
                            exit_price=round(adjusted_exit, 6),
                            size=round(open_position["size"], 8),
                            notional_usd=round(open_position["notional_usd"], 4),
                            pnl_usd=round(pnl - open_position["entry_fee"] - exit_fee, 6),
                            pnl_pct=round(((pnl - open_position["entry_fee"] - exit_fee) / open_position["notional_usd"]) * 100, 6),
                            fees_usd=round(open_position["entry_fee"] + exit_fee, 6),
                            score=round(open_position["score"], 4),
                            entry_reason=open_position["entry_reason"],
                            exit_reason=exit_reason,
                            bars_held=(idx + 1) - open_position["opened_idx"],
                        )
                    )
                    open_position = None
                    equity_curve.append(equity)
                    continue

            closes_window = candle_closes(candles_15m[max(0, idx - 59) : idx + 2])
            atr_buffer = 0.0
            if len(closes_window) >= 15:
                atr_window = candles_15m[max(0, idx - 59) : idx + 2]
                atr = calculate_atr(atr_window, period=14)
                initial_stop_distance = abs(open_position["entry_price"] - open_position["original_sl"])
                atr_buffer = min(atr * 0.5, initial_stop_distance * 0.5)
            trailing = calculate_dynamic_stop_price(
                entry=open_position["entry_price"],
                current=float(next_candle["c"]),
                take_profit=open_position["tp"],
                is_long=open_position["side"] == "LONG",
                original_stop=open_position["sl"],
                atr_buffer=atr_buffer,
            )
            if open_position["side"] == "LONG":
                if trailing["stop_price"] > open_position["sl"]:
                    open_position["sl"] = trailing["stop_price"]
                    open_position["management_stage"] = trailing["stage"]
            else:
                if trailing["stop_price"] < open_position["sl"]:
                    open_position["sl"] = trailing["stop_price"]
                    open_position["management_stage"] = trailing["stage"]

            mark_to_market = equity + _trade_pnl(open_position["side"], open_position["size"], open_position["entry_price"], float(next_candle["c"]))
            equity_curve.append(mark_to_market)
            continue

        one_h_idx = bisect_right(one_h_times, current_time_ms) - 1
        btc_regime_idx = bisect_right(btc_one_h_times, current_time_ms) - 1
        if one_h_idx < 210 or btc_regime_idx < 210:
            equity_curve.append(equity)
            continue

        candles_15m_window = candles_15m[max(0, idx - 59) : idx + 1]
        candles_1h_window = candles_1h[max(0, one_h_idx - 219) : one_h_idx + 1]
        btc_1h_window = btc_1h[max(0, btc_regime_idx - 399) : btc_regime_idx + 1]

        regime = determine_market_regime_from_prices(candle_closes(btc_1h_window))
        if regime.get("regime") == "CHOP":
            equity_curve.append(equity)
            continue

        funding_rate = _latest_funding_rate(funding_rows, funding_times, current_time_ms)
        prev_day_px = _rolling_prev_day_px(candles_15m, idx)
        volume_24h_usd = _rolling_volume_usd(candles_15m, idx)

        scored = score_opportunity(
            coin=coin,
            candles=candles_15m_window,
            funding_rate=funding_rate,
            volume_24h=volume_24h_usd,
            prev_day_px=prev_day_px,
            regime=regime["regime"],
        )
        if scored.get("rejected"):
            equity_curve.append(equity)
            continue

        side = scored["suggested_side"]
        entry_setup = evaluate_pullback_entry_from_candles(side, candles_15m_window[-40:], coin=coin)
        htf_context = evaluate_higher_timeframe_context_from_candles(side, candles_1h_window, coin=coin)
        if not entry_setup.get("triggered") or not htf_context.get("context_ok"):
            equity_curve.append(equity)
            continue

        limit_price = float(entry_setup["entry_limit_price"])
        stop_price = limit_price * (1 - risk_pct / 100) if side == "LONG" else limit_price * (1 + risk_pct / 100)
        take_profit = limit_price * (1 + reward_pct / 100) if side == "LONG" else limit_price * (1 - reward_pct / 100)
        stop_distance_pct = abs(limit_price - stop_price) / limit_price * 100 if limit_price else 0.0
        notional_usd = calculate_risk_based_notional(
            equity=equity,
            risk_budget_pct=account_risk_pct,
            stop_distance_pct=stop_distance_pct,
        )
        pending_order = {
            "side": side,
            "limit_price": limit_price,
            "sl": stop_price,
            "original_sl": stop_price,
            "tp": take_profit,
            "regime": regime["regime"],
            "score": float(scored["score"]),
            "entry_reason": entry_setup.get("reason", "ok"),
            "notional_usd": notional_usd,
            "ttl_left": pending_ttl_candles,
        }
        equity_curve.append(equity)

    if open_position:
        final_candle = candles_15m[-1]
        final_close = float(final_candle["c"])
        pnl = _trade_pnl(open_position["side"], open_position["size"], open_position["entry_price"], final_close)
        exit_fee = _fee_amount(open_position["size"] * final_close, fee_bps)
        equity += pnl - exit_fee
        trades.append(
            BacktestTrade(
                coin=coin,
                side=open_position["side"],
                regime=open_position["regime"],
                opened_at=_ms_to_iso(open_position["opened_at_ms"]),
                closed_at=_ms_to_iso(int(final_candle["t"])),
                entry_price=round(open_position["entry_price"], 6),
                exit_price=round(final_close, 6),
                size=round(open_position["size"], 8),
                notional_usd=round(open_position["notional_usd"], 4),
                pnl_usd=round(pnl - open_position["entry_fee"] - exit_fee, 6),
                pnl_pct=round(((pnl - open_position["entry_fee"] - exit_fee) / open_position["notional_usd"]) * 100, 6),
                fees_usd=round(open_position["entry_fee"] + exit_fee, 6),
                score=round(open_position["score"], 4),
                entry_reason=open_position["entry_reason"],
                exit_reason="end_of_test",
                bars_held=(len(candles_15m) - 1) - open_position["opened_idx"],
            )
        )
        equity_curve.append(equity)

    wins = [trade for trade in trades if trade.pnl_usd > 0]
    losses = [trade for trade in trades if trade.pnl_usd <= 0]
    gross_profit = sum(trade.pnl_usd for trade in wins)
    gross_loss = abs(sum(trade.pnl_usd for trade in losses))
    by_regime: Dict[str, Dict[str, Any]] = {}
    for regime_name in {"BULL", "BEAR"}:
        regime_trades = [trade for trade in trades if trade.regime == regime_name]
        if regime_trades:
            regime_wins = [trade for trade in regime_trades if trade.pnl_usd > 0]
            by_regime[regime_name] = {
                "trades": len(regime_trades),
                "win_rate_pct": round((len(regime_wins) / len(regime_trades)) * 100, 2),
                "net_pnl_usd": round(sum(trade.pnl_usd for trade in regime_trades), 6),
            }

    result = {
        "config": {
            "coin": coin,
            "start": start,
            "end": end,
            "starting_equity": starting_equity,
            "risk_pct": risk_pct,
            "reward_pct": reward_pct,
            "account_risk_pct": account_risk_pct,
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
            "pending_ttl_candles": pending_ttl_candles,
        },
        "summary": {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round((len(wins) / len(trades)) * 100, 2) if trades else 0.0,
            "starting_equity": round(starting_equity, 6),
            "ending_equity": round(equity, 6),
            "net_pnl_usd": round(equity - starting_equity, 6),
            "net_return_pct": round(((equity / starting_equity) - 1) * 100, 6) if starting_equity else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 6) if gross_loss > 0 else None,
            "max_drawdown_pct": round(_max_drawdown(equity_curve), 6),
            "avg_trade_pnl_usd": round(sum(trade.pnl_usd for trade in trades) / len(trades), 6) if trades else 0.0,
        },
        "by_regime": by_regime,
        "trades": [trade.to_dict() for trade in trades],
        "notes": [
            "Backtest em candles de 15m com contexto 1h e regime BTC 1h.",
            "Entrada simulada via ordem limit com validade curta; trailing recalculado ao fecho de cada candle.",
            "Quando stop e take profit são tocados no mesmo candle, o resultado assume stop primeiro de forma conservadora.",
        ],
    }
    return result
