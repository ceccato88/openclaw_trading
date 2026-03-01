import logging
from typing import Any, Dict, List, Optional

from runtime.health import record_cycle_result
from skills.dsl import run_dynamic_stop_loss
from skills.entry_manager import reconcile_pending_entries
from skills.portfolio import get_portfolio_status
from skills.risk_manager import check_daily_drawdown
from skills.scanner import run_opportunity_scanner
from skills.trade_state_reconciler import reconcile_trade_states
from skills.wolf_strategy import execute_wolf_strategy_trade

logger = logging.getLogger("RuntimeCycles")


def run_heartbeat_cycle() -> Dict[str, Any]:
    reconcile_result = reconcile_pending_entries()
    trade_state_reconcile_result = reconcile_trade_states()
    trailing_result = run_dynamic_stop_loss()
    risk_result = check_daily_drawdown()
    portfolio_result = get_portfolio_status()

    status = "success"
    for result in [reconcile_result, trade_state_reconcile_result, trailing_result, portfolio_result]:
        if result.get("status") in {"error", "partial_error"}:
            status = result["status"]
            break
    if not risk_result.get("can_trade", True) and status == "success":
        status = "blocked"

    result = {
        "status": status,
        "risk": risk_result,
        "reconcile_pending_entries": reconcile_result,
        "reconcile_trade_states": trade_state_reconcile_result,
        "trailing_stop": trailing_result,
        "portfolio": portfolio_result,
    }
    record_cycle_result("heartbeat", result)
    return result


def choose_best_ready_opportunity(opportunities: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ready = [opportunity for opportunity in opportunities if opportunity.get("entry_ready")]
    if not ready:
        return None
    return sorted(ready, key=lambda item: item.get("score", 0), reverse=True)[0]


def run_hunt_cycle(
    position_usd: float = 25.0,
    leverage: int = 10,
    risk_pct: float = 2.0,
    account_risk_pct: float = 1.0,
    reward_pct: float | None = None,
    min_volume: float = 5_000_000,
    max_results: int = 3,
) -> Dict[str, Any]:
    risk_result = check_daily_drawdown()
    if not risk_result["can_trade"]:
        result = {"status": "blocked", "message": risk_result["message"], "risk": risk_result}
        record_cycle_result("hunt", result)
        return result

    reconcile_result = reconcile_pending_entries()
    if reconcile_result.get("status") in {"error", "partial_error"}:
        result = {
            "status": reconcile_result["status"],
            "message": "Falha ao reconciliar entradas pendentes antes da caça.",
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
        }
        record_cycle_result("hunt", result)
        return result

    trade_state_reconcile_result = reconcile_trade_states()
    if trade_state_reconcile_result.get("status") in {"error", "partial_error"}:
        result = {
            "status": trade_state_reconcile_result["status"],
            "message": "Falha ao reconciliar trade states antes da caça.",
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
            "reconcile_trade_states": trade_state_reconcile_result,
        }
        record_cycle_result("hunt", result)
        return result

    trailing_result = run_dynamic_stop_loss()
    if trailing_result.get("status") in {"error", "partial_error"}:
        result = {
            "status": trailing_result["status"],
            "message": "Falha ao atualizar trailing stop antes da caça.",
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
            "reconcile_trade_states": trade_state_reconcile_result,
            "trailing_stop": trailing_result,
        }
        record_cycle_result("hunt", result)
        return result

    portfolio_result = get_portfolio_status()
    if portfolio_result.get("status") in {"error", "partial_error"}:
        result = {
            "status": portfolio_result["status"],
            "message": "Falha ao obter portfolio antes da caça.",
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
            "reconcile_trade_states": trade_state_reconcile_result,
            "trailing_stop": trailing_result,
            "portfolio": portfolio_result,
        }
        record_cycle_result("hunt", result)
        return result

    opportunities = run_opportunity_scanner(min_volume=min_volume, max_results=max_results)
    if not opportunities:
        result = {
            "status": "no_trade",
            "message": "Scanner sem oportunidades.",
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
            "reconcile_trade_states": trade_state_reconcile_result,
            "trailing_stop": trailing_result,
            "portfolio": portfolio_result,
        }
        record_cycle_result("hunt", result)
        return result

    if isinstance(opportunities[0], dict) and opportunities[0].get("error"):
        result = {
            "status": "error",
            "message": opportunities[0]["error"],
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
            "reconcile_trade_states": trade_state_reconcile_result,
            "trailing_stop": trailing_result,
            "portfolio": portfolio_result,
            "scanner": opportunities,
        }
        record_cycle_result("hunt", result)
        return result

    if isinstance(opportunities[0], dict) and opportunities[0].get("status") == "no_trade":
        result = {
            "status": "no_trade",
            "message": opportunities[0].get("reason", "market_regime_chop"),
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
            "reconcile_trade_states": trade_state_reconcile_result,
            "trailing_stop": trailing_result,
            "portfolio": portfolio_result,
            "scanner": opportunities,
        }
        record_cycle_result("hunt", result)
        return result

    best_opportunity = choose_best_ready_opportunity(opportunities)
    if not best_opportunity:
        result = {
            "status": "waiting_entry",
            "message": "Existem candidatos, mas nenhum com trigger de entrada confirmado.",
            "risk": risk_result,
            "reconcile_pending_entries": reconcile_result,
            "reconcile_trade_states": trade_state_reconcile_result,
            "trailing_stop": trailing_result,
            "portfolio": portfolio_result,
            "scanner": opportunities,
        }
        record_cycle_result("hunt", result)
        return result

    trade_result = execute_wolf_strategy_trade(
        coin=best_opportunity["coin"],
        side=best_opportunity["suggested_side"],
        usdt_size=position_usd,
        leverage=leverage,
        risk_pct=risk_pct,
        account_risk_pct=account_risk_pct,
        reward_pct=reward_pct,
    )
    result = {
        "status": trade_result.get("status", "unknown"),
        "risk": risk_result,
        "reconcile_pending_entries": reconcile_result,
        "reconcile_trade_states": trade_state_reconcile_result,
        "trailing_stop": trailing_result,
        "portfolio": portfolio_result,
        "selected_opportunity": best_opportunity,
        "trade": trade_result,
        "scanner": opportunities,
    }
    record_cycle_result("hunt", result)
    return result
