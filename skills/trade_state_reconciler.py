from __future__ import annotations

from hl_client import logger
from skills.support import (
    compute_protection_prices,
    delete_trade_state,
    get_active_protection_levels,
    get_open_position,
    list_trade_states,
    save_trade_state,
    upsert_trade_protection,
)


def _infer_pct_from_levels(entry_price: float, target_price: float, is_long: bool) -> float:
    if entry_price <= 0 or target_price <= 0:
        return 0.0
    if is_long:
        return abs((target_price - entry_price) / entry_price) * 100
    return abs((entry_price - target_price) / entry_price) * 100


def reconcile_trade_states() -> dict:
    """
    Verifica se cada trade_state local continua alinhado com a posição e proteção reais na exchange.
    Se a proteção tiver sido removida externamente, tenta restaurá-la.
    """
    logger.info("A reconciliar trade states com a exchange...")

    try:
        actions_taken = []
        had_partial_error = False

        for trade_state in list_trade_states():
            coin = trade_state["coin"]
            position = get_open_position(coin)

            if not position:
                delete_trade_state(coin)
                actions_taken.append({"coin": coin, "action": "stale_trade_state_removed"})
                continue

            size = abs(float(position["szi"]))
            entry_price = float(position["entryPx"])
            is_long = float(position["szi"]) > 0
            active_protection = get_active_protection_levels(coin)
            active_sl = active_protection.get("sl_price")
            active_tp = active_protection.get("tp_price")

            missing_sl = active_sl is None
            missing_tp = active_tp is None

            if not missing_sl and not missing_tp:
                synced = False
                if float(trade_state.get("sl", active_sl) or active_sl) != float(active_sl):
                    trade_state["sl"] = float(active_sl)
                    synced = True
                if float(trade_state.get("tp", active_tp) or active_tp) != float(active_tp):
                    trade_state["tp"] = float(active_tp)
                    synced = True
                if float(trade_state.get("entry", entry_price) or entry_price) != entry_price:
                    trade_state["entry"] = entry_price
                    synced = True
                if synced:
                    trade_state["status"] = "protected"
                    save_trade_state(coin, trade_state)
                    actions_taken.append({"coin": coin, "action": "trade_state_synced_to_exchange"})
                continue

            risk_pct = float(trade_state.get("risk_pct", 0.0) or 0.0)
            reward_pct = float(trade_state.get("reward_pct", 0.0) or 0.0)
            if risk_pct <= 0 and trade_state.get("sl") is not None:
                risk_pct = _infer_pct_from_levels(entry_price, float(trade_state["sl"]), is_long)
            if reward_pct <= 0 and trade_state.get("tp") is not None:
                reward_pct = _infer_pct_from_levels(entry_price, float(trade_state["tp"]), is_long)

            if risk_pct <= 0 or reward_pct <= 0:
                had_partial_error = True
                actions_taken.append(
                    {
                        "coin": coin,
                        "action": "protection_missing_but_restore_params_unavailable",
                        "missing_sl": missing_sl,
                        "missing_tp": missing_tp,
                    }
                )
                continue

            protection_prices = compute_protection_prices(
                coin=coin,
                entry_price=entry_price,
                risk_pct=risk_pct,
                reward_pct=reward_pct,
                is_long=is_long,
            )
            protection = upsert_trade_protection(
                coin,
                is_long,
                size,
                protection_prices["sl"],
                protection_prices["tp"],
                active_protection=active_protection,
            )
            trade_state["entry"] = entry_price
            trade_state["size"] = size
            trade_state["sl"] = protection["sl"]["price"]
            trade_state["tp"] = protection["tp"]["price"]
            trade_state["risk_pct"] = risk_pct
            trade_state["reward_pct"] = reward_pct
            trade_state["status"] = "protected"
            trade_state.setdefault("management_stage", "initial_risk")
            save_trade_state(coin, trade_state)
            actions_taken.append(
                {
                    "coin": coin,
                    "action": "missing_protection_restored",
                    "missing_sl": missing_sl,
                    "missing_tp": missing_tp,
                    "sl": protection["sl"]["price"],
                    "tp": protection["tp"]["price"],
                }
            )

        status = "partial_error" if had_partial_error else "success"
        return {"status": status, "actions_taken": actions_taken}
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Erro na reconciliação de trade states: {exc}")
        return {"status": "error", "message": str(exc)}
