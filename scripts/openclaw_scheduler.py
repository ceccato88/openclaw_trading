from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from runtime.cycles import run_heartbeat_cycle, run_hunt_cycle


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


HEARTBEAT_INTERVAL_SECONDS = _env_int("WOLF_HEARTBEAT_INTERVAL_SECONDS", 180)
HUNT_INTERVAL_SECONDS = _env_int("WOLF_HUNT_INTERVAL_SECONDS", 900)
POSITION_USD = _env_float("WOLF_POSITION_USD", 25.0)
LEVERAGE = _env_int("WOLF_LEVERAGE", 10)
RISK_PCT = _env_float("WOLF_RISK_PCT", 2.0)
MIN_VOLUME = _env_float("WOLF_MIN_VOLUME", 5_000_000)
MAX_RESULTS = _env_int("WOLF_MAX_RESULTS", 3)


def _compact_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _run_heartbeat() -> Dict[str, Any]:
    logger.info("A correr heartbeat.")
    result = run_heartbeat_cycle()
    logger.info("Heartbeat concluído | status=%s", result.get("status"))
    if result.get("status") in {"error", "partial_error"}:
        logger.warning("Heartbeat com detalhe: %s", _compact_payload(result))
    return result


def _run_hunt() -> Dict[str, Any]:
    logger.info(
        "A correr hunt | position_usd=%s leverage=%sx risk_pct=%s min_volume=%s max_results=%s",
        POSITION_USD,
        LEVERAGE,
        RISK_PCT,
        MIN_VOLUME,
        MAX_RESULTS,
    )
    result = run_hunt_cycle(
        position_usd=POSITION_USD,
        leverage=LEVERAGE,
        risk_pct=RISK_PCT,
        min_volume=MIN_VOLUME,
        max_results=MAX_RESULTS,
    )
    logger.info("Hunt concluído | status=%s", result.get("status"))
    if result.get("status") in {"error", "partial_error"}:
        logger.warning("Hunt com detalhe: %s", _compact_payload(result))
    return result


def run_heartbeat_once() -> Dict[str, Any]:
    return _run_heartbeat()


def run_hunt_once() -> Dict[str, Any]:
    return _run_hunt()


def _run_loop() -> None:
    logger.info("Scheduler OpenClaw inicializado.")
    logger.info(
        "Configuração | heartbeat=%ss hunt=%ss position_usd=%s leverage=%sx risk_pct=%s",
        HEARTBEAT_INTERVAL_SECONDS,
        HUNT_INTERVAL_SECONDS,
        POSITION_USD,
        LEVERAGE,
        RISK_PCT,
    )

    try:
        _run_heartbeat()
    except Exception:  # noqa: BLE001
        logger.exception("Falha no heartbeat inicial.")

    next_heartbeat_at = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
    next_hunt_at = time.monotonic() + HUNT_INTERVAL_SECONDS

    while True:
        now = time.monotonic()

        if now >= next_heartbeat_at:
            try:
                _run_heartbeat()
            except Exception:  # noqa: BLE001
                logger.exception("Falha no ciclo heartbeat.")
            next_heartbeat_at = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS

        if now >= next_hunt_at:
            try:
                _run_hunt()
            except Exception:  # noqa: BLE001
                logger.exception("Falha no ciclo hunt.")
            next_hunt_at = time.monotonic() + HUNT_INTERVAL_SECONDS

        time.sleep(1.0)


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
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Scheduler interrompido pelo utilizador.")
