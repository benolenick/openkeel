#!/usr/bin/env python3
"""IntentionPacket, SessionShard, IntentionBroker.

The landscape does the remembering, not the water.

- IntentionPacket: persistent cross-session goal tracker (lives in Hyphae)
- SessionShard: ephemeral per-session working memory (7-day TTL)
- IntentionBroker: shuttles between Calcifer and Hyphae
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

HYPHAE_URL = "http://127.0.0.1:8100"
SHARD_TTL_DAYS = 7


# ── Helpers ────────────────────────────────────────────────────────────────────

def _hyphae_remember(text: str, tags: dict | None = None) -> bool:
    payload = {"text": text, "source": "calcifer"}
    if tags:
        payload["tags"] = tags
    try:
        req = urllib.request.Request(
            f"{HYPHAE_URL}/remember",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def _hyphae_recall(query: str, top_k: int = 3) -> list[dict]:
    payload = {"query": query, "top_k": top_k}
    try:
        req = urllib.request.Request(
            f"{HYPHAE_URL}/recall",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=3)
        return json.loads(resp.read()).get("results", [])
    except Exception:
        return []


# ── HypothesisVersion ─────────────────────────────────────────────────────────

@dataclass
class HypothesisVersion:
    version: int
    text: str
    confidence: float
    evidence: float = 0.0
    prediction_accuracy: float = 0.0
    parsimony: float = 0.5
    failed: bool = False
    notes: str = ""

    def overall_confidence(self) -> float:
        return 0.4 * self.evidence + 0.4 * self.prediction_accuracy + 0.2 * self.parsimony

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "text": self.text,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "prediction_accuracy": self.prediction_accuracy,
            "parsimony": self.parsimony,
            "failed": self.failed,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> HypothesisVersion:
        return cls(**d)


# ── IntentionPacket ───────────────────────────────────────────────────────────

@dataclass
class IntentionPacket:
    id: str
    intended_outcome: str
    user: str = "om"
    project: str = "openkeel"

    must_preserve: list[str] = field(default_factory=list)
    forbidden_tradeoffs: list[str] = field(default_factory=list)

    hypothesis_chain: list[HypothesisVersion] = field(default_factory=list)

    attempts: list[dict] = field(default_factory=list)
    stuck_pattern: Optional[str] = None

    next_action: str = ""
    blocker: Optional[str] = None

    prevention_deployed: bool = False
    prevention_tests: list[str] = field(default_factory=list)
    regression_test_until: Optional[str] = None

    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    @classmethod
    def from_goal(cls, goal: str, user: str = "om", project: str = "openkeel") -> IntentionPacket:
        packet_id = hashlib.sha256(f"{user}:{project}:{goal}".encode()).hexdigest()[:16]
        return cls(id=packet_id, intended_outcome=goal, user=user, project=project)

    @property
    def current_hypothesis(self) -> Optional[HypothesisVersion]:
        return self.hypothesis_chain[-1] if self.hypothesis_chain else None

    def add_hypothesis(self, text: str, confidence: float = 0.3) -> HypothesisVersion:
        v = HypothesisVersion(
            version=len(self.hypothesis_chain) + 1,
            text=text,
            confidence=confidence,
        )
        self.hypothesis_chain.append(v)
        self._touch()
        return v

    def record_attempt(self, session_id: str, tried: str, result: str) -> None:
        self.attempts.append({
            "session": session_id,
            "tried": tried,
            "result": result,
            "at": datetime.utcnow().isoformat(),
        })
        self._check_stuck_pattern()
        self._touch()

    def _check_stuck_pattern(self) -> None:
        if len(self.attempts) < 3:
            return
        last_three = [a["result"].lower() for a in self.attempts[-3:]]
        failure_words = {"fail", "regress", "broken", "same", "again", "wrong"}
        failures = sum(1 for r in last_three if any(w in r for w in failure_words))
        if failures >= 2:
            self.stuck_pattern = "symptom_patching"

    def _touch(self) -> None:
        self.updated_at = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "intended_outcome": self.intended_outcome,
            "user": self.user,
            "project": self.project,
            "must_preserve": self.must_preserve,
            "forbidden_tradeoffs": self.forbidden_tradeoffs,
            "hypothesis_chain": [h.to_dict() for h in self.hypothesis_chain],
            "attempts": self.attempts,
            "stuck_pattern": self.stuck_pattern,
            "next_action": self.next_action,
            "blocker": self.blocker,
            "prevention_deployed": self.prevention_deployed,
            "prevention_tests": self.prevention_tests,
            "regression_test_until": self.regression_test_until,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> IntentionPacket:
        chain = [HypothesisVersion.from_dict(h) for h in d.pop("hypothesis_chain", [])]
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj.hypothesis_chain = chain
        return obj

    def summary(self) -> str:
        h = self.current_hypothesis
        parts = [f"goal: {self.intended_outcome[:80]}"]
        if h:
            parts.append(f"hypothesis v{h.version} ({h.confidence:.0%}): {h.text[:60]}")
        if self.stuck_pattern:
            parts.append(f"STUCK: {self.stuck_pattern}")
        if self.next_action:
            parts.append(f"next: {self.next_action[:60]}")
        return " | ".join(parts)


# ── SessionShard ──────────────────────────────────────────────────────────────

@dataclass
class SessionShard:
    session_id: str
    intention_id: str
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    expires_at: str = field(default_factory=lambda: (datetime.utcnow() + timedelta(days=SHARD_TTL_DAYS)).isoformat())

    actions_taken: list[str] = field(default_factory=list)
    obstacles_hit: list[str] = field(default_factory=list)
    discoveries: list[str] = field(default_factory=list)

    session_summary: str = ""
    escalation_decision: str = "CONTINUE"
    escalation_reason: str = ""

    def is_expired(self) -> bool:
        return datetime.utcnow().isoformat() > self.expires_at

    def record_action(self, action: str) -> None:
        self.actions_taken.append(action)

    def record_obstacle(self, obstacle: str) -> None:
        self.obstacles_hit.append(obstacle)

    def record_discovery(self, discovery: str) -> None:
        self.discoveries.append(discovery)

    def close(self, summary: str, decision: str = "CONTINUE", reason: str = "") -> None:
        self.session_summary = summary
        self.escalation_decision = decision
        self.escalation_reason = reason

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "intention_id": self.intention_id,
            "started_at": self.started_at,
            "expires_at": self.expires_at,
            "actions_taken": self.actions_taken,
            "obstacles_hit": self.obstacles_hit,
            "discoveries": self.discoveries,
            "session_summary": self.session_summary,
            "escalation_decision": self.escalation_decision,
            "escalation_reason": self.escalation_reason,
        }


# ── IntentionBroker ───────────────────────────────────────────────────────────

class IntentionBroker:
    """Shuttles IntentionPackets and SessionShards between Calcifer and Hyphae."""

    def __init__(self) -> None:
        self._packets: dict[str, IntentionPacket] = {}
        self._shards: dict[str, SessionShard] = {}

    # ── Intention lifecycle ───────────────────────────────────────────────────

    def get_or_create(self, goal: str, user: str = "om", project: str = "openkeel") -> IntentionPacket:
        """Load matching IntentionPacket from Hyphae or create a fresh one."""
        packet_id = hashlib.sha256(f"{user}:{project}:{goal}".encode()).hexdigest()[:16]

        if packet_id in self._packets:
            return self._packets[packet_id]

        results = _hyphae_recall(f"IntentionPacket {packet_id} {goal[:40]}", top_k=3)
        for r in results:
            text = r.get("text", "")
            if f"IntentionPacket:{packet_id}" in text:
                try:
                    payload = json.loads(text.split("IntentionPacket:", 1)[1])
                    packet = IntentionPacket.from_dict(payload)
                    self._packets[packet_id] = packet
                    return packet
                except Exception:
                    pass

        packet = IntentionPacket.from_goal(goal, user=user, project=project)
        self._packets[packet_id] = packet
        return packet

    def load_intention(self, intention_id: str) -> Optional[IntentionPacket]:
        if intention_id in self._packets:
            return self._packets[intention_id]
        results = _hyphae_recall(f"IntentionPacket:{intention_id}", top_k=2)
        for r in results:
            text = r.get("text", "")
            if f"IntentionPacket:{intention_id}" in text:
                try:
                    payload = json.loads(text.split("IntentionPacket:", 1)[1])
                    packet = IntentionPacket.from_dict(payload)
                    self._packets[intention_id] = packet
                    return packet
                except Exception:
                    pass
        return None

    def merge_to_hyphae(self, intention_id: str) -> bool:
        packet = self._packets.get(intention_id)
        if not packet:
            return False
        text = f"IntentionPacket:{intention_id}{json.dumps(packet.to_dict())}"
        tags = {"intention_id": intention_id, "project": packet.project, "type": "intention"}
        return _hyphae_remember(text, tags)

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def start_session(self, session_id: str, intention_id: str) -> SessionShard:
        shard = SessionShard(session_id=session_id, intention_id=intention_id)
        self._shards[session_id] = shard
        return shard

    def record_discovery(self, session_id: str, text: str) -> None:
        shard = self._shards.get(session_id)
        if shard:
            shard.record_discovery(text)
        _hyphae_remember(f"[session:{session_id}] discovery: {text}", {"session": session_id})

    def record_obstacle(self, session_id: str, text: str) -> None:
        shard = self._shards.get(session_id)
        if shard:
            shard.record_obstacle(text)

    def record_action(self, session_id: str, action: str) -> None:
        shard = self._shards.get(session_id)
        if shard:
            shard.record_action(action)

    def close_session(
        self,
        session_id: str,
        summary: str,
        decision: str = "CONTINUE",
        reason: str = "",
    ) -> Optional[SessionShard]:
        shard = self._shards.get(session_id)
        if not shard:
            return None
        shard.close(summary, decision, reason)

        packet = self._packets.get(shard.intention_id)
        if packet:
            if shard.discoveries:
                packet.next_action = shard.discoveries[-1]
            if decision == "ESCALATE":
                packet.blocker = reason
            self.merge_to_hyphae(shard.intention_id)

        _hyphae_remember(
            f"[session:{session_id}] closed. decision={decision}. {summary}",
            {"session": session_id, "decision": decision},
        )
        return shard

    def get_shard(self, session_id: str) -> Optional[SessionShard]:
        return self._shards.get(session_id)

    def get_packet(self, intention_id: str) -> Optional[IntentionPacket]:
        return self._packets.get(intention_id)

    def status(self) -> str:
        return (
            f"IntentionBroker: {len(self._packets)} packets, "
            f"{len(self._shards)} shards in flight"
        )


# ── Module-level singleton ────────────────────────────────────────────────────

_broker: Optional[IntentionBroker] = None


def get_broker() -> IntentionBroker:
    global _broker
    if _broker is None:
        _broker = IntentionBroker()
    return _broker
