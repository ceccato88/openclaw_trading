from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass
class DebateDecision:
    symbol: str
    action: str
    confidence: int
    leverage: int
    position_pct: float
    risk_pct: float
    reward_pct: float
    reasoning: str
    account_risk_pct: Optional[float] = None
    score: Optional[float] = None
    entry_ready: Optional[bool] = None
    executed: bool = False
    executed_at: Optional[str] = None
    execution_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DebateDecision":
        return cls(**payload)


@dataclass
class DebateParticipant:
    personality: str
    mode: str = "heuristic"
    model_id: Optional[str] = None
    id: str = field(default_factory=lambda: new_id("participant"))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DebateParticipant":
        return cls(**payload)


@dataclass
class DebateMessage:
    session_id: str
    participant_id: str
    personality: str
    round: int
    content: str
    decisions: List[DebateDecision]
    created_at: str = field(default_factory=utc_now_iso)
    id: str = field(default_factory=lambda: new_id("message"))

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["decisions"] = [decision.to_dict() for decision in self.decisions]
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DebateMessage":
        cloned = dict(payload)
        cloned["decisions"] = [DebateDecision.from_dict(item) for item in cloned.get("decisions", [])]
        return cls(**cloned)


@dataclass
class DebateVote:
    session_id: str
    participant_id: str
    personality: str
    reasoning: str
    decisions: List[DebateDecision]
    created_at: str = field(default_factory=utc_now_iso)
    id: str = field(default_factory=lambda: new_id("vote"))

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["decisions"] = [decision.to_dict() for decision in self.decisions]
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DebateVote":
        cloned = dict(payload)
        cloned["decisions"] = [DebateDecision.from_dict(item) for item in cloned.get("decisions", [])]
        return cls(**cloned)


@dataclass
class DebateSession:
    name: str
    symbols: List[str]
    participants: List[DebateParticipant]
    max_rounds: int = 3
    auto_execute: bool = False
    id: str = field(default_factory=lambda: new_id("debate"))
    status: str = "pending"
    current_round: int = 0
    messages: List[DebateMessage] = field(default_factory=list)
    votes: List[DebateVote] = field(default_factory=list)
    final_decisions: List[DebateDecision] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["participants"] = [participant.to_dict() for participant in self.participants]
        payload["messages"] = [message.to_dict() for message in self.messages]
        payload["votes"] = [vote.to_dict() for vote in self.votes]
        payload["final_decisions"] = [decision.to_dict() for decision in self.final_decisions]
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "DebateSession":
        cloned = dict(payload)
        cloned["participants"] = [DebateParticipant.from_dict(item) for item in cloned.get("participants", [])]
        cloned["messages"] = [DebateMessage.from_dict(item) for item in cloned.get("messages", [])]
        cloned["votes"] = [DebateVote.from_dict(item) for item in cloned.get("votes", [])]
        cloned["final_decisions"] = [DebateDecision.from_dict(item) for item in cloned.get("final_decisions", [])]
        return cls(**cloned)
