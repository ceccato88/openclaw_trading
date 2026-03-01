from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

import requests

from debate.consensus import determine_consensus
from debate.context_builder import build_market_context
from debate.models import DebateDecision, DebateMessage, DebateParticipant, DebateSession, DebateVote, utc_now_iso
from debate.personalities import get_personality_spec
from debate.store import DebateStore
from hl_client import logger
from skills.portfolio import get_portfolio_status
from skills.wolf_strategy import execute_wolf_strategy_trade


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_DEBATE_MODEL = "openai/gpt-4o-mini"


def _env_to_bool(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


class DebateEngine:
    def __init__(self, store: Optional[DebateStore] = None) -> None:
        self.store = store or DebateStore()

    def create_session(
        self,
        name: str,
        symbols: Sequence[str] | None,
        personalities: Sequence[str],
        max_rounds: int = 3,
        auto_execute: bool = False,
        mode: str = "llm",
        model_id: Optional[str] = None,
    ) -> DebateSession:
        normalized_mode = mode.strip().lower()
        if normalized_mode != "llm":
            raise ValueError("O Debate Arena local suporta apenas mode='llm'.")
        participants = [
            DebateParticipant(
                personality=personality,
                mode=normalized_mode,
                model_id=model_id or os.getenv("DEBATE_MODEL_ID") or os.getenv("OPENROUTER_MODEL_ID") or DEFAULT_DEBATE_MODEL,
            )
            for personality in personalities
        ]
        session = DebateSession(
            name=name,
            symbols=[symbol.upper() for symbol in (symbols or [])],
            participants=participants,
            max_rounds=max(1, min(max_rounds, 5)),
            auto_execute=auto_execute,
            metadata={"mode": normalized_mode},
        )
        self.store.create_session(session)
        return session

    def get_session(self, session_id: str) -> Optional[DebateSession]:
        return self.store.get_session(session_id)

    def list_sessions(self) -> List[DebateSession]:
        return self.store.list_sessions()

    def run_session(self, session_id: str, execute: bool | None = None) -> DebateSession:
        session = self._require_session(session_id)
        try:
            session.messages = []
            session.votes = []
            session.final_decisions = []
            session.current_round = 0
            session.status = "running"
            session.updated_at = utc_now_iso()
            session.metadata.pop("last_error", None)
            self.store.save_session(session)

            context = build_market_context(symbols=session.symbols)
            session.metadata["market_context"] = context
            session.metadata["account_snapshot"] = get_portfolio_status()
            if context["market_regime"].get("regime") == "CHOP" and not _env_to_bool("DEBATE_ALLOW_CHOP", default=False):
                session.status = "completed"
                session.updated_at = utc_now_iso()
                session.final_decisions = [
                    DebateDecision(
                        symbol=item["symbol"],
                        action="WAIT",
                        confidence=0,
                        leverage=1,
                        position_pct=0.0,
                        risk_pct=0.0,
                        reward_pct=0.0,
                        account_risk_pct=0.0,
                        reasoning="Debate encerrado cedo: regime de mercado CHOP e DEBATE_ALLOW_CHOP=false.",
                        score=float(item.get("score", {}).get("score") or 0.0),
                        entry_ready=False,
                    )
                    for item in context.get("symbols", [])
                ]
                session.metadata["early_exit_reason"] = "market_regime_chop"
                self.store.save_session(session)
                logger.info("Debate %s | encerrado cedo por CHOP.", session.id)
                return session

            for round_number in range(1, session.max_rounds + 1):
                session.current_round = round_number
                logger.info("Debate %s | round %s/%s iniciado.", session.id, round_number, session.max_rounds)
                for participant in session.participants:
                    logger.info(
                        "Debate %s | round %s/%s | participante=%s | início",
                        session.id,
                        round_number,
                        session.max_rounds,
                        participant.personality,
                    )
                    message = self._run_participant_round(session, participant, context, round_number)
                    session.messages.append(message)
                    session.updated_at = utc_now_iso()
                    self.store.save_session(session)
                    logger.info(
                        "Debate %s | round %s/%s | participante=%s | fim | decisões=%s",
                        session.id,
                        round_number,
                        session.max_rounds,
                        participant.personality,
                        len(message.decisions),
                    )

            session.status = "voting"
            session.updated_at = utc_now_iso()
            self.store.save_session(session)
            logger.info("Debate %s | fase de votação iniciada.", session.id)

            votes = []
            for participant in session.participants:
                logger.info("Debate %s | votação | participante=%s | início", session.id, participant.personality)
                vote = self._collect_vote(session, participant, context)
                votes.append(vote)
                logger.info(
                    "Debate %s | votação | participante=%s | fim | decisões=%s",
                    session.id,
                    participant.personality,
                    len(vote.decisions),
                )
            session.votes.extend(votes)
            session.final_decisions = determine_consensus(votes)
            session.status = "completed"
            session.updated_at = utc_now_iso()
            logger.info("Debate %s | consenso calculado | decisões=%s", session.id, len(session.final_decisions))

            should_execute = session.auto_execute if execute is None else execute
            if should_execute:
                self._execute_consensus(session)

            self.store.save_session(session)
            return session
        except Exception as exc:  # noqa: BLE001
            session.status = "failed"
            session.updated_at = utc_now_iso()
            session.metadata["last_error"] = str(exc)
            self.store.save_session(session)
            raise

    def _require_session(self, session_id: str) -> DebateSession:
        session = self.store.get_session(session_id)
        if session is None:
            raise RuntimeError(f"Sessão de debate não encontrada: {session_id}")
        return session

    def _run_participant_round(
        self,
        session: DebateSession,
        participant: DebateParticipant,
        context: Dict[str, Any],
        round_number: int,
    ) -> DebateMessage:
        decisions = self._generate_decisions(participant, context)
        content = self._build_message_content(participant, decisions, context, round_number)
        return DebateMessage(
            session_id=session.id,
            participant_id=participant.id,
            personality=participant.personality,
            round=round_number,
            content=content,
            decisions=decisions,
        )

    def _collect_vote(
        self,
        session: DebateSession,
        participant: DebateParticipant,
        context: Dict[str, Any],
    ) -> DebateVote:
        decisions = self._generate_decisions(participant, context)
        reasoning = self._build_vote_reasoning(participant, decisions)
        return DebateVote(
            session_id=session.id,
            participant_id=participant.id,
            personality=participant.personality,
            reasoning=reasoning,
            decisions=decisions,
        )

    def _generate_decisions(self, participant: DebateParticipant, context: Dict[str, Any]) -> List[DebateDecision]:
        if participant.mode != "llm":
            raise RuntimeError(f"Modo de participante não suportado: {participant.mode}")
        decisions = self._generate_decisions_llm(participant, context)
        if not decisions:
            raise RuntimeError(f"O participante {participant.personality} não devolveu decisões válidas.")
        return decisions

    def _generate_decisions_llm(self, participant: DebateParticipant, context: Dict[str, Any]) -> List[DebateDecision]:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY não configurada.")

        spec = get_personality_spec(participant.personality)
        prompt = {
            "personality": spec.label,
            "description": spec.description,
            "market_regime": context["market_regime"],
            "risk": context["risk"],
            "account": {
                "equity": context["account"].get("equity"),
                "positions": len(context["account"].get("positions", [])),
                "open_orders": len(context["account"].get("open_orders", [])),
            },
            "symbols": [
                {
                    "symbol": item["symbol"],
                    "market_price": item["market_price"],
                    "entry_ready": item["entry_ready"],
                    "entry_setup": item["entry_setup"],
                    "higher_timeframe_context": item["higher_timeframe_context"],
                    "score": item["score"],
                }
                for item in context["symbols"]
            ],
        }

        response = requests.post(
            OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://local.openclaw",
                "X-Title": "OpenClaw Debate Arena",
            },
            json={
                "model": participant.model_id or DEFAULT_DEBATE_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Você é um debatedor de trading. Responda apenas JSON válido com a chave 'decisions'. "
                            "Devolva exatamente uma decisão por símbolo recebido, mesmo que seja HOLD. "
                            "Ação permitida: LONG, SHORT, HOLD, WAIT, OPEN_LONG, OPEN_SHORT. "
                            "Cada decisão deve conter: symbol, action, confidence, leverage, position_pct, risk_pct, reward_pct, account_risk_pct, reasoning."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(prompt, ensure_ascii=False),
                    },
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        parsed = self._parse_llm_json_content(content)
        decisions = self._extract_decisions_payload(parsed)
        normalized = [self._normalize_llm_decision(item, context) for item in decisions]
        if not normalized:
            raise RuntimeError(
                f"Participante {participant.personality} sem decisões após parse. raw={content!r} parsed={json.dumps(parsed, ensure_ascii=False)}"
            )
        return normalized

    def _parse_llm_json_content(self, content: str) -> Dict[str, Any]:
        raw = content.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
            if not match:
                raise RuntimeError(f"Resposta do modelo não é JSON válido: {raw}")
            try:
                parsed = json.loads(match.group(1))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Resposta do modelo não é JSON válido: {raw}") from exc

        if isinstance(parsed, list):
            return {"decisions": parsed}
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Resposta do modelo em formato inválido: {raw}")
        return parsed

    def _extract_decisions_payload(self, parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
        if isinstance(parsed.get("decisions"), list):
            return parsed["decisions"]
        if isinstance(parsed.get("decision"), dict):
            return [parsed["decision"]]
        if isinstance(parsed.get("decision"), list):
            return parsed["decision"]
        if all(key in parsed for key in ("symbol", "action")):
            return [parsed]
        raise RuntimeError(f"Resposta do modelo sem campo de decisões utilizável: {json.dumps(parsed, ensure_ascii=False)}")

    def _normalize_llm_decision(self, payload: Dict[str, Any], context: Dict[str, Any]) -> DebateDecision:
        symbol = str(payload["symbol"]).upper()
        symbol_context = next((item for item in context["symbols"] if item["symbol"] == symbol), None)
        score = symbol_context["score"] if symbol_context else {}
        def _to_int(value: Any, default: int) -> int:
            if value is None:
                return default
            if isinstance(value, str):
                value = value.strip().replace("%", "")
            return int(float(value))

        def _to_float(value: Any, default: float) -> float:
            if value is None:
                return default
            if isinstance(value, str):
                value = value.strip().replace("%", "")
            return float(value)

        raw_action = str(payload.get("action", "WAIT")).strip().upper()
        action_map = {
            "OPEN_LONG": "LONG",
            "LONG": "LONG",
            "BUY": "LONG",
            "OPEN_SHORT": "SHORT",
            "SHORT": "SHORT",
            "SELL": "SHORT",
            "WAIT": "WAIT",
            "HOLD": "WAIT",
            "NO_TRADE": "WAIT",
        }
        normalized_action = action_map.get(raw_action, "WAIT")
        return DebateDecision(
            symbol=symbol,
            action=normalized_action,
            confidence=max(0, min(100, _to_int(payload.get("confidence", 50), 50))),
            leverage=max(1, _to_int(payload.get("leverage", 5), 5)),
            position_pct=_to_float(payload.get("position_pct", 0.1), 0.1),
            risk_pct=_to_float(payload.get("risk_pct", 2.0), 2.0),
            reward_pct=_to_float(payload.get("reward_pct", 4.0), 4.0),
            account_risk_pct=_to_float(payload.get("account_risk_pct", 1.0), 1.0),
            reasoning=str(payload.get("reasoning", "")),
            score=float(score.get("score") or 0.0),
            entry_ready=bool(symbol_context.get("entry_ready")) if symbol_context else False,
        )

    def _build_message_content(
        self,
        participant: DebateParticipant,
        decisions: Sequence[DebateDecision],
        context: Dict[str, Any],
        round_number: int,
    ) -> str:
        spec = get_personality_spec(participant.personality)
        best = decisions[0] if decisions else None
        headline = f"{spec.emoji} {spec.label} round {round_number}"
        if best is None:
            return f"{headline}: sem decisão."
        return (
            f"{headline}: melhor leitura em {best.symbol} -> {best.action} "
            f"(confidence={best.confidence}, score={best.score}, entry_ready={best.entry_ready})."
        )

    def _build_vote_reasoning(self, participant: DebateParticipant, decisions: Sequence[DebateDecision]) -> str:
        spec = get_personality_spec(participant.personality)
        top = max(decisions, key=lambda item: (item.confidence, item.score or 0.0), default=None)
        if top is None:
            return f"{spec.label}: sem decisão."
        return f"{spec.label}: {top.symbol} {top.action} com confiança {top.confidence}."

    def _execute_consensus(self, session: DebateSession) -> None:
        for decision in session.final_decisions:
            if decision.action not in {"LONG", "SHORT"}:
                continue
            if not decision.entry_ready:
                decision.execution_result = {
                    "status": "skipped",
                    "message": "Consensus sem trigger de entrada confirmado.",
                }
                continue
            result = execute_wolf_strategy_trade(
                coin=decision.symbol,
                side=decision.action,
                usdt_size=12.0,
                leverage=decision.leverage,
                risk_pct=decision.risk_pct,
                reward_pct=decision.reward_pct,
                account_risk_pct=float(decision.account_risk_pct or 1.0),
            )
            decision.execution_result = result
            decision.executed = result.get("status") in {"success", "pending_entry"}
            decision.executed_at = utc_now_iso() if decision.executed else None
