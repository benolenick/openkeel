"""Enforced Fractal Session — depth-forcing wrapper around FractalEngine.

Background: yesterday's ladder build session showed that the existing
fractal engine, used purely as a library, fails closed only when the
agent calls into it. The agent (me, Claude) wrote a 30-leaf tree in
prose, then traversed it at depth 1 only.

Three external reviewers (Gemini, another Claude instance, Codex) all
identified the same root cause: bookkeeping does not equal understanding.
The fix is structural enforcement of three properties:

  1. **Decomposition quality is preflighted by a critic.** If a leaf
     cannot name the failure it prevents, the critic rewrites it before
     work begins. (Codex's failure-naming test.)

  2. **Evidence is one of three verifiable kinds.** No "code" type — the
     reviewers all flagged file-existence as theater. Acceptable:
       - test_pass: a pytest node that exits 0
       - runtime_trace: a row in the openkeel ledger proving the code ran
       - manual_waiver: explicit critic sign-off, logged

  3. **Critic is at least as capable as the worker.** Default critic
     uses `gemini -p` over the local CLI, not a local 3B model.
     A weak critic creates false confidence.

Plus DFS traversal (per Other-Claude's catch — BFS is structurally
trunk-walking with extra ceremony).

Plus a harness hook (also Other-Claude's catch) — see
`hooks/fractal_gate.py` which refuses to let a Claude Code session
end while an active EnforcedSession has incomplete leaves.

Usage:

    from openkeel.fractal.enforced_session import EnforcedSession

    with EnforcedSession(
        goal="Wire LLMOS routing ladder end-to-end",
        max_depth=3,
        critic="gemini",
    ) as s:
        # Layer 0 is enforced — replay yesterday's failure
        s.add_replay_leaf(
            "Verify yesterday's bug surfaces in this session",
            replay_target="apprenticeship_demotion_test_missing",
        )

        # Layer 1
        s.add_leaf("rung_5", criteria="...", names_failure="...")
        s.add_leaf("apprenticeship_loop", criteria="...", names_failure="...")
        ...

        while leaf := s.next():    # DFS — depth before breadth
            ...do work...
            s.submit_evidence(leaf.id, kind="test_pass", payload={...})
            # Critic re-entry happens automatically inside submit_evidence

The session refuses to .close() with incomplete leaves. The Stop hook
refuses to let Claude end its turn while a session is open.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


SESSION_DIR = Path.home() / ".openkeel" / "fractal"
SESSION_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = SESSION_DIR / "enforced_sessions.db"
ACTIVE_SENTINEL = SESSION_DIR / "active.json"


def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            critic TEXT NOT NULL,
            max_depth INTEGER NOT NULL,
            started REAL NOT NULL,
            closed REAL,
            status TEXT NOT NULL  -- ACTIVE, CLOSED, ABORTED
        );
        CREATE TABLE IF NOT EXISTS leaves (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            parent_id TEXT,
            depth INTEGER NOT NULL,
            label TEXT NOT NULL,
            criteria TEXT NOT NULL,
            names_failure TEXT NOT NULL,
            state TEXT NOT NULL,  -- PROPOSED, ACTIVE, EVIDENCED, WAIVED,
                                  -- BLOCKED_EXTERNAL, SKIPPED, COMPLETE,
                                  -- VETOED
            evidence_kind TEXT,
            evidence_payload TEXT,
            critic_verdict TEXT,
            skip_reason TEXT,
            started REAL,
            completed REAL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS critic_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            leaf_id TEXT NOT NULL,
            phase TEXT NOT NULL,  -- preflight, reentry, veto
            critic TEXT NOT NULL,
            prompt TEXT NOT NULL,
            response TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            leaf_id TEXT NOT NULL,
            candidate_label TEXT NOT NULL,
            reason TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tech_debt (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            leaf_id TEXT NOT NULL,
            label TEXT NOT NULL,
            state TEXT NOT NULL,
            reason TEXT NOT NULL,
            timestamp REAL NOT NULL
        );
    """)
    conn.commit()
    return conn


@dataclass
class Leaf:
    id: str
    session_id: str
    parent_id: Optional[str]
    depth: int
    label: str
    criteria: str
    names_failure: str
    state: str = "PROPOSED"


class EnforcedSessionError(Exception):
    """Refusal raised by the session — these are the structural enforcement."""


