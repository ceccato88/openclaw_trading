#!/usr/bin/env python3
"""Local Debate Arena CLI for OpenClaw."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from debate.engine import DebateEngine
from project_env import load_project_env


load_project_env(BASE_DIR)


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local Debate Arena sessions for OpenClaw.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new debate session.")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--symbols", nargs="*", default=[])
    create_parser.add_argument("--participants", nargs="+", default=["bull", "bear", "analyst", "risk_manager"])
    create_parser.add_argument("--max-rounds", type=int, default=3)
    create_parser.add_argument("--auto-execute", action="store_true")
    create_parser.add_argument("--model-id", default=None)

    run_parser = subparsers.add_parser("run", help="Run an existing debate session.")
    run_parser.add_argument("--session-id", required=True)
    run_parser.add_argument("--execute", action="store_true")

    show_parser = subparsers.add_parser("show", help="Show a debate session.")
    show_parser.add_argument("--session-id", required=True)

    subparsers.add_parser("list", help="List debate sessions.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    engine = DebateEngine()

    if args.command == "create":
        session = engine.create_session(
            name=args.name,
            symbols=args.symbols,
            personalities=args.participants,
            max_rounds=args.max_rounds,
            auto_execute=args.auto_execute,
            mode="llm",
            model_id=args.model_id,
        )
        print(_json(session.to_dict()))
        return

    if args.command == "run":
        session = engine.run_session(args.session_id, execute=args.execute)
        print(_json(session.to_dict()))
        return

    if args.command == "show":
        session = engine.get_session(args.session_id)
        if session is None:
            raise SystemExit(f"Sessão não encontrada: {args.session_id}")
        print(_json(session.to_dict()))
        return

    if args.command == "list":
        sessions = [session.to_dict() for session in engine.list_sessions()]
        print(_json(sessions))
        return


if __name__ == "__main__":
    main()
