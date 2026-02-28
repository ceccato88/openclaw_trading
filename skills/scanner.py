from concurrent.futures import ThreadPoolExecutor, as_completed

from hl_client import get_info, logger
from skills.signals import (
    evaluate_pullback_entry_from_candles,
    evaluate_higher_timeframe_context_from_candles,
    fetch_candles,
    get_market_regime,
    score_opportunity,
)
from skills.support import check_portfolio_heat, get_meta_context

DEFAULT_CANDIDATE_POOL_SIZE = 40
DEFAULT_SCANNER_WORKERS = 8


def _score_candidate(
    idx: int,
    universe: list,
    asset_contexts: list,
    regime: dict,
    liquid_candidates_count: int,
    candidate_pool_size: int,
):
    ctx = asset_contexts[idx]
    coin = universe[idx]["name"]
    volume_24h = float(ctx["dayNtlVlm"])
    raw_price = ctx.get("markPx") or ctx.get("midPx") or ctx.get("oraclePx")
    raw_prev_day_px = ctx.get("prevDayPx")
    raw_funding = ctx.get("funding") or 0

    if raw_price is None or raw_prev_day_px is None:
        return None

    price = float(raw_price)
    funding_rate = float(raw_funding)
    prev_day_px = float(raw_prev_day_px)

    try:
        candles = fetch_candles(coin, interval="15m", lookback=60)
        higher_timeframe_candles = fetch_candles(coin, interval="1h", lookback=120)
    except Exception as candle_error:  # noqa: BLE001
        logger.warning(f"Falha ao obter candles de {coin}: {candle_error}")
        return None

    if len(candles) < 30 or len(higher_timeframe_candles) < 50:
        return None

    scored = score_opportunity(
        coin=coin,
        candles=candles,
        funding_rate=funding_rate,
        volume_24h=volume_24h,
        prev_day_px=prev_day_px,
        regime=regime["regime"],
    )
    if scored.get("rejected"):
        return {"scored": True, "opportunity": None}

    entry = evaluate_pullback_entry_from_candles(
        scored["suggested_side"],
        candles[-40:],
    )
    higher_timeframe_context = evaluate_higher_timeframe_context_from_candles(
        scored["suggested_side"],
        higher_timeframe_candles,
    )
    entry.update({"coin": coin, "interval": "15m"})
    entry["higher_timeframe_context"] = higher_timeframe_context
    if entry.get("triggered") and not higher_timeframe_context.get("context_ok"):
        entry["triggered"] = False
        entry["reason"] = higher_timeframe_context.get("reason", "higher_timeframe_context_filter")
    heat = check_portfolio_heat(new_coin=coin, new_side=scored["suggested_side"])
    scored["market_regime"] = regime["regime"]
    scored["entry_setup"] = entry
    scored["portfolio_heat_ok"] = heat["can_trade"]
    scored["portfolio_heat_message"] = heat["message"]
    scored["entry_ready"] = bool(entry.get("triggered")) and heat["can_trade"]
    scored["price"] = price
    scored["scanner_context"] = {
        "candidate_pool_size": candidate_pool_size,
        "liquid_candidates_count": liquid_candidates_count,
    }
    return {"scored": True, "opportunity": scored}


def run_opportunity_scanner(
    min_volume: float = 5000000,
    max_results: int = 3,
    candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    max_workers: int = DEFAULT_SCANNER_WORKERS,
) -> list:
    """
    Skill OpenClaw: Analisa o mercado à procura de oportunidades com regime, multi-fator e timing de entrada.
    """
    logger.info("A iniciar o Opportunity Scanner...")

    try:
        get_info()
        regime = get_market_regime()
        if regime["regime"] == "CHOP":
            logger.info("Scanner abortado: regime de mercado em CHOP.")
            return [{"status": "no_trade", "reason": "market_regime_chop", "regime": regime}]

        universe, asset_contexts = get_meta_context()
        liquid_candidates = []

        for idx, ctx in enumerate(asset_contexts):
            volume_24h = float(ctx["dayNtlVlm"])
            if volume_24h < min_volume:
                continue
            liquid_candidates.append((idx, volume_24h))

        liquid_candidates_count = len(liquid_candidates)
        liquid_candidates.sort(key=lambda item: item[1], reverse=True)
        liquid_candidates = liquid_candidates[:candidate_pool_size]

        opportunities = []
        scored_candidates = 0
        worker_count = max(1, min(max_workers, len(liquid_candidates)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    _score_candidate,
                    idx,
                    universe,
                    asset_contexts,
                    regime,
                    liquid_candidates_count,
                    candidate_pool_size,
                )
                for idx, _volume in liquid_candidates
            ]

            for future in as_completed(futures):
                result = future.result()
                if not result:
                    continue
                if result.get("scored"):
                    scored_candidates += 1
                opportunity = result.get("opportunity")
                if not opportunity:
                    continue
                opportunity["scanner_context"]["scored_candidates"] = scored_candidates
                opportunity["scanner_context"]["max_workers"] = worker_count
                opportunities.append(opportunity)

        opportunities = sorted(opportunities, key=lambda x: x['score'], reverse=True)
        top_picks = opportunities[:max_results]
        
        logger.info(
            "Scanner finalizado. %s ativos selecionados | liquidos=%s | scoreados=%s | pool=%s",
            len(top_picks),
            liquid_candidates_count,
            scored_candidates,
            candidate_pool_size,
        )
        return top_picks

    except Exception as e:
        logger.error(f"Erro no scanner: {e}")
        return [{"error": str(e)}]
