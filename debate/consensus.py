from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Sequence

from debate.models import DebateDecision, DebateVote


def determine_consensus(votes: Sequence[DebateVote]) -> List[DebateDecision]:
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {
        "weight": 0.0,
        "confidence_sum": 0.0,
        "leverage_sum": 0.0,
        "position_pct_sum": 0.0,
        "risk_pct_sum": 0.0,
        "reward_pct_sum": 0.0,
        "account_risk_pct_sum": 0.0,
        "count": 0,
        "reasons": [],
        "score_sum": 0.0,
        "entry_ready_votes": 0,
    }))

    for vote in votes:
        for decision in vote.decisions:
            action_key = decision.action.upper()
            bucket = grouped[decision.symbol][action_key]
            weight = max(decision.confidence, 1) / 100.0
            bucket["weight"] += weight
            bucket["confidence_sum"] += decision.confidence
            bucket["leverage_sum"] += decision.leverage
            bucket["position_pct_sum"] += decision.position_pct
            bucket["risk_pct_sum"] += decision.risk_pct
            bucket["reward_pct_sum"] += decision.reward_pct
            bucket["account_risk_pct_sum"] += float(decision.account_risk_pct or 0.0)
            bucket["count"] += 1
            bucket["score_sum"] += float(decision.score or 0.0)
            bucket["entry_ready_votes"] += 1 if decision.entry_ready else 0
            if decision.reasoning:
                bucket["reasons"].append(decision.reasoning)

    consensus: List[DebateDecision] = []
    for symbol, actions in grouped.items():
        winning_action, winning_data = max(actions.items(), key=lambda item: item[1]["weight"])
        count = max(winning_data["count"], 1)
        avg_confidence = int(round(winning_data["confidence_sum"] / count))
        avg_leverage = max(1, int(round(winning_data["leverage_sum"] / count)))
        avg_position_pct = max(0.05, min(1.0, winning_data["position_pct_sum"] / count))
        avg_risk_pct = max(0.1, winning_data["risk_pct_sum"] / count)
        avg_reward_pct = max(avg_risk_pct, winning_data["reward_pct_sum"] / count)
        avg_account_risk_pct = max(0.1, winning_data["account_risk_pct_sum"] / count)
        entry_ready = winning_data["entry_ready_votes"] >= max(1, (count + 1) // 2)
        score = round(winning_data["score_sum"] / count, 2) if winning_data["score_sum"] else None
        consensus.append(
            DebateDecision(
                symbol=symbol,
                action=winning_action,
                confidence=avg_confidence,
                leverage=avg_leverage,
                position_pct=round(avg_position_pct, 4),
                risk_pct=round(avg_risk_pct, 4),
                reward_pct=round(avg_reward_pct, 4),
                account_risk_pct=round(avg_account_risk_pct, 4),
                reasoning=" | ".join(winning_data["reasons"][:3]),
                score=score,
                entry_ready=entry_ready,
            )
        )

    return sorted(consensus, key=lambda item: (item.confidence, item.score or 0.0), reverse=True)
