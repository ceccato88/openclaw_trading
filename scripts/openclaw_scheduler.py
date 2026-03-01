from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import json
import logging
import os
import signal
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from dotenv import dotenv_values
from runtime.cycles import run_heartbeat_cycle, run_hunt_cycle
from skills.support import acquire_file_lock, release_file_lock


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - OPENCLAW SCHEDULER - %(levelname)s - %(message)s",
)
logger = logging.getLogger("OpenClawScheduler")


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return int(raw_value)


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return float(raw_value)


SCHEDULER_LOCK_NAME = "openclaw-scheduler"
ENV_FILE = BASE_DIR / ".env"
RELOAD_CONFIG_REQUESTED = False


@dataclass
class SchedulerConfig:
    heartbeat_interval_seconds: int = 180
    hunt_interval_seconds: int = 900
    position_usd: float = 25.0
    leverage: int = 10
    risk_pct: float = 2.0
    reward_pct: float = 4.0
    account_risk_pct: float = 1.0
    max_drawdown_pct: float = 10.0
    min_volume: float = 5_000_000
    max_results: int = 3
    max_consecutive_failures: int = 3
    failure_backoff_seconds: int = 60


def _env_or_file_value(name: str) -> str | None:
    env_value = os.getenv(name)
    if env_value is not None:
        return env_value
    if not ENV_FILE.exists():
        return None
    file_values = dotenv_values(ENV_FILE)
    value = file_values.get(name)
    return str(value) if value is not None else None


def _config_int(name: str, default: int) -> int:
    raw_value = _env_or_file_value(name)
    if raw_value is None:
        return default
    return int(raw_value)


def _config_float(name: str, default: float) -> float:
    raw_value = _env_or_file_value(name)
    if raw_value is None:
        return default
    return float(raw_value)


def load_scheduler_config() -> SchedulerConfig:
    return SchedulerConfig(
        heartbeat_interval_seconds=_config_int("WOLF_HEARTBEAT_INTERVAL_SECONDS", 180),
        hunt_interval_seconds=_config_int("WOLF_HUNT_INTERVAL_SECONDS", 900),
        position_usd=_config_float("WOLF_POSITION_USD", 25.0),
        leverage=_config_int("WOLF_LEVERAGE", 10),
        risk_pct=_config_float("WOLF_RISK_PCT", 2.0),
        reward_pct=_config_float("WOLF_REWARD_PCT", 4.0),
        account_risk_pct=_config_float("WOLF_ACCOUNT_RISK_PCT", 1.0),
        max_drawdown_pct=_config_float("WOLF_MAX_DRAWDOWN_PCT", 10.0),
        min_volume=_config_float("WOLF_MIN_VOLUME", 5_000_000),
        max_results=_config_int("WOLF_MAX_RESULTS", 3),
        max_consecutive_failures=_config_int("WOLF_MAX_CONSECUTIVE_FAILURES", 3),
        failure_backoff_seconds=_config_int("WOLF_FAILURE_BACKOFF_SECONDS", 60),
    )


def _handle_sighup(_signum, _frame) -> None:
    global RELOAD_CONFIG_REQUESTED
    RELOAD_CONFIG_REQUESTED = True
    logger.info("SIGHUP recebido. Configuração será recarregada no próximo tick.")


def _compact_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _run_heartbeat() -> Dict[str, Any]:
    logger.info("A correr heartbeat.")
    result = run_heartbeat_cycle()
    logger.info("Heartbeat concluído | status=%s", result.get("status"))
    if result.get("status") in {"error", "partial_error"}:
        logger.warning("Heartbeat com detalhe: %s", _compact_payload(result))
    return result


def _run_hunt(config: SchedulerConfig) -> Dict[str, Any]:
    logger.info(
        "A correr hunt | min_notional_usd=%s leverage=%sx stop_pct=%s reward_pct=%s account_risk_pct=%s max_drawdown_pct=%s min_volume=%s max_results=%s",
        config.position_usd,
        config.leverage,
        config.risk_pct,
        config.reward_pct,
        config.account_risk_pct,
        config.max_drawdown_pct,
        config.min_volume,
        config.max_results,
    )
    result = run_hunt_cycle(
        position_usd=config.position_usd,
        leverage=config.leverage,
        risk_pct=config.risk_pct,
        reward_pct=config.reward_pct,
        account_risk_pct=config.account_risk_pct,
        min_volume=config.min_volume,
        max_results=config.max_results,
    )
    logger.info("Hunt concluído | status=%s", result.get("status"))
    if result.get("status") in {"error", "partial_error"}:
        logger.warning("Hunt com detalhe: %s", _compact_payload(result))
    return result


def _is_failure_result(result: Dict[str, Any]) -> bool:
    return str(result.get("status", "unknown")) in {"error", "partial_error"}


def _record_cycle_outcome(
    cycle_name: str,
    failed: bool,
    consecutive_failures: Dict[str, int],
    paused_until: Dict[str, float],
    config: SchedulerConfig,
) -> None:
    if failed:
        consecutive_failures[cycle_name] = consecutive_failures.get(cycle_name, 0) + 1
        if consecutive_failures[cycle_name] >= config.max_consecutive_failures:
            paused_until[cycle_name] = time.monotonic() + config.failure_backoff_seconds
            logger.warning(
                "Circuit breaker ativo para %s por %ss após %s falhas consecutivas.",
                cycle_name,
                config.failure_backoff_seconds,
                consecutive_failures[cycle_name],
            )
            consecutive_failures[cycle_name] = 0
        return

    consecutive_failures[cycle_name] = 0
    paused_until[cycle_name] = 0.0


