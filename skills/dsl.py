from hl_client import get_exchange, logger
from skills.signals import (
    TRAILING_BREAKEVEN_PROGRESS,
    calculate_atr,
    calculate_dynamic_stop_price,
    fetch_candles,
)
from skills.support import (
    compute_protection_prices,
    get_active_protection_levels,
    get_meta_context,
    get_open_positions,
    load_trade_state,
    save_trade_state,
    upsert_trade_protection,
)

BREAKEVEN_ATR_MULTIPLIER = 0.5
BREAKEVEN_BUFFER_MAX_INITIAL_RISK_SHARE = 0.5
TRAILING_ATR_INTERVAL = "15m"
TRAILING_ATR_LOOKBACK = 30
TRAILING_ATR_PERIOD = 14


def run_dynamic_stop_loss() -> dict:
    """
    Skill OpenClaw: Verifica as posições abertas e ajusta o stop de forma dinâmica por progresso.
    """
    logger.info("A verificar trailing dinâmico das posições abertas...")
    
    try:
        get_exchange()
        positions = get_open_positions()
        universe, asset_contexts = get_meta_context()
        context_by_coin = {asset["name"]: asset_contexts[idx] for idx, asset in enumerate(universe)}
        
        actions_taken = []

        for p_data in positions:
            coin = p_data["coin"]
            size = float(p_data["szi"])
            
            if size == 0:
                continue
                
            entry_px = float(p_data["entryPx"])
            is_long = size > 0
            
            # Identifica o preço de marcação atual
            context = context_by_coin.get(coin)
            if not context:
                continue

            raw_current_price = context.get('markPx') or context.get('midPx') or context.get('oraclePx')
            if raw_current_price is None:
                continue
            current_price = float(raw_current_price)
            
            # Procura pelo ficheiro de configuração deste trade
            trade_data = load_trade_state(coin)
            if not trade_data:
                continue

            trade_status = str(trade_data.get("status", "protected"))
            active_protection = get_active_protection_levels(coin)

            if trade_status == "filled_awaiting_protection" or trade_data.get("sl") is None or trade_data.get("tp") is None:
                risk_pct = float(trade_data.get("risk_pct", 0.0) or 0.0)
                reward_pct = float(trade_data.get("reward_pct", 0.0) or 0.0)
                if risk_pct <= 0 or reward_pct <= 0:
                    actions_taken.append({
                        "coin": coin,
                        "action": "awaiting_protection_missing_risk_params",
                    })
                    continue

                protection_prices = compute_protection_prices(
                    coin=coin,
                    entry_price=entry_px,
                    risk_pct=risk_pct,
                    reward_pct=reward_pct,
                    is_long=is_long,
                )
                protection = upsert_trade_protection(
                    coin,
                    is_long,
                    abs(size),
                    protection_prices["sl"],
                    protection_prices["tp"],
                    active_protection=active_protection,
                )
                trade_data["entry"] = entry_px
                trade_data["size"] = abs(size)
                trade_data["sl"] = protection["sl"]["price"]
                trade_data["tp"] = protection["tp"]["price"]
                trade_data["management_stage"] = "initial_risk"
                trade_data["status"] = "protected"
                save_trade_state(coin, trade_data)
                actions_taken.append({
                    "coin": coin,
                    "action": "protection_completed_from_partial_state",
                    "new_stop": protection["sl"]["price"],
                    "take_profit": protection["tp"]["price"],
                })
                active_protection = get_active_protection_levels(coin)

            tp_price = float(trade_data["tp"])
            original_sl = float(trade_data["sl"])
            original_tp = float(trade_data["tp"])
            current_sl = float(active_protection["sl_price"]) if active_protection["sl_price"] is not None else original_sl
            current_tp = float(active_protection["tp_price"]) if active_protection["tp_price"] is not None else original_tp
            atr_buffer = 0.0
            try:
                trailing_candles = fetch_candles(coin, interval=TRAILING_ATR_INTERVAL, lookback=TRAILING_ATR_LOOKBACK)
                atr_value = calculate_atr(trailing_candles, period=TRAILING_ATR_PERIOD)
                raw_buffer = atr_value * BREAKEVEN_ATR_MULTIPLIER
                initial_risk_distance = abs(entry_px - current_sl)
                max_buffer = initial_risk_distance * BREAKEVEN_BUFFER_MAX_INITIAL_RISK_SHARE
                atr_buffer = min(raw_buffer, max_buffer) if max_buffer > 0 else 0.0
            except Exception as atr_error:
                logger.warning(f"Falha ao calcular ATR de trailing para {coin}: {atr_error}")

            desired = calculate_dynamic_stop_price(
                entry_px,
                current_price,
                tp_price,
                is_long,
                current_sl,
                atr_buffer=atr_buffer,
            )
            new_stop = float(desired["stop_price"])
            stage = desired["stage"]
            progress = float(desired["progress"])

            should_improve = (is_long and new_stop > current_sl) or ((not is_long) and new_stop < current_sl)
            if not should_improve:
                continue

            logger.info(f"🎯 {coin}: progresso {progress*100:.1f}% | novo estágio {stage} | stop alvo {new_stop}")
            canceled_order_ids = []

            try:
                protection = upsert_trade_protection(
                    coin,
                    is_long,
                    abs(size),
                    new_stop,
                    tp_price,
                    active_protection=active_protection,
                )

                trade_data["breakeven_done"] = progress >= TRAILING_BREAKEVEN_PROGRESS
                trade_data["entry"] = entry_px
                trade_data["size"] = abs(size)
                trade_data["sl"] = protection["sl"]["price"]
                trade_data["tp"] = protection["tp"]["price"]
                trade_data["management_stage"] = stage
                trade_data["last_progress"] = progress
                save_trade_state(coin, trade_data)

                actions_taken.append({
                    "coin": coin,
                    "action": "stop_updated",
                    "stage": stage,
                    "progress_pct": round(progress * 100, 2),
                    "atr_buffer": round(atr_buffer, 6),
                    "new_stop": protection["sl"]["price"],
                    "cancelled_order_ids": canceled_order_ids,
                })
            except Exception as coin_error:
                logger.error(f"Falha ao reajustar proteção de {coin}: {coin_error}")
                recovery = "not_attempted"

                try:
                    upsert_trade_protection(
                        coin,
                        is_long,
                        abs(size),
                        current_sl,
                        current_tp,
                        active_protection=get_active_protection_levels(coin),
                    )
                    recovery = "previous_protection_restored"
                except Exception as restore_error:
                    recovery = f"restore_failed: {restore_error}"
                    logger.error(f"Falha ao restaurar proteção anterior de {coin}: {restore_error}")

                actions_taken.append({
                    "coin": coin,
                    "action": "stop_update_failed",
                    "stage": stage,
                    "progress_pct": round(progress * 100, 2),
                    "atr_buffer": round(atr_buffer, 6),
                    "message": str(coin_error),
                    "recovery": recovery,
                    "cancelled_order_ids": canceled_order_ids,
                })

        final_status = "success"
        if any(action["action"] == "stop_update_failed" for action in actions_taken):
            final_status = "partial_error"

        return {"status": final_status, "actions_taken": actions_taken}

    except Exception as e:
        logger.error(f"Erro no módulo de Breakeven: {e}")
        return {"status": "error", "message": str(e)}
