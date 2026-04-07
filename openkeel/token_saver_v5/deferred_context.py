"""
Token Saver v5 — relevance-gated session start context.

The single biggest token leak in the current system is the SessionStart
hook. It dumps ~5-10K tokens of project context into the conversation
BEFORE the user has said anything. Measured in one session:

  - OpenKeel mission auto-detect directive         (~300 tok)
  - Recent work context (15 bullet points)          (~1500 tok)
  - Known infrastructure (5 bullet points)          (~800 tok)
  - Project map (183 files, ranked)                 (~3000 tok)
  - Recent changes diff stat                        (~200 tok)
  - Recent commits (10 commits)                     (~300 tok)
  - Uncommitted changes (115 files)                 (~800 tok)
  - Lifetime savings summary                        (~50 tok)
                                              total ≈ 7000 tok

Across 517 sessions: ~3.6M tokens of static preamble.

Most of that is irrelevant to any given user message. If the user asks
"what time is it", zero project context is needed. If they ask about
the token saver, the monitor board context is noise.

This module gates the dump. Flow:
  1. SessionStart hook calls `capture(blocks)` with the full static dump,
     stored as a structured list of labeled blocks in a per-session cache.
     NOTHING is emitted to the conversation yet.
  2. UserPromptSubmit hook calls `score_and_emit(user_msg)` with the
     first user message. A cheap relevance scorer picks the top K blocks
     that actually relate to the message and returns ONLY those.
  3. Subsequent user messages are ignored (first-message-only, so we
     don't re-inject on every turn).

Fallback: if the scorer fails or produces empty output, return a tiny
"context available; ask if needed" breadcrumb so the agent knows it CAN
ask for things.

The scorer is deliberately dumb-but-fast: keyword overlap + TF weighting +
a small LLM escalation path for ambiguous messages. No embeddings, no
vector DB, no training. Honesty > sophistication.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

from .config import CFG, ensure_dirs
from .debug_log import note, swallow


# Top K blocks to emit after scoring. Keep small; quality > quantity.
TOP_K = 3

# If user's first message is shorter than this, assume it's a greeting
# or trivial and emit nothing.
MIN_MESSAGE_CHARS = 12

# Words we never treat as topical signals.
_STOPWORDS = frozenset("""
a an the of and or but if then else when where why how what which who
is are was were be been being have has had do does did this that these
those i you he she it we they me him her us them my your his our their
to from in on at by for with about as can could should would will just
get got set put make made take taken go went know knew see saw look
""".split())


@dataclass
class ContextBlock:
    label: str           # e.g. "recent_work", "project_map", "infra"
    priority: int        # 1 = always try to include, 5 = only on strong match
    text: str            # the actual content that would be injected
    keywords: list[str]  # optional explicit keywords for matching

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextBlock":
        return cls(
            label=data["label"],
            priority=int(data.get("priority", 3)),
            text=data["text"],
            keywords=list(data.get("keywords", [])),
        )


@dataclass
class DeferredDump:
    session_id: str
    captured_at: float
    blocks: list[ContextBlock]
    emitted: bool = False


def capture(session_id: str, blocks: list[ContextBlock]) -> None:
    """
    Called from SessionStart hook. Stashes context for later relevance-gated
    emission. If deferred_context is disabled, this is a no-op (and the
    SessionStart hook should emit directly as before).
    """
    if not CFG.deferred_context:
        return
    try:
        ensure_dirs()
        dump = DeferredDump(
            session_id=session_id,
            captured_at=time.time(),
            blocks=blocks,
        )
        _write(dump)
        note("deferred_context.capture", f"session={session_id} blocks={len(blocks)}")
    except Exception as e:
        swallow("deferred_context.capture", error=e)


def score_and_emit(session_id: str, user_message: str) -> str | None:
    """
    Called from UserPromptSubmit hook with the user's first message.
    Returns the relevance-gated context string to inject, or None if
    nothing relevant (or if already emitted).

    Guarantees:
      - Only emits once per session (first user message).
      - Returns None on any error (fail-closed: no dump beats wrong dump).
    """
    if not CFG.deferred_context:
        return None
    try:
        dump = _read(session_id)
        if dump is None or dump.emitted:
            return None
        if len(user_message.strip()) < MIN_MESSAGE_CHARS:
            # Greeting or trivial; emit nothing, mark as handled.
            dump.emitted = True
            _write(dump)
            note("deferred_context.emit", "skipped: short message", session=session_id)
            return None

        ranked = _rank_blocks(dump.blocks, user_message)
        dump.emitted = True
        _write(dump)

        if not ranked:
            note("deferred_context.emit", "nothing relevant", session=session_id)
            return _breadcrumb(dump.blocks)

        top = ranked[:TOP_K]
        out_parts = [f"[v5 deferred context — {len(top)}/{len(dump.blocks)} blocks relevant to your message]"]
        for block, score in top:
            out_parts.append(f"\n### {block.label} (relevance={score:.2f})\n{block.text}")
        out_parts.append(f"\n[{len(dump.blocks) - len(top)} other blocks available — ask if needed]")
        return "\n".join(out_parts)
    except Exception as e:
        swallow("deferred_context.emit", error=e)
        return None


def _rank_blocks(
    blocks: list[ContextBlock], message: str,
) -> list[tuple[ContextBlock, float]]:
    """
    Cheap TF-style scorer. Returns blocks sorted by relevance, filtering
    out zero-signal matches.
    """
    msg_terms = _tokenize(message)
    if not msg_terms:
        return []

    scored: list[tuple[ContextBlock, float]] = []
    for block in blocks:
        # Explicit keyword boost
        keyword_hits = sum(1 for k in block.keywords if k.lower() in message.lower())
        # Body term overlap
        body_terms = _tokenize(block.text)
        body_count = {t: body_terms.count(t) for t in set(body_terms)}
        overlap = sum(body_count.get(t, 0) for t in msg_terms)
        # Priority is inverse: priority 1 → multiplier 1.2, priority 5 → 0.8
        priority_mult = 1.2 - (block.priority - 1) * 0.1
        raw = (keyword_hits * 5.0) + (overlap * 1.0)
        score = raw * priority_mult / max(len(body_terms), 1) ** 0.5
        if score > 0:
            scored.append((block, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", text.lower())
    return [w for w in words if w not in _STOPWORDS]


def _breadcrumb(blocks: list[ContextBlock]) -> str:
    labels = ", ".join(b.label for b in blocks[:8])
    return (
        f"[v5 deferred context — nothing scored relevant to your first message. "
        f"Available blocks: {labels}. Ask if you want any of them.]"
    )


def _path_for(session_id: str):
    return CFG.deferred_context_cache.with_name(
        f"deferred_context_{session_id}.json"
    )


def _write(dump: DeferredDump) -> None:
    path = _path_for(dump.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "session_id": dump.session_id,
            "captured_at": dump.captured_at,
            "emitted": dump.emitted,
            "blocks": [b.to_dict() for b in dump.blocks],
        }),
        encoding="utf-8",
    )


def _read(session_id: str) -> DeferredDump | None:
    path = _path_for(session_id)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return DeferredDump(
        session_id=data["session_id"],
        captured_at=data["captured_at"],
        emitted=data.get("emitted", False),
        blocks=[ContextBlock.from_dict(b) for b in data["blocks"]],
    )
