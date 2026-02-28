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
        return json.load(handle)


def save_runtime_health_state(payload: Dict[str, Any]) -> None:
    _ensure_health_dir()
    with HEALTH_FILE.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def record_cycle_result(cycle_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
    state = load_runtime_health_state()
    cycles = state.setdefault("cycles", {})
    cycle_state = cycles.setdefault(cycle_name, {})
    status = str(result.get("status", "unknown"))
    now = time.time()

    cycle_state["last_status"] = status
    cycle_state["last_updated_at"] = now
    cycle_state["last_result"] = result

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
