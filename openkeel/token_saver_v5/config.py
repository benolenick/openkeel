"""
Token Saver v5 — centralized configuration.

All hosts, ports, thresholds, and paths live here. v3/v4 have them scattered
across ~6 files; v5 reads everything through this module. New code should
`from openkeel.token_saver_v5.config import CFG` and use attributes, never
hard-code hosts or magic numbers.

Env var overrides let you move the daemon, point at a different Ollama,
or shift thresholds without editing code.

IMPORTANT: env vars are read *per instantiation*, not at class definition
time. Tests call `reload()` after setting env vars; production code uses
the module-level CFG which reads env once at first import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw not in ("0", "false", "False", "no", "")


@dataclass
class Config:
    # --- daemon / ollama endpoints ---
    daemon_url: str = field(default_factory=lambda: _env("TOKEN_SAVER_DAEMON", "http://127.0.0.1:11450"))
    ollama_url: str = field(default_factory=lambda: _env("TOKEN_SAVER_OLLAMA", "http://127.0.0.1:11434"))
    jagg_ollama_url: str = field(default_factory=lambda: _env("TOKEN_SAVER_JAGG_OLLAMA", "http://192.168.0.224:11434"))

    # --- models ---
    fast_model: str = field(default_factory=lambda: _env("TOKEN_SAVER_FAST_MODEL", "qwen2.5:3b"))
    escalation_model: str = field(default_factory=lambda: _env("TOKEN_SAVER_ESCALATION_MODEL", "gemma2:27b"))

    # --- thresholds (chars, not tokens) ---
    bash_compress_min: int = field(default_factory=lambda: _env_int("TOKEN_SAVER_BASH_MIN", 2000))
    file_skeleton_min: int = field(default_factory=lambda: _env_int("TOKEN_SAVER_SKELETON_MIN", 4000))
    goal_reader_min: int = field(default_factory=lambda: _env_int("TOKEN_SAVER_GOAL_MIN", 4000))

    # --- paths ---
    ledger_db: Path = field(default_factory=lambda: Path(_env(
        "TOKEN_SAVER_LEDGER",
        str(Path.home() / ".openkeel" / "token_ledger.db"),
    )))
    debug_log: Path = field(default_factory=lambda: Path(_env(
        "TOKEN_SAVER_DEBUG_LOG",
        str(Path.home() / ".openkeel" / "logs" / "token_saver_debug.log"),
    )))
    deferred_context_cache: Path = field(default_factory=lambda: Path(_env(
        "TOKEN_SAVER_DEFERRED_CACHE",
        str(Path.home() / ".openkeel" / "cache" / "deferred_context.json"),
    )))
    error_loop_state: Path = field(default_factory=lambda: Path(_env(
        "TOKEN_SAVER_ERROR_STATE",
        str(Path.home() / ".openkeel" / "cache" / "error_loop_state.json"),
    )))

    # --- behavior flags ---
    enabled: bool = field(default_factory=lambda: _env_bool("TOKEN_SAVER_V5", True))
    deferred_context: bool = field(default_factory=lambda: _env_bool("TOKEN_SAVER_V5_DEFERRED", False))
    error_loop_nudges: bool = field(default_factory=lambda: _env_bool("TOKEN_SAVER_V5_ERRORLOOP", True))
    terse_hooks: bool = field(default_factory=lambda: _env_bool("TOKEN_SAVER_V5_TERSE", True))


CFG = Config()


def reload() -> Config:
    """
    Re-read all env vars and overwrite CFG in place.

    Tests call this after setting env vars. Modules that did
    `from .config import CFG` keep their reference valid because we mutate
    the existing instance rather than swapping it out.
    """
    fresh = Config()
    for field_name in Config.__dataclass_fields__:
        setattr(CFG, field_name, getattr(fresh, field_name))
    return CFG


def ensure_dirs() -> None:
    """Create the directories v5 writes to. Safe to call many times."""
    for p in (CFG.debug_log, CFG.deferred_context_cache, CFG.error_loop_state):
        p.parent.mkdir(parents=True, exist_ok=True)
