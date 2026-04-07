"""Token Saver v4 — experimental compounding layer on top of v3.

Adds three features, all opt-in via TOKEN_SAVER_V4=1:

  1. lingua_compressor — aggressive prose/code pruning for injected context.
     Uses LLMLingua-2 if available, falls back to a rule-based pruner that
     works with zero extra dependencies.

  2. subagent_offload — detects exploration chains (many Grep/Read/Glob with
     no Edit) and nudges the main agent to delegate to an Explore subagent.
     Nudge-only. Never auto-spawns.

  3. hybrid_recall — on Hyphae /recall results, detects multi-hop queries
     (regex first, LLM classifier second) and expands the result set via a
     cheap entity-graph stored in a sibling SQLite DB. Manifold recall stays
     the default for single-hop queries.

Zero edits to v3. Everything in this package is additive. The v3 stack keeps
running exactly as-is whenever TOKEN_SAVER_V4 is unset.
"""

from __future__ import annotations

import os

__version__ = "0.4.0"

def is_enabled() -> bool:
    return os.environ.get("TOKEN_SAVER_V4", "0") == "1"
