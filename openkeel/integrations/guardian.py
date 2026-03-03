"""Guardian model integration — external safety check via Ollama API.

Queries a safety-classification model (e.g. granite3.3-guardian:8b) to get
a risk assessment of a command before execution.  Used for GATED commands
that passed scope checks but need an extra LLM-based sanity check.

The model is asked to classify the command as safe/unsafe given optional
context (engagement description, scope constraints).  The response is
parsed for a binary allow/deny decision and an explanation.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openkeel.core.profile import GuardianConfig

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
You are a security operations safety checker. Evaluate whether the following \
shell command is safe to execute in the given context.

Context: {context}

Command: {command}

Respond with EXACTLY one line in this format:
SAFE: <reason>
or
UNSAFE: <reason>

Do not include any other text."""


class GuardianClient:
    """Query an Ollama-hosted guardian model for command safety checks."""

    def __init__(self, config: GuardianConfig):
        self.endpoint = config.endpoint.rstrip("/")
        self.model = config.model
        self.timeout = config.timeout
        self.context = config.context

    def check_command(self, command: str) -> tuple[bool, str]:
        """Check a command for safety.

        Returns:
            (allowed, explanation) — allowed=True means safe to proceed.
        """
        prompt = _PROMPT_TEMPLATE.format(
            context=self.context or "Authorized penetration test / CTF engagement",
            command=command,
        )

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }

        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            response_text = data.get("response", "").strip()
            return self._parse_response(response_text)

        except urllib.error.URLError as exc:
            logger.warning("Guardian: connection failed: %s", exc)
            # Fail open — if guardian is unreachable, allow the command
            return True, f"Guardian unreachable: {exc}"
        except Exception as exc:
            logger.warning("Guardian: unexpected error: %s", exc)
            return True, f"Guardian error: {exc}"

    def _parse_response(self, text: str) -> tuple[bool, str]:
        """Parse the guardian model's response into (allowed, explanation)."""
        # Look for SAFE: or UNSAFE: prefix in the response
        for line in text.splitlines():
            line = line.strip()
            upper = line.upper()
            if upper.startswith("SAFE:"):
                return True, line[5:].strip()
            if upper.startswith("UNSAFE:"):
                return False, line[7:].strip()

        # If the model didn't follow the format, check for keywords
        upper_text = text.upper()
        if "UNSAFE" in upper_text or "DENY" in upper_text or "BLOCK" in upper_text:
            return False, text[:200]

        # Default: allow (fail open) with the raw response as explanation
        logger.warning("Guardian: could not parse response, failing open: %s", text[:100])
        return True, f"Unparseable response (failing open): {text[:200]}"
