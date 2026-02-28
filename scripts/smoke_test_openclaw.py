#!/usr/bin/env python3
"""Run a real end-to-end smoke test against Hyperliquid testnet without Agno."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from hl_client import get_exchange
from project_env import load_project_env
from runtime.cycles import run_heartbeat_cycle, run_hunt_cycle
from skills.close_trade import close_position
from skills.dsl import run_dynamic_stop_loss
from skills.entry_manager import reconcile_pending_entries
from skills.portfolio import close_all_positions, get_portfolio_status
from skills.risk_manager import check_daily_drawdown
from skills.scanner import run_opportunity_scanner
from skills.signals import get_market_regime
from skills.support import (
    MIN_PERP_TRADE_NOTIONAL_USD,
    cancel_orders_for_coin,
    compute_protection_prices,
    delete_pending_entry_state,
    delete_trade_state,
    ensure_exchange_ok,
    extract_fill_details,
    get_asset_context,
    list_pending_entry_states,
    list_trade_states,
    place_trade_protection,
    save_trade_state,
    update_exchange_leverage,
)
from skills.wolf_strategy import execute_wolf_strategy_trade

load_project_env(BASE_DIR)
SMOKE_NOTIONAL_BUFFER_USD = 2.0


def _to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _assert_clean_account(snapshot: Dict[str, Any]) -> None:
    _require(snapshot.get("status") == "success", f"Snapshot inválido: {_json(snapshot)}")
    _require(not snapshot.get("positions"), "A smoke test exige conta sem posições abertas.")
    _require(not snapshot.get("open_orders"), "A smoke test exige conta sem ordens abertas.")


def _assert_minimum_tradeable_balance(snapshot: Dict[str, Any], required_notional_usd: float, slippage_buffer_pct: float = 1.0) -> None:
    _require(snapshot.get("status") == "success", f"Snapshot inválido para verificação de saldo: {_json(snapshot)}")
    available = float(snapshot.get("spot_usdc_available") or snapshot.get("withdrawable") or 0.0)
    required_balance = required_notional_usd * (1 + (slippage_buffer_pct / 100))
    _require(
        available >= required_balance,
        f"Saldo insuficiente para smoke test. Disponível=${available:.2f} | Necessário>={required_balance:.2f}",
    )


def _record_cleanup_and_assert(summary: Dict[str, Any], label: str, allow_dirty_start: bool) -> None:
    cleanup_key = f"cleanup_after_{label}"
    snapshot_key = f"snapshot_after_{label}"
    summary[cleanup_key] = _cleanup_local_state()
    summary[snapshot_key] = get_portfolio_status()
    if not allow_dirty_start:
        _assert_clean_account(summary[snapshot_key])


def _run_scheduler_once(flag: str, env_overrides: Dict[str, str]) -> Dict[str, Any]:
    env = os.environ.copy()
    env.update(env_overrides)
    completed = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts" / "openclaw_scheduler.py"), flag],
        cwd=BASE_DIR,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = completed.stdout.strip()
    payload = json.loads(stdout) if stdout else None
    return {
        "flag": flag,
        "returncode": completed.returncode,
        "payload": payload,
        "stderr": completed.stderr.strip(),
    }


def _cleanup_local_state() -> Dict[str, Any]:
    cleanup: Dict[str, Any] = {
        "pending_entries": [],
        "close_all_positions": None,
        "deleted_trade_states": [],
    }

    for pending in list_pending_entry_states():
        coin = pending["coin"]
        cancel_result: Any = "not_attempted"
        try:
            cancel_result = cancel_orders_for_coin(coin)
        except Exception as exc:  # noqa: BLE001
            cancel_result = f"cancel_failed: {exc}"
        delete_pending_entry_state(coin)
        cleanup["pending_entries"].append(
            {
                "coin": coin,
                "entry_oid": pending.get("entry_oid"),
                "cancel_result": cancel_result,
            }
        )

    try:
        cleanup["close_all_positions"] = close_all_positions()
    except Exception as exc:  # noqa: BLE001
        cleanup["close_all_positions"] = {"status": "error", "message": str(exc)}

    for trade in list_trade_states():
        coin = trade["coin"]
        delete_trade_state(coin)
        cleanup["deleted_trade_states"].append(coin)

    return cleanup


def _run_direct_market_trade(coin: str, usdt_size: float, leverage: int, risk_pct: float, reward_pct: float) -> Dict[str, Any]:
    exchange = get_exchange()
    regime = get_market_regime()
    if regime["regime"] == "BULL":
        side = "LONG"
    elif regime["regime"] == "BEAR":
        side = "SHORT"
    else:
        # No smoke test, CHOP não deve impedir a validação técnica do fluxo de execução.
        side = "LONG" if float(regime["current_price"]) >= float(regime["ema_50"]) else "SHORT"
    is_buy = side == "LONG"
    current_price, asset_info, _ = get_asset_context(coin)
    effective_usdt_size = max(usdt_size, MIN_PERP_TRADE_NOTIONAL_USD + SMOKE_NOTIONAL_BUFFER_USD)
    pre_trade_snapshot = get_portfolio_status()
    _assert_clean_account(pre_trade_snapshot)
    _assert_minimum_tradeable_balance(pre_trade_snapshot, effective_usdt_size)
    size_in_coins = round(effective_usdt_size / current_price, int(asset_info["szDecimals"]))
    _require(size_in_coins > 0, f"Tamanho arredondado ficou zero para {coin}.")

    is_cross = not bool(asset_info.get("onlyIsolated", False))
    leverage_response = update_exchange_leverage(leverage, coin, is_cross=is_cross)
    ensure_exchange_ok(leverage_response, f"ajuste de alavancagem smoke {coin}")

    fill = extract_fill_details(
        exchange.market_open(coin, is_buy, size_in_coins, slippage=0.01),
        f"market_open smoke {coin}",
    )
    protection_prices = compute_protection_prices(
        coin=coin,
        entry_price=fill["avg_px"],
        risk_pct=risk_pct,
        reward_pct=reward_pct,
        is_long=is_buy,
    )
    protection = place_trade_protection(
        coin,
        is_buy,
        fill["size"],
        protection_prices["sl"],
        protection_prices["tp"],
    )
    save_trade_state(
        coin,
        {
            "coin": coin,
            "entry": fill["avg_px"],
            "filled_size": fill["size"],
            "tp": protection["tp"]["price"],
            "sl": protection["sl"]["price"],
            "risk_pct": risk_pct,
            "reward_pct": reward_pct,
            "breakeven_done": False,
            "management_stage": "initial_risk",
        },
    )

    snapshot_after_open = get_portfolio_status()
    trailing_result = run_dynamic_stop_loss()
    reconcile_result = reconcile_pending_entries()
    close_result = close_position(coin)
    final_snapshot = get_portfolio_status()

    return {
        "regime": regime,
        "side": side,
        "requested_usdt_size": usdt_size,
        "effective_usdt_size": effective_usdt_size,
        "reward_pct": reward_pct,
        "pre_trade_snapshot": pre_trade_snapshot,
        "leverage_response": leverage_response,
        "fill": fill,
        "protection": protection,
        "snapshot_after_open": snapshot_after_open,
        "trailing_result": trailing_result,
        "reconcile_result": reconcile_result,
        "close_result": close_result,
        "final_snapshot": final_snapshot,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Real smoke test for OpenClaw Hyperliquid runtime on testnet.")
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--usdt-size", type=float, default=10.0)
    parser.add_argument("--leverage", type=int, default=10)
    parser.add_argument("--risk-pct", type=float, default=2.0)
    parser.add_argument("--reward-pct", type=float, default=4.0)
    parser.add_argument("--account-risk-pct", type=float, default=1.0)
    parser.add_argument("--allow-dirty-start", action="store_true")
    args = parser.parse_args()

    _require(_to_bool(os.getenv("HYPERLIQUID_TESTNET"), default=False), "Defina HYPERLIQUID_TESTNET=true.")

    summary: Dict[str, Any] = {
        "coin": args.coin,
        "usdt_size": args.usdt_size,
        "leverage": args.leverage,
        "risk_pct": args.risk_pct,
        "reward_pct": args.reward_pct,
        "account_risk_pct": args.account_risk_pct,
    }

    scheduler_env = {
        "WOLF_POSITION_USD": str(args.usdt_size),
        "WOLF_LEVERAGE": str(args.leverage),
        "WOLF_RISK_PCT": str(args.risk_pct),
        "WOLF_ACCOUNT_RISK_PCT": str(args.account_risk_pct),
        "WOLF_MIN_VOLUME": os.getenv("WOLF_MIN_VOLUME", "1000000"),
        "WOLF_MAX_RESULTS": os.getenv("WOLF_MAX_RESULTS", "3"),
    }

    try:
        initial_snapshot = get_portfolio_status()
        summary["initial_snapshot"] = initial_snapshot
        if not args.allow_dirty_start:
            _assert_clean_account(initial_snapshot)

        summary["risk"] = check_daily_drawdown()
        summary["scanner"] = run_opportunity_scanner(min_volume=1_000_000, max_results=5, candidate_pool_size=20)
        summary["heartbeat_direct"] = run_heartbeat_cycle()
        summary["scheduler_heartbeat_once"] = _run_scheduler_once("--heartbeat-once", scheduler_env)
        summary["scheduler_hunt_once"] = _run_scheduler_once("--hunt-once", scheduler_env)
        _record_cleanup_and_assert(summary, "scheduler", args.allow_dirty_start)

        summary["hunt_direct"] = run_hunt_cycle(
            position_usd=args.usdt_size,
            leverage=args.leverage,
            risk_pct=args.risk_pct,
            account_risk_pct=args.account_risk_pct,
            min_volume=1_000_000,
            max_results=3,
        )
        _record_cleanup_and_assert(summary, "hunt", args.allow_dirty_start)

        regime = get_market_regime()
        summary["strategy_attempt"] = execute_wolf_strategy_trade(
            coin=args.coin,
            side="SHORT" if regime["regime"] == "BEAR" else "LONG",
            usdt_size=args.usdt_size,
            leverage=args.leverage,
            risk_pct=args.risk_pct,
            account_risk_pct=args.account_risk_pct,
        )
        summary["reconcile_after_strategy"] = reconcile_pending_entries()
        _record_cleanup_and_assert(summary, "strategy", args.allow_dirty_start)

        summary["direct_market_trade"] = _run_direct_market_trade(
            coin=args.coin,
            usdt_size=args.usdt_size,
            leverage=args.leverage,
            risk_pct=args.risk_pct,
            reward_pct=args.reward_pct,
        )

        summary["final_cleanup"] = _cleanup_local_state()
        summary["final_snapshot"] = get_portfolio_status()
        if not args.allow_dirty_start:
            _assert_clean_account(summary["final_snapshot"])

        print(_json(summary))
    except Exception as exc:  # noqa: BLE001
        summary["final_cleanup_on_error"] = _cleanup_local_state()
        summary["final_snapshot_on_error"] = get_portfolio_status()
        summary["error"] = str(exc)
        print(_json(summary))
        raise


if __name__ == "__main__":
    main()
