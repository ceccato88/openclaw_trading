from hl_client import info, logger
from skills.signals import evaluate_pullback_entry, get_market_regime, score_opportunity
from skills.support import check_portfolio_heat, get_meta_context

def run_opportunity_scanner(min_volume: float = 5000000, max_results: int = 3, candidate_pool_size: int = 20) -> list:
    """
    Skill OpenClaw: Analisa o mercado à procura de oportunidades com regime, multi-fator e timing de entrada.
    """
    logger.info("A iniciar o Opportunity Scanner...")
    
    if not info:
        return [{"error": "Cliente Info não inicializado"}]

    try:
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

        liquid_candidates.sort(key=lambda item: item[1], reverse=True)
        liquid_candidates = liquid_candidates[:candidate_pool_size]

        opportunities = []

        for idx, _volume in liquid_candidates:
            ctx = asset_contexts[idx]
            coin = universe[idx]['name']
            volume_24h = float(ctx['dayNtlVlm'])
            raw_price = ctx.get('markPx') or ctx.get('midPx') or ctx.get('oraclePx')
            raw_prev_day_px = ctx.get('prevDayPx')
            raw_funding = ctx.get('funding') or 0

            if raw_price is None or raw_prev_day_px is None:
                continue

            price = float(raw_price)
            funding_rate = float(raw_funding)
            prev_day_px = float(raw_prev_day_px)
            try:
                from skills.signals import fetch_candles

                candles = fetch_candles(coin, interval="15m", lookback=60)
            except Exception as candle_error:
                logger.warning(f"Falha ao obter candles de {coin}: {candle_error}")
                continue

            if len(candles) < 30:
                continue

            scored = score_opportunity(
                coin=coin,
                candles=candles,
                funding_rate=funding_rate,
                volume_24h=volume_24h,
                prev_day_px=prev_day_px,
                regime=regime["regime"],
            )
            if scored.get("rejected"):
                continue

            entry = evaluate_pullback_entry(coin, scored["suggested_side"], interval="15m", lookback=40)
            heat = check_portfolio_heat(new_side=scored["suggested_side"])
            scored["market_regime"] = regime["regime"]
            scored["entry_setup"] = entry
            scored["portfolio_heat_ok"] = heat["can_trade"]
            scored["portfolio_heat_message"] = heat["message"]
            scored["entry_ready"] = bool(entry.get("triggered")) and heat["can_trade"]
            scored["price"] = price
            opportunities.append(scored)

        opportunities = sorted(opportunities, key=lambda x: x['score'], reverse=True)
        top_picks = opportunities[:max_results]
        
        logger.info(f"Scanner finalizado. {len(top_picks)} ativos selecionados.")
        return top_picks

    except Exception as e:
        logger.error(f"Erro no scanner: {e}")
        return [{"error": str(e)}]
