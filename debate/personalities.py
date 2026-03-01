from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class PersonalitySpec:
    key: str
    emoji: str
    label: str
    description: str
    base_side: str


PERSONALITIES: Dict[str, PersonalitySpec] = {
    "bull": PersonalitySpec(
        key="bull",
        emoji="🐂",
        label="Bull",
        description="Procura continuação de tendência, momentum e longs com convicção.",
        base_side="LONG",
    ),
    "bear": PersonalitySpec(
        key="bear",
        emoji="🐻",
        label="Bear",
        description="Procura fraqueza, reversão e shorts quando o risco compensa.",
        base_side="SHORT",
    ),
    "analyst": PersonalitySpec(
        key="analyst",
        emoji="📊",
        label="Analyst",
        description="Avalia dados de forma neutra e privilegia o melhor score ajustado ao contexto.",
        base_side="NEUTRAL",
    ),
    "contrarian": PersonalitySpec(
        key="contrarian",
        emoji="🔄",
        label="Contrarian",
        description="Questiona o consenso e procura excesso de confiança ou assimetrias ignoradas.",
        base_side="OPPOSITE",
    ),
    "risk_manager": PersonalitySpec(
        key="risk_manager",
        emoji="🛡️",
        label="Risk Manager",
        description="Prioriza preservação de capital, drawdown, heat de portfólio e tamanho de posição.",
        base_side="RISK",
    ),
}


def get_personality_spec(personality: str) -> PersonalitySpec:
    normalized = personality.strip().lower()
    if normalized not in PERSONALITIES:
        raise ValueError(f"Personalidade não suportada: {personality}")
    return PERSONALITIES[normalized]
