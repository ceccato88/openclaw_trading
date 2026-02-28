from hl_client import get_exchange, logger
from skills.support import (
    acquire_file_lock,
    cancel_orders_for_coin,
    compute_protection_prices,
    delete_pending_entry_state,
    ensure_exchange_ok,
    get_frontend_open_orders,
    get_open_position,
    is_protection_order,
    list_pending_entry_states,
    load_trade_state,
    place_trade_protection,
    release_file_lock,
    save_trade_state,
    with_retry,
)

ENTRY_DRIFT_WARNING_PCT = 0.5
ENTRY_DRIFT_CRITICAL_PCT = 1.0


def reconcile_pending_entries() -> dict:
    """
    Skill interna: verifica ordens limit pendentes e, quando houver fill, planta a proteção.
    """
    logger.info("A reconciliar ordens pendentes...")

    try:
        exchange = get_exchange()
        open_orders = get_frontend_open_orders()
        open_orders_by_oid = {order["oid"]: order for order in open_orders}
        actions = []
        warnings = []

        for pending in list_pending_entry_states():
            coin = pending["coin"]
            lock_path = acquire_file_lock(f"pending-entry-{coin}")
            if lock_path is None:
                actions.append({"coin": coin, "action": "lock_busy_skip"})
                continue

            try:
                existing_trade = load_trade_state(coin)
                if existing_trade:
                    delete_pending_entry_state(coin)
                    actions.append({"coin": coin, "action": "pending_state_removed_existing_trade"})
                    continue

                position = get_open_position(coin)
                entry_oid = pending.get("entry_oid")
                order_still_open = entry_oid in open_orders_by_oid if entry_oid is not None else False
                coin_orders = [order for order in open_orders if order.get("coin") == coin]
                protection_orders = [
                    order
                    for order in coin_orders
                    if is_protection_order(order)
                ]

                if position:
                    actual_size = abs(float(position["szi"]))
                    planned_size = abs(float(pending.get("planned_size", actual_size) or actual_size))
                    size_mismatch = abs(actual_size - planned_size) > max(planned_size * 0.05, 1e-9)
                    fill_ratio = (actual_size / planned_size) if planned_size > 0 else 1.0
                    partial_fill_detected = size_mismatch and actual_size < planned_size
                    overfill_detected = size_mismatch and actual_size > planned_size
                    is_long = float(position["szi"]) > 0
                    entry_price = float(position["entryPx"])
                    planned_entry_price = float(pending.get("entry_limit_price", entry_price) or entry_price)
                    entry_drift_pct = (
                        abs(entry_price - planned_entry_price) / planned_entry_price * 100
                        if planned_entry_price > 0
                        else 0.0
                    )
                    entry_drift_detected = entry_drift_pct >= ENTRY_DRIFT_WARNING_PCT
                    entry_drift_critical = entry_drift_pct >= ENTRY_DRIFT_CRITICAL_PCT
                    reward_pct = float(pending["reward_pct"])
                    risk_pct = float(pending["risk_pct"])

                    if size_mismatch:
                        mismatch_payload = {
                            "coin": coin,
                            "planned_size": planned_size,
                            "actual_size": actual_size,
                            "fill_ratio": round(fill_ratio, 4),
                            "partial_fill_detected": partial_fill_detected,
                            "overfill_detected": overfill_detected,
                        }
                        warnings.append(mismatch_payload)
                        logger.warning(
                            "ALERTA DE TAMANHO NA RECONCILIACAO | %s | planeado=%s | atual=%s | fill_ratio=%.4f",
                            coin,
                            planned_size,
                            actual_size,
                            fill_ratio,
                        )

                    if entry_drift_detected:
                        drift_payload = {
                            "coin": coin,
                            "planned_entry_price": planned_entry_price,
                            "actual_entry_price": entry_price,
                            "entry_drift_pct": round(entry_drift_pct, 4),
                            "entry_drift_detected": entry_drift_detected,
                            "entry_drift_critical": entry_drift_critical,
                        }
                        warnings.append(drift_payload)
                        logger.warning(
                            "ALERTA DE ENTRY DRIFT | %s | planeado=%s | fill=%s | drift=%.4f%% | critical=%s",
                            coin,
                            planned_entry_price,
                            entry_price,
                            entry_drift_pct,
                            entry_drift_critical,
                        )

                    if order_still_open and entry_oid is not None:
                        try:
                            cancel_response = with_retry(
                                lambda: exchange.cancel(coin, entry_oid),
                                action_label=f"cancelar restante da entrada {entry_oid} em {coin}",
                            )
                            ensure_exchange_ok(cancel_response, f"cancelar restante da entrada {entry_oid} em {coin}")
                        except Exception as cancel_error:
                            logger.warning(f"Falha ao cancelar restante da ordem pendente {entry_oid} em {coin}: {cancel_error}")
                    protection_prices = compute_protection_prices(
                        coin=coin,
                        entry_price=entry_price,
                        risk_pct=risk_pct,
                        reward_pct=reward_pct,
                        is_long=is_long,
                    )
                    sl_price = protection_prices["sl"]
                    tp_price = protection_prices["tp"]

                    if protection_orders:
                        save_trade_state(coin, {
                            "coin": coin,
                            "entry": entry_price,
                            "planned_entry_price": planned_entry_price,
                            "filled_size": actual_size,
                            "planned_size": planned_size,
                            "fill_ratio": fill_ratio,
                            "size_mismatch": size_mismatch,
                            "partial_fill_detected": partial_fill_detected,
                            "overfill_detected": overfill_detected,
                            "entry_drift_pct": entry_drift_pct,
                            "entry_drift_detected": entry_drift_detected,
                            "entry_drift_critical": entry_drift_critical,
                            "tp": tp_price,
                            "sl": sl_price,
                            "risk_pct": risk_pct,
                            "reward_pct": reward_pct,
                            "breakeven_done": False,
                            "management_stage": "initial_risk",
                        })
                        delete_pending_entry_state(coin)
                        actions.append({
                            "coin": coin,
                            "action": "partial_fill_existing_protection_detected" if partial_fill_detected else "existing_protection_detected",
                            "entry": entry_price,
                            "planned_entry_price": planned_entry_price,
                            "size": actual_size,
                            "planned_size": planned_size,
                            "fill_ratio": round(fill_ratio, 4),
                            "size_mismatch": size_mismatch,
                            "partial_fill_detected": partial_fill_detected,
                            "overfill_detected": overfill_detected,
                            "entry_drift_pct": round(entry_drift_pct, 4),
                            "entry_drift_detected": entry_drift_detected,
                            "entry_drift_critical": entry_drift_critical,
                        })
                        continue

                    protection = place_trade_protection(coin, is_long, actual_size, sl_price, tp_price)
                    save_trade_state(coin, {
                        "coin": coin,
                        "entry": entry_price,
                        "planned_entry_price": planned_entry_price,
                        "filled_size": actual_size,
                        "planned_size": planned_size,
                        "fill_ratio": fill_ratio,
                        "size_mismatch": size_mismatch,
                        "partial_fill_detected": partial_fill_detected,
                        "overfill_detected": overfill_detected,
                        "entry_drift_pct": entry_drift_pct,
                        "entry_drift_detected": entry_drift_detected,
                        "entry_drift_critical": entry_drift_critical,
                        "tp": protection["tp"]["price"],
                        "sl": protection["sl"]["price"],
                        "risk_pct": risk_pct,
                        "reward_pct": reward_pct,
                        "breakeven_done": False,
                        "management_stage": "initial_risk",
                    })
                    delete_pending_entry_state(coin)
                    actions.append({
                        "coin": coin,
                        "action": "partial_fill_protected" if partial_fill_detected else "filled_and_protected",
                        "entry": entry_price,
                        "planned_entry_price": planned_entry_price,
                        "size": actual_size,
                        "planned_size": planned_size,
                        "fill_ratio": round(fill_ratio, 4),
                        "size_mismatch": size_mismatch,
                        "partial_fill_detected": partial_fill_detected,
                        "overfill_detected": overfill_detected,
                        "entry_drift_pct": round(entry_drift_pct, 4),
                        "entry_drift_detected": entry_drift_detected,
                        "entry_drift_critical": entry_drift_critical,
                    })
                    continue

                if not order_still_open:
                    delete_pending_entry_state(coin)
                    actions.append({"coin": coin, "action": "pending_order_closed_without_position"})
                    continue

                actions.append({"coin": coin, "action": "waiting_fill", "entry_oid": entry_oid})
            finally:
                release_file_lock(lock_path)

        return {"status": "success", "actions_taken": actions, "warnings": warnings}
    except Exception as e:
        logger.error(f"Erro ao reconciliar ordens pendentes: {e}")
        return {"status": "error", "message": str(e)}
