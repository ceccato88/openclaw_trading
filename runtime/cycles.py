import logging
from typing import Any, Dict, List, Optional

from runtime.health import record_cycle_result
from skills.dsl import run_dynamic_stop_loss
from skills.entry_manager import reconcile_pending_entries
from skills.portfolio import get_portfolio_status
from skills.risk_manager import check_daily_drawdown
from skills.scanner import run_opportunity_scanner
from skills.wolf_strategy import execute_wolf_strategy_trade

logger = logging.getLogger("RuntimeCycles")


def run_heartbeat_cycle() -> Dict[str, Any]:
    reconcile_result = reconcile_pending_entries()
    trailing_result = run_dynamic_stop_loss()
    risk_result = check_daily_drawdown(max_drawdown_pct=10.0)
    portfolio_result = get_portfolio_status()

    status = "success"
    for result in [reconcile_result, trailing_result, portfolio_result]:
        if result.get("status") in {"error", "partial_error"}:
            status = result["status"]
            break
    if not risk_result.get("can_trade", True) and status == "success":
        status = "blocked"

    result = {
        "status": status,
        "risk": risk_result,
        "reconcile_pending_entries": reconcile_result,
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
    min_volume: float = 5_000_000,
    max_results: int = 3,
) -> Dict[str, Any]:
    risk_result = check_daily_drawdown(max_drawdown_pct=10.0)
    if not risk_result["can_trade"]:
        result = {"status": "blocked", "message": risk_result["message"], "risk": risk_result}
        record_cycle_result("hunt", result)
        return result

    opportunities = run_opportunity_scanner(min_volume=min_volume, max_results=max_results)
    if not opportunities:
        result = {"status": "no_trade", "message": "Scanner sem oportunidades."}
        record_cycle_result("hunt", result)
        return result

    if isinstance(opportunities[0], dict) and opportunities[0].get("error"):
        result = {"status": "error", "message": opportunities[0]["error"], "scanner": opportunities}
        record_cycle_result("hunt", result)
        return result

    if isinstance(opportunities[0], dict) and opportunities[0].get("status") == "no_trade":
        result = {"status": "no_trade", "message": opportunities[0].get("reason", "market_regime_chop"), "scanner": opportunities}
        record_cycle_result("hunt", result)
        return result

    best_opportunity = choose_best_ready_opportunity(opportunities)
    if not best_opportunity:
        result = {
            "status": "waiting_entry",
            "message": "Existem candidatos, mas nenhum com trigger de entrada confirmado.",
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
    )
    result = {
        "status": trade_result.get("status", "unknown"),
        "selected_opportunity": best_opportunity,
        "trade": trade_result,
        "scanner": opportunities,
    }
    record_cycle_result("hunt", result)
    return result
