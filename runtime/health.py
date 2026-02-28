from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
HEALTH_DIR = BASE_DIR / "var" / "openclaw"
HEALTH_FILE = HEALTH_DIR / "runtime_health.json"

DEFAULT_HEALTH_STATE = {
    "cycles": {
        "heartbeat": {},
        "hunt": {},
    }
}
FAILURE_STATUSES = {"error", "partial_error"}
HEALTHY_STATUSES = {"success", "no_trade", "waiting_entry", "pending_entry", "blocked"}


def _ensure_health_dir() -> None:
    HEALTH_DIR.mkdir(parents=True, exist_ok=True)


def load_runtime_health_state() -> Dict[str, Any]:
    _ensure_health_dir()
    if not HEALTH_FILE.exists():
        return json.loads(json.dumps(DEFAULT_HEALTH_STATE))
    with HEALTH_FILE.open("r", encoding="utf-8") as handle:
        state = json.load(handle)

    cycles = state.setdefault("cycles", {})
    migrated = False
    for cycle_state in cycles.values():
        last_result = cycle_state.get("last_result")
        if isinstance(last_result, dict):
            compacted = summarize_cycle_result(last_result)
            if compacted != last_result:
                cycle_state["last_result"] = compacted
                migrated = True

    if migrated:
        save_runtime_health_state(state)
    return state


def save_runtime_health_state(payload: Dict[str, Any]) -> None:
    _ensure_health_dir()
    with HEALTH_FILE.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _summarize_nested_status(name: str, payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"name": name, "status": "unknown"}

    summary: Dict[str, Any] = {
        "name": name,
        "status": payload.get("status", "unknown"),
    }
    if payload.get("message") is not None:
        summary["message"] = payload.get("message")
    if name == "portfolio":
        summary.update(
            {
                "account_mode": payload.get("account_mode"),
                "equity": payload.get("equity"),
                "perp_equity": payload.get("perp_equity"),
                "positions_count": len(payload.get("positions", [])),
                "open_orders_count": len(payload.get("open_orders", [])),
            }
        )
    elif name == "risk":
        summary.update(
            {
                "status": "blocked" if payload.get("can_trade") is False else "success",
                "can_trade": payload.get("can_trade"),
                "drawdown_pct": payload.get("drawdown_pct"),
                "current_equity": payload.get("current_equity"),
            }
        )
    elif name in {"reconcile_pending_entries", "trailing_stop"}:
        summary["actions_taken"] = payload.get("actions_taken", [])
        warnings = payload.get("warnings")
        if warnings:
            summary["warnings"] = warnings
    return summary


def summarize_cycle_result(result: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "status": result.get("status", "unknown"),
    }
    if result.get("message") is not None:
        summary["message"] = result.get("message")

    for key in ("risk", "reconcile_pending_entries", "trailing_stop", "portfolio", "trade"):
        if key in result:
            summary[key] = _summarize_nested_status(key, result[key])

    scanner = result.get("scanner")
    if isinstance(scanner, list):
        summary["scanner"] = {
            "count": len(scanner),
            "top_status": scanner[0].get("status") if scanner and isinstance(scanner[0], dict) else None,
            "top_coin": scanner[0].get("coin") if scanner and isinstance(scanner[0], dict) else None,
            "top_score": scanner[0].get("score") if scanner and isinstance(scanner[0], dict) else None,
            "top_reason": scanner[0].get("reason") if scanner and isinstance(scanner[0], dict) else None,
        }

    selected = result.get("selected_opportunity")
    if isinstance(selected, dict):
        summary["selected_opportunity"] = {
            "coin": selected.get("coin"),
            "suggested_side": selected.get("suggested_side"),
            "score": selected.get("score"),
            "entry_ready": selected.get("entry_ready"),
        }

    return summary


def record_cycle_result(cycle_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    state = load_runtime_health_state()
    cycles = state.setdefault("cycles", {})
    cycle_state = cycles.setdefault(cycle_name, {})
    status = str(result.get("status", "unknown"))
    now = time.time()

    cycle_state["last_status"] = status
    cycle_state["last_updated_at"] = now
    cycle_state["last_result"] = summarize_cycle_result(result)

    if status in FAILURE_STATUSES:
        cycle_state["consecutive_failures"] = int(cycle_state.get("consecutive_failures", 0)) + 1
        cycle_state["last_error"] = result.get("message") or result.get("trade", {}).get("message")
    elif status in HEALTHY_STATUSES:
        cycle_state["consecutive_failures"] = 0
        cycle_state["last_success_at"] = now
        cycle_state["last_error"] = None

    save_runtime_health_state(state)
    return state


def get_runtime_health(
    heartbeat_max_age_seconds: int = 480,
    hunt_max_age_seconds: int = 1_800,
    max_consecutive_failures: int = 2,
) -> Dict[str, Any]:
    state = load_runtime_health_state()
    cycles = state.setdefault("cycles", {})
    now = time.time()
    alerts = []

    cycle_thresholds = {
        "heartbeat": heartbeat_max_age_seconds,
        "hunt": hunt_max_age_seconds,
    }

    for cycle_name, max_age_seconds in cycle_thresholds.items():
        cycle_state = cycles.setdefault(cycle_name, {})
        last_updated_at = cycle_state.get("last_updated_at")
        consecutive_failures = int(cycle_state.get("consecutive_failures", 0) or 0)

        if not last_updated_at:
            alerts.append(f"{cycle_name}_never_ran")
            continue

        age_seconds = now - float(last_updated_at)
        cycle_state["age_seconds"] = round(age_seconds, 2)

        if age_seconds > max_age_seconds:
            alerts.append(f"{cycle_name}_stale")
        if consecutive_failures >= max_consecutive_failures:
            alerts.append(f"{cycle_name}_consecutive_failures")

    return {
        "healthy": not alerts,
        "alerts": alerts,
        "cycles": cycles,
    }
