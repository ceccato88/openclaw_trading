from hl_client import logger, wallet_address
from skills.close_trade import close_position
from skills.support import get_account_equity_snapshot, get_frontend_open_orders, get_meta_context


def get_portfolio_status() -> dict:
    """
    Skill OpenClaw: Retorna um snapshot em tempo real da conta, posições e ordens abertas.
    """
    logger.info("A recolher snapshot do portefólio...")

    if not wallet_address:
        return {"status": "error", "message": "Cliente não inicializado."}

    try:
        account_snapshot = get_account_equity_snapshot()
        user_state = account_snapshot["user_state"]
        open_orders = get_frontend_open_orders()
        universe, asset_contexts = get_meta_context()
        prices_by_coin = {}

        for idx, asset in enumerate(universe):
            context = asset_contexts[idx]
            prices_by_coin[asset["name"]] = context.get("markPx") or context.get("midPx") or context.get("oraclePx")

        positions = []
        for wrapped_position in user_state.get("assetPositions", []):
            position = wrapped_position["position"]
            size = float(position["szi"])
            if size == 0:
                continue

            coin = position["coin"]
            positions.append({
                "coin": coin,
                "size": size,
                "entry_price": float(position["entryPx"]) if position.get("entryPx") is not None else None,
                "mark_price": float(prices_by_coin[coin]) if prices_by_coin.get(coin) is not None else None,
                "unrealized_pnl": float(position["unrealizedPnl"]),
                "margin_used": float(position["marginUsed"]),
                "position_value": float(position["positionValue"]),
                "liquidation_price": float(position["liquidationPx"]) if position.get("liquidationPx") is not None else None,
                "leverage": position.get("leverage"),
            })

        formatted_orders = []
        for order in open_orders:
            formatted_orders.append({
                "coin": order["coin"],
                "oid": order["oid"],
                "side": order["side"],
                "size": float(order["sz"]),
                "limit_price": float(order["limitPx"]),
                "reduce_only": bool(order.get("reduceOnly")),
                "is_trigger": bool(order.get("isTrigger")),
                "order_type": order.get("orderType"),
                "trigger_price": float(order["triggerPx"]) if order.get("triggerPx") is not None else None,
            })

        margin_summary = user_state["marginSummary"]
        return {
            "status": "success",
            "account_address": wallet_address,
            "account_mode": account_snapshot["account_mode"],
            "equity": account_snapshot["total_equity"],
            "perp_equity": account_snapshot["perp_equity"],
            "spot_usdc_total": account_snapshot["spot_usdc_total"],
            "spot_usdc_available": account_snapshot["spot_usdc_available"],
            "spot_balances": account_snapshot["spot_balances"],
            "total_margin_used": float(margin_summary["totalMarginUsed"]),
            "total_notional_position": float(margin_summary["totalNtlPos"]),
            "withdrawable": account_snapshot["withdrawable"],
            "positions": positions,
            "open_orders": formatted_orders,
        }
    except Exception as e:
        logger.error(f"Erro ao recolher o estado do portefólio: {e}")
        return {"status": "error", "message": str(e)}


def close_all_positions() -> dict:
    """
    Skill OpenClaw: Fecha todas as posições abertas e limpa as ordens pendentes relacionadas.
    """
    logger.info("🚨 A executar fecho total do portefólio...")

    snapshot = get_portfolio_status()
    if snapshot.get("status") != "success":
        return snapshot

    positions = snapshot["positions"]
    if not positions:
        return {"status": "success", "message": "Não existem posições abertas.", "results": []}

    results = []
    had_error = False

    for position in positions:
        result = close_position(position["coin"])
        if result.get("status") != "success":
            had_error = True
        results.append({"coin": position["coin"], "result": result})

    status = "partial_error" if had_error else "success"
    return {"status": status, "results": results}