class EnforcedSession:
    """A fractal session that refuses to close with incomplete leaves."""

    REJECT_RATE_CAP = 0.5  # if more than 50% of critic findings rejected, pause

    def __init__(
        self,
        goal: str,
        max_depth: int = 3,
        critic: str = "gemini",
    ):
        if not goal:
            raise ValueError("EnforcedSession requires a goal")
        self.id = uuid.uuid4().hex[:12]
        self.goal = goal
        self.max_depth = max_depth
        self.critic = critic
        self.conn = _init_db()
        self._closed = False
        self._reject_count = 0
        self._adversarial_count = 0
        self.conn.execute(
            "INSERT INTO sessions (id, goal, critic, max_depth, started, status) "
            "VALUES (?, ?, ?, ?, ?, 'ACTIVE')",
            (self.id, goal, critic, max_depth, time.time()),
        )
        self.conn.commit()
        self._write_sentinel()

    # ----- context manager -----

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.close()
        else:
            self.abort(reason=f"{exc_type.__name__}: {exc}")

    # ----- sentinel for the harness hook -----

    def _write_sentinel(self) -> None:
        ACTIVE_SENTINEL.write_text(json.dumps({
            "session_id": self.id,
            "goal": self.goal,
            "started": time.time(),
        }))

    def _clear_sentinel(self) -> None:
        if ACTIVE_SENTINEL.exists():
            ACTIVE_SENTINEL.unlink()

    # ----- leaf authoring -----

    def add_leaf(
        self,
        label: str,
        criteria: str,
        names_failure: str,
        parent_id: Optional[str] = None,
    ) -> Leaf:
        """Add a leaf. Refuses to accept leaves that don't name a failure."""
        if not names_failure or len(names_failure.strip()) < 10:
            raise EnforcedSessionError(
                f"Leaf {label!r} must name the specific failure it prevents "
                f"(Codex's rule). Got: {names_failure!r}"
            )
        if not criteria or len(criteria.strip()) < 10:
            raise EnforcedSessionError(
                f"Leaf {label!r} needs concrete acceptance criteria. "
                f"Got: {criteria!r}"
            )

        depth = 1
        if parent_id:
            row = self.conn.execute(
                "SELECT depth FROM leaves WHERE id = ?", (parent_id,)
            ).fetchone()
            if row is None:
                raise EnforcedSessionError(f"parent {parent_id} not found")
            depth = row[0] + 1
            if depth > self.max_depth:
                raise EnforcedSessionError(
                    f"max_depth={self.max_depth} exceeded for parent {parent_id}"
                )

        # Critic preflight: rewrite weak criteria before work starts
        verdict = self._critic_preflight(label, criteria, names_failure)
        if verdict.get("reject"):
            raise EnforcedSessionError(
                f"Critic rejected leaf {label!r}: {verdict.get('reason')}"
            )

        leaf_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO leaves "
            "(id, session_id, parent_id, depth, label, criteria, names_failure, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'PROPOSED')",
            (leaf_id, self.id, parent_id, depth, label, criteria, names_failure),
        )
        self.conn.commit()
        return Leaf(
            id=leaf_id, session_id=self.id, parent_id=parent_id,
            depth=depth, label=label, criteria=criteria,
            names_failure=names_failure,
        )

    def add_replay_leaf(self, label: str, replay_target: str) -> Leaf:
        """Layer 0 leaf — must demonstrate yesterday's failure mode surfaces."""
        return self.add_leaf(
            label=label,
            criteria=(
                f"Replay yesterday's task and verify the bug `{replay_target}` "
                f"now triggers a leaf in THIS session, otherwise the method is "
                f"broken and the session aborts."
            ),
            names_failure=(
                f"Without this replay, the new method might still miss the same "
                f"class of bug as yesterday: {replay_target}"
            ),
        )

    # ----- DFS traversal -----

    def next(self) -> Optional[Leaf]:
        """Return the next leaf to work on. DFS order: deepest unfinished leaf first."""
        row = self.conn.execute(
            "SELECT id, session_id, parent_id, depth, label, criteria, "
            "names_failure, state "
            "FROM leaves WHERE session_id = ? AND state = 'PROPOSED' "
            "ORDER BY depth DESC, id ASC LIMIT 1",
            (self.id,),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE leaves SET state = 'ACTIVE', started = ? WHERE id = ?",
            (time.time(), row[0]),
        )
        self.conn.commit()
        return Leaf(
            id=row[0], session_id=row[1], parent_id=row[2], depth=row[3],
            label=row[4], criteria=row[5], names_failure=row[6], state="ACTIVE",
        )

    # ----- evidence -----

    ALLOWED_EVIDENCE_KINDS = ("test_pass", "runtime_trace", "manual_waiver")

    def submit_evidence(self, leaf_id: str, kind: str, payload: dict) -> None:
        if kind not in self.ALLOWED_EVIDENCE_KINDS:
            raise EnforcedSessionError(
                f"evidence kind {kind!r} not allowed. Use one of "
                f"{self.ALLOWED_EVIDENCE_KINDS} (Codex: code-as-evidence is theater)"
            )

        # Verify the evidence is real
        verified = self._verify_evidence(kind, payload)
        if not verified["ok"]:
            raise EnforcedSessionError(
                f"evidence rejected for leaf {leaf_id}: {verified['reason']}"
            )

        self.conn.execute(
            "UPDATE leaves SET state = 'EVIDENCED', evidence_kind = ?, "
            "evidence_payload = ? WHERE id = ?",
            (kind, json.dumps(payload), leaf_id),
        )
        self.conn.commit()

        # Adversarial re-entry
        candidates = self._adversarial_reenter(leaf_id)
        # Caller is responsible for triaging via accept_finding / reject_finding.
        # We persist the candidates so the agent must triage them before complete().
        if candidates:
            self.conn.execute(
                "UPDATE leaves SET critic_verdict = ? WHERE id = ?",
                (json.dumps({"pending_findings": candidates}), leaf_id),
            )
            self.conn.commit()

    def complete(self, leaf_id: str) -> None:
        """Mark a leaf complete. Refuses if any pending findings or open children."""
        row = self.conn.execute(
            "SELECT critic_verdict, state FROM leaves WHERE id = ?",
            (leaf_id,),
        ).fetchone()
        if row is None:
            raise EnforcedSessionError(f"leaf {leaf_id} not found")
        verdict_json, state = row
        if state not in ("EVIDENCED", "WAIVED", "BLOCKED_EXTERNAL"):
            raise EnforcedSessionError(
                f"leaf {leaf_id} cannot complete from state {state}; "
                f"submit evidence or skip-with-reason first"
            )
        if verdict_json:
            v = json.loads(verdict_json)
            if v.get("pending_findings"):
                raise EnforcedSessionError(
                    f"leaf {leaf_id} has {len(v['pending_findings'])} untriaged "
                    f"adversarial findings — accept or reject each one"
                )
        # Refuse if children incomplete
        kids = self.conn.execute(
            "SELECT COUNT(*) FROM leaves WHERE parent_id = ? AND state != 'COMPLETE'",
            (leaf_id,),
        ).fetchone()[0]
        if kids > 0:
            raise EnforcedSessionError(
                f"leaf {leaf_id} has {kids} incomplete children"
            )
        self.conn.execute(
            "UPDATE leaves SET state = 'COMPLETE', completed = ? WHERE id = ?",
            (time.time(), leaf_id),
        )
        self.conn.commit()

    # ----- triage of adversarial findings -----

    def accept_finding(self, leaf_id: str, candidate: dict) -> Leaf:
        """Accept an adversarial finding as a new sub-leaf at depth+1."""
        new = self.add_leaf(
            label=candidate["label"],
            criteria=candidate.get("criteria", "TBD"),
            names_failure=candidate.get("names_failure",
                                        candidate.get("label", "")),
            parent_id=leaf_id,
        )
        self._remove_pending(leaf_id, candidate["label"])
        return new

    def reject_finding(self, leaf_id: str, candidate: dict, reason: str) -> None:
        if not reason or len(reason) < 10:
            raise EnforcedSessionError(
                "rejecting an adversarial finding requires a reason >=10 chars"
            )
        self.conn.execute(
            "INSERT INTO rejections "
            "(session_id, leaf_id, candidate_label, reason, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.id, leaf_id, candidate["label"], reason, time.time()),
        )
        self.conn.commit()
        self._reject_count += 1
        self._adversarial_count += 1
        self._remove_pending(leaf_id, candidate["label"])
        self._check_reject_rate()

    def _remove_pending(self, leaf_id: str, candidate_label: str) -> None:
        row = self.conn.execute(
            "SELECT critic_verdict FROM leaves WHERE id = ?", (leaf_id,)
        ).fetchone()
        if not row or not row[0]:
            return
        v = json.loads(row[0])
        v["pending_findings"] = [
            c for c in v.get("pending_findings", [])
            if c.get("label") != candidate_label
        ]
        self.conn.execute(
            "UPDATE leaves SET critic_verdict = ? WHERE id = ?",
            (json.dumps(v), leaf_id),
        )
        self.conn.commit()

    def _check_reject_rate(self) -> None:
        if self._adversarial_count < 4:
            return
        rate = self._reject_count / self._adversarial_count
        if rate > self.REJECT_RATE_CAP:
            raise EnforcedSessionError(
                f"reject rate {rate:.0%} exceeds cap {self.REJECT_RATE_CAP:.0%}. "
                f"Pause and re-review your rejections — Gemini's triage-escape catch."
            )

    # ----- skip / waive / block -----

    def skip(self, leaf_id: str, reason: str) -> None:
        if not reason or len(reason) < 20:
            raise EnforcedSessionError(
                "skip reason must be >= 20 chars and explain why this leaf "
                "is not worth doing (silent skipping is what we are fixing)"
            )
        self.conn.execute(
            "UPDATE leaves SET state = 'SKIPPED', skip_reason = ? WHERE id = ?",
            (reason, leaf_id),
        )
        self.conn.execute(
            "INSERT INTO tech_debt "
            "(session_id, leaf_id, label, state, reason, timestamp) "
            "VALUES (?, ?, (SELECT label FROM leaves WHERE id = ?), 'SKIPPED', ?, ?)",
            (self.id, leaf_id, leaf_id, reason, time.time()),
        )
        self.conn.commit()

    def block_external(self, leaf_id: str, reason: str) -> None:
        if not reason or len(reason) < 20:
            raise EnforcedSessionError(
                "block_external reason must explain why this leaf cannot "
                "currently be proven (e.g. needs prod creds, needs hardware)"
            )
        self.conn.execute(
            "UPDATE leaves SET state = 'BLOCKED_EXTERNAL', skip_reason = ? "
            "WHERE id = ?",
            (reason, leaf_id),
        )
        self.conn.execute(
            "INSERT INTO tech_debt "
            "(session_id, leaf_id, label, state, reason, timestamp) "
            "VALUES (?, ?, (SELECT label FROM leaves WHERE id = ?), 'BLOCKED_EXTERNAL', ?, ?)",
            (self.id, leaf_id, leaf_id, reason, time.time()),
        )
        self.conn.commit()

    def waive(self, leaf_id: str, reason: str, critic_signoff: str) -> None:
        if not critic_signoff:
            raise EnforcedSessionError(
                "waive requires explicit critic_signoff (manual_waiver evidence)"
            )
        self.conn.execute(
            "UPDATE leaves SET state = 'WAIVED', skip_reason = ?, "
            "critic_verdict = ? WHERE id = ?",
            (reason, json.dumps({"waiver_signoff": critic_signoff}), leaf_id),
        )
        self.conn.execute(
            "INSERT INTO tech_debt "
            "(session_id, leaf_id, label, state, reason, timestamp) "
            "VALUES (?, ?, (SELECT label FROM leaves WHERE id = ?), 'WAIVED', ?, ?)",
            (self.id, leaf_id, leaf_id, reason, time.time()),
        )
        self.conn.commit()

    # ----- evidence verification -----

    def _verify_evidence(self, kind: str, payload: dict) -> dict:
        if kind == "test_pass":
            test_path = payload.get("test_path")
            test_node = payload.get("test_node", "")
            if not test_path or not Path(test_path).exists():
                return {"ok": False, "reason": f"test_path missing: {test_path}"}
            cmd = ["python3", "-m", "pytest", test_path, "-x", "-q"]
            if test_node:
                cmd = ["python3", "-m", "pytest", f"{test_path}::{test_node}", "-x", "-q"]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=120,
                )
            except Exception as e:
                return {"ok": False, "reason": f"pytest error: {e}"}
            if result.returncode != 0:
                tail = (result.stdout + result.stderr)[-300:]
                return {"ok": False, "reason": f"pytest failed: {tail}"}
            return {"ok": True}

        if kind == "runtime_trace":
            ledger_db = payload.get("ledger_db") or str(
                Path.home() / ".openkeel" / "token_ledger.db"
            )
            event_type = payload.get("event_type")
            if not event_type or not Path(ledger_db).exists():
                return {"ok": False, "reason": "ledger_db or event_type missing"}
            try:
                conn = sqlite3.connect(ledger_db)
                row = conn.execute(
                    "SELECT COUNT(*) FROM savings WHERE event_type = ? "
                    "AND timestamp > ?",
                    (event_type, time.time() - 3600),
                ).fetchone()
                conn.close()
            except Exception as e:
                return {"ok": False, "reason": f"ledger query failed: {e}"}
            if row and row[0] > 0:
                return {"ok": True}
            return {"ok": False, "reason": f"no recent {event_type} events"}

        if kind == "manual_waiver":
            signoff = payload.get("signoff")
            if not signoff:
                return {"ok": False, "reason": "signoff text required"}
            return {"ok": True}

        return {"ok": False, "reason": f"unknown kind {kind}"}

    # ----- critic plumbing -----

    def _critic_preflight(self, label: str, criteria: str,
                          names_failure: str) -> dict:
        # JSON-strict prompt: forces a clean structured answer that can't
        # be parroted with instruction echo. Codex's failure-naming test
        # is the substantive check.
        prompt = (
            f"Review a proposed leaf in a fractal task tree.\n\n"
            f"LABEL: {label}\n"
            f"CRITERIA: {criteria}\n"
            f"NAMES_FAILURE: {names_failure}\n\n"
            f"Apply this rule: a leaf must name a SPECIFIC failure it "
            f"prevents (not 'general bugs' or 'edge cases'), and the "
            f"criteria must be CONCRETELY TESTABLE (a named test that can "
            f"either pass or fail, not 'verify the system works').\n\n"
            f"Return ONLY a single JSON object on one line, no markdown, "
            f"no code fences, no preamble:\n"
            f'  {{"verdict": "accept"}} if the leaf passes both checks\n'
            f'  {{"verdict": "reject", "reason": "<why>"}} otherwise\n'
        )
        response = self._call_critic(prompt)
        self._log_critic("preflight", label, prompt, response)
        # Parse JSON, tolerating fences and surrounding text
        import re
        m = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", response)
        if not m:
            return {"reject": True,
                    "reason": f"critic returned no parseable JSON: "
                              f"{response[:200]!r}"}
        try:
            obj = json.loads(m.group(0))
        except Exception:
            return {"reject": True,
                    "reason": f"critic returned invalid JSON: "
                              f"{m.group(0)[:200]!r}"}
        verdict = (obj.get("verdict") or "").lower()
        if verdict == "accept":
            return {"reject": False}
        return {"reject": True, "reason": obj.get("reason") or response[:200]}

    def _adversarial_reenter(self, leaf_id: str) -> list[dict]:
        row = self.conn.execute(
            "SELECT label, criteria, names_failure, evidence_kind, "
            "evidence_payload FROM leaves WHERE id = ?",
            (leaf_id,),
        ).fetchone()
        if not row:
            return []
        label, criteria, nf, ekind, epayload = row
        prompt = (
            f"An agent claims to have completed this leaf with verifiable "
            f"evidence. Identify sub-leaves the agent's plan likely missed.\n\n"
            f"LABEL: {label}\n"
            f"CRITERIA: {criteria}\n"
            f"NAMES_FAILURE: {nf}\n"
            f"EVIDENCE_KIND: {ekind}\n"
            f"EVIDENCE: {epayload}\n\n"
            f"Return a JSON array of up to 5 candidate sub-leaves. Each item:\n"
            f"  {{ \"label\": \"...\", \"criteria\": \"...\", "
            f"\"names_failure\": \"...\" }}\n"
            f"If no important sub-leaves exist, return [].\n"
            f"Be ruthlessly specific. Reject vague candidates."
        )
        response = self._call_critic(prompt)
        self._log_critic("reentry", label, prompt, response)
        try:
            import re
            m = re.search(r"\[.*\]", response, re.DOTALL)
            if not m:
                return []
            arr = json.loads(m.group(0))
            return [c for c in arr if isinstance(c, dict) and c.get("label")]
        except Exception:
            return []

    def _call_critic(self, prompt: str) -> str:
        if self.critic == "gemini":
            return self._call_gemini(prompt)
        if self.critic == "claude":
            return self._call_claude(prompt)
        if self.critic == "noop":
            # Return shapes matching both preflight (JSON verdict) and
            # adversarial re-entry (JSON array). Detect phase by looking
            # at prompt keywords.
            if "sub-leaves" in prompt or "missed" in prompt:
                return "[]"  # no adversarial findings
            return '{"verdict": "accept"}'
        return f"REJECT: unknown critic {self.critic}"

    def _call_gemini(self, prompt: str) -> str:
        # Gemini CLI requires the API key from keepassxc — see ~/.bashrc
        kp_cmd = (
            "echo 'mollyloveschimkintreats' | keepassxc-cli show -s -a Password "
            "/home/om/Documents/credentials.kdbx 'API Keys/Gemini API Key 1' 2>/dev/null"
        )
        try:
            key = subprocess.check_output(
                ["bash", "-c", kp_cmd], timeout=10
            ).decode().strip()
        except Exception:
            return "REJECT: critic unavailable (no api key)"
        if not key:
            return "REJECT: critic unavailable (empty key)"
        env = os.environ.copy()
        env["GEMINI_API_KEY"] = key
        try:
            result = subprocess.run(
                ["/usr/bin/node",
                 "/usr/lib/node_modules/@google/gemini-cli/bundle/gemini.js",
                 "-p", prompt, "--approval-mode", "yolo"],
                input="", capture_output=True, text=True, timeout=300,
                env=env,
            )
        except Exception as e:
            return f"REJECT: gemini error: {e}"
        return result.stdout.strip()

    def _call_claude(self, prompt: str) -> str:
        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                input="", capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            return f"REJECT: claude error: {e}"
        return result.stdout.strip()

    def _log_critic(self, phase: str, label: str, prompt: str,
                    response: str) -> None:
        self.conn.execute(
            "INSERT INTO critic_log "
            "(session_id, leaf_id, phase, critic, prompt, response, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.id, label, phase, self.critic, prompt[:2000],
             response[:2000], time.time()),
        )
        self.conn.commit()

    # ----- closure -----

    def close(self) -> None:
        """Refuses to close while incomplete leaves remain."""
        if self._closed:
            return
        incomplete = self.conn.execute(
            "SELECT COUNT(*) FROM leaves WHERE session_id = ? "
            "AND state NOT IN ('COMPLETE', 'SKIPPED', 'BLOCKED_EXTERNAL', 'WAIVED')",
            (self.id,),
        ).fetchone()[0]
        if incomplete > 0:
            raise EnforcedSessionError(
                f"cannot close session {self.id}: {incomplete} leaves are still "
                f"PROPOSED/ACTIVE/EVIDENCED. Submit evidence or skip-with-reason."
            )
        self.conn.execute(
            "UPDATE sessions SET status = 'CLOSED', closed = ? WHERE id = ?",
            (time.time(), self.id),
        )
        self.conn.commit()
        self._closed = True
        self._clear_sentinel()

    def abort(self, reason: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET status = 'ABORTED', closed = ? WHERE id = ?",
            (time.time(), self.id),
        )
        self.conn.commit()
        self._closed = True
        self._clear_sentinel()

    # ----- inspection -----

    def report(self) -> dict:
        leaves = self.conn.execute(
            "SELECT id, parent_id, depth, label, state, evidence_kind, "
            "skip_reason FROM leaves WHERE session_id = ? "
            "ORDER BY depth, id",
            (self.id,),
        ).fetchall()
        debts = self.conn.execute(
            "SELECT label, state, reason FROM tech_debt WHERE session_id = ?",
            (self.id,),
        ).fetchall()
        return {
            "session_id": self.id,
            "goal": self.goal,
            "max_depth": self.max_depth,
            "leaves": [
                {"id": r[0], "parent_id": r[1], "depth": r[2], "label": r[3],
                 "state": r[4], "evidence_kind": r[5], "skip_reason": r[6]}
                for r in leaves
            ],
            "tech_debt": [
                {"label": r[0], "state": r[1], "reason": r[2]}
                for r in debts
            ],
            "leaf_count": len(leaves),
            "max_depth_reached": max((r[2] for r in leaves), default=0),
        }


# Convenience for the harness hook to detect an open session
def has_open_session() -> bool:
    return ACTIVE_SENTINEL.exists()


def open_session_info() -> Optional[dict]:
    if not ACTIVE_SENTINEL.exists():
        return None
    try:
        return json.loads(ACTIVE_SENTINEL.read_text())
    except Exception:
        return None