def _submit_heartbeat(executor: ThreadPoolExecutor) -> Future:
    return executor.submit(_run_heartbeat)


def _submit_hunt(executor: ThreadPoolExecutor, config: SchedulerConfig) -> Future:
    return executor.submit(_run_hunt, config)


def run_heartbeat_once() -> Dict[str, Any]:
    return _run_heartbeat()


def run_hunt_once() -> Dict[str, Any]:
    return _run_hunt(load_scheduler_config())


def _run_loop() -> None:
    global RELOAD_CONFIG_REQUESTED
    config = load_scheduler_config()
    logger.info("Scheduler OpenClaw inicializado.")
    logger.info(
        "Configuração | heartbeat=%ss hunt=%ss min_notional_usd=%s leverage=%sx stop_pct=%s reward_pct=%s account_risk_pct=%s max_drawdown_pct=%s",
        config.heartbeat_interval_seconds,
        config.hunt_interval_seconds,
        config.position_usd,
        config.leverage,
        config.risk_pct,
        config.reward_pct,
        config.account_risk_pct,
        config.max_drawdown_pct,
    )

    scheduler_lock = acquire_file_lock(SCHEDULER_LOCK_NAME)
    if scheduler_lock is None:
        raise RuntimeError("Já existe outra instância do scheduler OpenClaw em execução.")

    try:
        try:
            heartbeat_result = _run_heartbeat()
            heartbeat_failed = _is_failure_result(heartbeat_result)
        except Exception:  # noqa: BLE001
            logger.exception("Falha no heartbeat inicial.")
            heartbeat_failed = True

        next_heartbeat_at = time.monotonic() + config.heartbeat_interval_seconds
        next_hunt_at = time.monotonic() + config.hunt_interval_seconds
        consecutive_failures = {"heartbeat": 0, "hunt": 0}
        paused_until = {"heartbeat": 0.0, "hunt": 0.0}
        running_futures: Dict[str, Future | None] = {"heartbeat": None, "hunt": None}
        _record_cycle_outcome("heartbeat", heartbeat_failed, consecutive_failures, paused_until, config)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="openclaw-scheduler") as executor:
            while True:
                now = time.monotonic()

                if RELOAD_CONFIG_REQUESTED:
                    previous = asdict(config)
                    config = load_scheduler_config()
                    RELOAD_CONFIG_REQUESTED = False
                    logger.info("Configuração recarregada: %s", _compact_payload(asdict(config)))
                    if config.heartbeat_interval_seconds != previous["heartbeat_interval_seconds"]:
                        next_heartbeat_at = now + config.heartbeat_interval_seconds
                    if config.hunt_interval_seconds != previous["hunt_interval_seconds"]:
                        next_hunt_at = now + config.hunt_interval_seconds

                for cycle_name in ("heartbeat", "hunt"):
                    future = running_futures[cycle_name]
                    if future is None or not future.done():
                        continue
                    try:
                        cycle_result = future.result()
                        failed = _is_failure_result(cycle_result)
                    except Exception:  # noqa: BLE001
                        logger.exception("Falha no worker do ciclo %s.", cycle_name)
                        failed = True
                    _record_cycle_outcome(cycle_name, failed, consecutive_failures, paused_until, config)
                    running_futures[cycle_name] = None

                if now >= next_heartbeat_at and now >= paused_until["heartbeat"]:
                    if running_futures["heartbeat"] is None:
                        running_futures["heartbeat"] = _submit_heartbeat(executor)
                    else:
                        logger.warning("Heartbeat anterior ainda em execução. A saltar este tick.")
                    next_heartbeat_at = time.monotonic() + config.heartbeat_interval_seconds

                if now >= next_hunt_at and now >= paused_until["hunt"]:
                    if running_futures["hunt"] is None:
                        running_futures["hunt"] = _submit_hunt(executor, config)
                    else:
                        logger.warning("Hunt anterior ainda em execução. A saltar este tick.")
                    next_hunt_at = time.monotonic() + config.hunt_interval_seconds

                time.sleep(1.0)
    finally:
        release_file_lock(scheduler_lock)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scheduler local do OpenClaw para o bot da Hyperliquid.")
    parser.add_argument("--heartbeat-once", action="store_true", help="Executa um ciclo heartbeat e termina.")
    parser.add_argument("--hunt-once", action="store_true", help="Executa um ciclo hunt e termina.")
    args = parser.parse_args()

    if args.heartbeat_once:
        print(json.dumps(run_heartbeat_once(), ensure_ascii=False, indent=2, default=str))
        return

    if args.hunt_once:
        print(json.dumps(run_hunt_once(), ensure_ascii=False, indent=2, default=str))
        return

    _run_loop()


if __name__ == "__main__":
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _handle_sighup)
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Scheduler interrompido pelo utilizador.")
