"""
Token Saver v5 — the honest layer.

v5 is not a replacement for v3/v4. It is a **surgical patch set** that:

  1. Fixes the critical bugs v3/v4 ship with today (JSON corruption,
     silent failures, fake line counts, code stripping, dead audit.db).
  2. Adds the two highest-leverage phase-2 features that v3/v4 never
     touched (deferred session-start context, error-loop detection).
  3. Centralizes config so the daemon/model/thresholds are in one place.

v5 modules are pure utilities. v3's pre_tool.py / local_edit.py /
session_start.py call into v5 at specific hotspots. If you disable v5
with `TOKEN_SAVER_V5=0`, you get exact v3/v4 behavior (modulo the
in-place patches, which are safe).

PUBLIC API:
  CFG                      — centralized config (openkeel.token_saver_v5.config)
  debug_log.note/swallow   — structured exception logging
  json_guard.looks_structured / should_bypass_compression
  hook_chatter.edit_applied / bash_compressed / ...  — terse status lines
  localedit_verify.verify_edit                         — real diff + rollback
  error_loop.observe                                   — N-strike error nudges
  deferred_context.capture / score_and_emit            — relevance-gated dump

DESIGN PRINCIPLES (learned from v4):
  - Fail-open but VISIBLE. Every swallow hits debug_log.
  - No new engines that aren't wired into a live hook.
  - Every module gets at least one test in tests/.
  - Every user-facing message is a single short line, not a paragraph.
  - Never compress structured data (JSON/HTML/CSV) with a text LLM.
"""

from .config import CFG

__version__ = "5.0.0-dev"
__all__ = ["CFG", "__version__"]
