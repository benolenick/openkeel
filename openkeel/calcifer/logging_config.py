#!/usr/bin/env python3
"""Comprehensive logging for Calcifer's Ladder.

Logs all key decision points, API calls, intention state, and sub-agent work.
"""

import logging
import json
from pathlib import Path
from datetime import datetime

LOG_DIR = Path("/tmp/calcifer_logs")
LOG_DIR.mkdir(exist_ok=True)

# Main ladder log
LADDER_LOG_FILE = LOG_DIR / f"ladder_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# Separate logs for specific components
GOVERNOR_LOG = LOG_DIR / "governor.log"
INTENTION_LOG = LOG_DIR / "intention.log"
DELEGATION_LOG = LOG_DIR / "delegation.log"


def setup_logging():
    """Configure logging for Calcifer."""
    # Root logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    # Main file handler (everything)
    main_handler = logging.FileHandler(LADDER_LOG_FILE, mode="a")
    main_handler.setLevel(logging.DEBUG)
    main_formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    main_handler.setFormatter(main_formatter)
    root.addHandler(main_handler)

    # Console handler (INFO and above)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    return root


# Specialized loggers
def get_logger(name: str) -> logging.Logger:
    """Get a logger for a component."""
    return logging.getLogger(f"calcifer.{name}")


def log_governor_turn(turn: int, user_input: str, response: str, intention_brief: str = ""):
    """Log a governor turn."""
    logger = get_logger("governor")
    logger.info(f"=== TURN {turn} START ===")
    logger.debug(f"User input: {user_input[:200]}")
    if intention_brief:
        logger.debug(f"Intention briefing:\n{intention_brief}")
    logger.info(f"Governor response ({len(response)} chars): {response[:500]}")
    logger.info(f"=== TURN {turn} END ===\n")


def log_intention_state(packet):
    """Log the full intention packet state."""
    logger = get_logger("intention")
    logger.info("=== INTENTION STATE ===")
    logger.info(f"Goal: {packet.intended_outcome}")
    logger.info(f"Hypotheses: {len(packet.hypothesis_chain)}")
    for h in packet.hypothesis_chain:
        logger.info(f"  v{h.version} ({h.confidence:.0%}): {h.text}")
    logger.info(f"Attempts: {len(packet.attempts)}")
    for a in packet.attempts[-3:]:
        logger.info(f"  • {a['tried']} → {a['result']}")
    if packet.stuck_pattern:
        logger.warning(f"STUCK PATTERN: {packet.stuck_pattern}")
    logger.info(f"Blocker: {packet.blocker}")
    logger.info("=== END STATE ===\n")


def log_delegation(runner: str, task_spec: str, result: str):
    """Log a delegation request and result."""
    logger = get_logger("delegation")
    logger.info(f"Delegating to {runner}")
    logger.debug(f"Task: {task_spec[:200]}")
    logger.info(f"Result ({len(result)} chars): {result[:500]}")


def log_api_call(model: str, prompt: str, response: str, tokens_used: dict = None):
    """Log an API call."""
    logger = get_logger("api")
    logger.info(f"API call to {model}")
    logger.debug(f"Prompt ({len(prompt)} chars): {prompt[:300]}")
    logger.info(f"Response ({len(response)} chars): {response[:300]}")
    if tokens_used:
        logger.info(f"Tokens: {json.dumps(tokens_used)}")


def log_cli_subprocess(cmd: str, returncode: int, stdout: str = "", stderr: str = ""):
    """Log a CLI subprocess call."""
    logger = get_logger("subprocess")
    logger.info(f"CLI command: {' '.join(cmd)}")
    logger.info(f"Return code: {returncode}")
    if stdout:
        logger.debug(f"Stdout ({len(stdout)} chars): {stdout[:500]}")
    if stderr:
        logger.warning(f"Stderr ({len(stderr)} chars): {stderr[:500]}")


def log_hyphae_operation(operation: str, text: str = "", tags: dict = None):
    """Log Hyphae read/write operations."""
    logger = get_logger("hyphae")
    logger.info(f"Hyphae {operation}")
    if text:
        logger.debug(f"Content ({len(text)} chars): {text[:200]}")
    if tags:
        logger.debug(f"Tags: {json.dumps(tags)}")


def dump_full_log():
    """Print the full log file to stdout."""
    if LADDER_LOG_FILE.exists():
        print(f"\n=== CALCIFER LOG ({LADDER_LOG_FILE}) ===\n")
        with open(LADDER_LOG_FILE) as f:
            print(f.read())
        print(f"\n=== END LOG ===\n")


# Initialize on import
setup_logging()
logger = get_logger("init")
logger.info(f"Calcifer logging initialized. Log file: {LADDER_LOG_FILE}")
