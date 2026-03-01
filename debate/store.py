from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from debate.models import DebateSession
from skills.support import atomic_write_json, ensure_state_dirs


BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state" / "debates"
SESSIONS_DIR = STATE_DIR / "sessions"


def ensure_debate_dirs() -> None:
    ensure_state_dirs()
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def session_path(session_id: str) -> Path:
    ensure_debate_dirs()
    return SESSIONS_DIR / f"{session_id}.json"


class DebateStore:
    def __init__(self) -> None:
        ensure_debate_dirs()

    def save_session(self, session: DebateSession) -> Path:
        session.updated_at = session.updated_at or session.created_at
        return atomic_write_json(session_path(session.id), session.to_dict())

    def create_session(self, session: DebateSession) -> Path:
        return self.save_session(session)

    def get_session(self, session_id: str) -> Optional[DebateSession]:
        path = session_path(session_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return DebateSession.from_dict(payload)

    def list_sessions(self) -> List[DebateSession]:
        ensure_debate_dirs()
        sessions: List[DebateSession] = []
        for path in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            sessions.append(DebateSession.from_dict(payload))
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions
