#!/usr/bin/env python3
"""StatusSummarizer: single chokepoint where raw output is normalized."""

from __future__ import annotations

from openkeel.calcifer.contracts import StatusPacket


class StatusSummarizer:
    """Normalize worker output to bounded StatusPackets."""

    MAX_SUMMARY_LINES = 40
    MAX_ACTIONS = 12

    def normalize(self, raw: dict | StatusPacket, step_id: str) -> StatusPacket:
        """Convert raw worker output to a StatusPacket."""
        if isinstance(raw, StatusPacket):
            return self._clip(raw)

        # Raw dict from a direct/semantic/sonnet runner
        return self._clip(
            StatusPacket(
                step_id=step_id,
                objective=raw.get("objective", "unknown"),
                actions_taken=raw.get("actions", []) [: self.MAX_ACTIONS],
                artifacts_touched=raw.get("artifacts", []),
                result_summary=self._trunc(
                    raw.get("summary", ""), self.MAX_SUMMARY_LINES
                ),
                acceptance_checks=raw.get("acceptance_checks", []),
                uncertainties=raw.get("uncertainties", []),
                needs_escalation=bool(
                    raw.get("needs_escalation", False) or raw.get("error")
                ),
                recommended_next_step=raw.get("recommended_next_step"),
                raw_evidence_refs=raw.get("evidence_refs", []),
                runner_id=raw.get("runner_id", ""),
                cost_units=float(raw.get("cost_units", 0.0)),
            )
        )

    def _clip(self, p: StatusPacket) -> StatusPacket:
        """Enforce packet size limits."""
        p.result_summary = self._trunc(p.result_summary, self.MAX_SUMMARY_LINES)
        p.actions_taken = p.actions_taken [: self.MAX_ACTIONS]
        return p

    def _trunc(self, s: str, n: int) -> str:
        """Truncate summary to N lines, with ellipsis."""
        lines = s.splitlines()
        if len(lines) <= n:
            return s
        remaining = len(lines) - n
        return (
            "\n".join(lines[:n])
            + f"\n…[{remaining} more lines; see raw_evidence_refs]"
        )
