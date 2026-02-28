import json
import time
from datetime import datetime, timezone

from hl_client import logger
from skills.support import (
    RISK_STATE_FILE,
    acquire_file_lock,
    ensure_state_dirs,
    get_account_equity_snapshot,
    release_file_lock,
)


def _acquire_daily_risk_lock(timeout_seconds: float = 5.0, poll_interval_seconds: float = 0.1):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        lock_path = acquire_file_lock("daily-risk-state")
        if lock_path is not None:
            return lock_path
        time.sleep(poll_interval_seconds)
    raise TimeoutError("Não foi possível adquirir lock do estado diário de risco.")

def check_daily_drawdown(max_drawdown_pct: float = 10.0) -> dict:
    """
    Skill de Uso Interno: Verifica se a conta atingiu o limite de perda diária.
    """
    try:
        lock_path = _acquire_daily_risk_lock()
        account_snapshot = get_account_equity_snapshot()
        user_state = account_snapshot["user_state"]
        margin_summary = user_state["marginSummary"]
        total_unrealized = sum(
            float(position["position"].get("unrealizedPnl", 0.0))
            for position in user_state.get("assetPositions", [])
        )
        if account_snapshot["account_mode"] == "unifiedAccount":
            current_equity = account_snapshot["total_equity"]
        else:
            current_equity = float(margin_summary["accountValue"]) - max(total_unrealized, 0.0)
        today_str = datetime.now(timezone.utc).date().isoformat()

        try:
            daily_state = {}
            if RISK_STATE_FILE.exists():
                with RISK_STATE_FILE.open("r", encoding="utf-8") as f:
                    daily_state = json.load(f)

            # Se for um dia novo, redefine o saldo inicial do dia
            if daily_state.get("date") != today_str:
                ensure_state_dirs()
                daily_state = {
                    "date": today_str,
                    "start_balance": current_equity
                }
                with RISK_STATE_FILE.open("w", encoding="utf-8") as f:
                    json.dump(daily_state, f)
                logger.info(f"Novo dia de negociação. Saldo inicial guardado: ${current_equity:.2f}")

            start_balance = daily_state["start_balance"]
            
            if start_balance > 0:
                drawdown_pct = ((start_balance - current_equity) / start_balance) * 100
            else:
                drawdown_pct = 0.0

            logger.info(f"Risco Diário: Drawdown atual é de {drawdown_pct:.2f}% (Limite: {max_drawdown_pct}%)")

            if drawdown_pct >= max_drawdown_pct:
                logger.warning(f"🚨 ALERTA: Stop Loss Diário de {max_drawdown_pct}% Atingido! Negociação bloqueada.")
                return {
                    "can_trade": False,
                    "message": f"Stop diário de {max_drawdown_pct}% atingido.",
                    "account_mode": account_snapshot["account_mode"],
                    "current_equity": current_equity,
                    "start_balance": start_balance,
                    "drawdown_pct": drawdown_pct,
                }

            return {
                "can_trade": True,
                "message": "Risco diário dentro do limite.",
                "account_mode": account_snapshot["account_mode"],
                "current_equity": current_equity,
                "start_balance": start_balance,
                "drawdown_pct": drawdown_pct,
            }
        finally:
            release_file_lock(lock_path)

    except Exception as e:
        logger.error(f"Erro ao verificar risco diário: {e}")
        return {"can_trade": False, "message": str(e)}
