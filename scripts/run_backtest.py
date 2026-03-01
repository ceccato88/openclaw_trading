#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backtest import run_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local historical backtest for the OpenClaw strategy.")
    parser.add_argument("--coin", required=True, help="Coin symbol, e.g. BTC or ETH")
    parser.add_argument("--start", required=True, help="Start datetime in ISO format, e.g. 2026-01-01T00:00:00+00:00")
    parser.add_argument("--end", required=True, help="End datetime in ISO format, e.g. 2026-02-01T00:00:00+00:00")
    parser.add_argument("--starting-equity", type=float, default=1000.0)
    parser.add_argument("--risk-pct", type=float, default=2.0)
    parser.add_argument("--reward-pct", type=float, default=4.0)
    parser.add_argument("--account-risk-pct", type=float, default=1.0)
    parser.add_argument("--fee-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=2.0)
    parser.add_argument("--pending-ttl-candles", type=int, default=4)
    parser.add_argument("--json-out", help="Optional path to save full JSON result")
    args = parser.parse_args()

    result = run_backtest(
        coin=args.coin,
        start=args.start,
        end=args.end,
        starting_equity=args.starting_equity,
        risk_pct=args.risk_pct,
        reward_pct=args.reward_pct,
        account_risk_pct=args.account_risk_pct,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        pending_ttl_candles=args.pending_ttl_candles,
    )

    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
