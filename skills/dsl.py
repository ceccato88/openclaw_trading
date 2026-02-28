from hl_client import exchange, logger
from skills.signals import calculate_dynamic_stop_price
from skills.support import (
    cancel_orders_for_coin,
    get_meta_context,
    get_open_positions,
    load_trade_state,
    place_trade_protection,
    save_trade_state,
)

def run_dynamic_stop_loss() -> dict:
    """
    Skill OpenClaw: Verifica as posições abertas e ajusta o stop de forma dinâmica por progresso.
    """
    logger.info("A verificar trailing dinâmico das posições abertas...")
    
    if not exchange:
        return {"status": "error", "message": "Clientes não inicializados"}

    try:
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
                
            tp_price = float(trade_data["tp"])
            original_sl = float(trade_data["sl"])
            original_tp = float(trade_data["tp"])
            
            desired = calculate_dynamic_stop_price(entry_px, current_price, tp_price, is_long, original_sl)
            new_stop = float(desired["stop_price"])
            stage = desired["stage"]
            progress = float(desired["progress"])

            should_improve = (is_long and new_stop > original_sl) or ((not is_long) and new_stop < original_sl)
            if not should_improve:
                continue

            logger.info(f"🎯 {coin}: progresso {progress*100:.1f}% | novo estágio {stage} | stop alvo {new_stop}")
            canceled_order_ids = []

            try:
                canceled_order_ids = cancel_orders_for_coin(coin, only_reduce_only=True)
                protection = place_trade_protection(coin, is_long, abs(size), new_stop, tp_price)

                trade_data["breakeven_done"] = progress >= 0.33
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
                    "new_stop": protection["sl"]["price"],
                    "cancelled_order_ids": canceled_order_ids,
                })
            except Exception as coin_error:
                logger.error(f"Falha ao reajustar proteção de {coin}: {coin_error}")
                recovery = "not_attempted"

                try:
                    cancel_orders_for_coin(coin, only_reduce_only=True)
                    place_trade_protection(coin, is_long, abs(size), original_sl, original_tp)
                    recovery = "previous_protection_restored"
                except Exception as restore_error:
                    recovery = f"restore_failed: {restore_error}"
                    logger.error(f"Falha ao restaurar proteção anterior de {coin}: {restore_error}")

                actions_taken.append({
                    "coin": coin,
                    "action": "stop_update_failed",
                    "stage": stage,
                    "progress_pct": round(progress * 100, 2),
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
