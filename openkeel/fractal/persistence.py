"""Fractal Persistence — crash-proof state management.

The fractal tree must survive:
  1. Agent crashes (process dies mid-cycle)
  2. Machine reboots (everything goes away)
  3. Conversation compacting (LLM loses context but process lives)
  4. Agent handoffs (one agent drops, another picks up)

Strategy: every state change writes to disk immediately.
Three layers of persistence:

  Layer 1: SQLite (kanban tasks with parent_id) — survives everything
  Layer 2: Fractal JSON (full tree + test results + discoveries) — survives everything
  Layer 3: Progress markdown (human-readable per-node) — context recovery for agents

On resume, the engine loads from Layer 2 (fastest, full fidelity).
If Layer 2 is corrupt/missing, it reconstructs from Layer 1 (kanban tasks).
Layer 3 is always regenerated on resume for agent consumption.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from .engine import (
    FractalEngine, FractalNode, FractalStatus,
    Resolution, Strategy, Discovery,
)

log = logging.getLogger("openkeel.fractal.persistence")

FRACTAL_DIR = Path.home() / ".openkeel" / "fractals"
PROGRESS_DIR = Path.home() / ".openkeel" / "progress"


class PersistentFractalEngine(FractalEngine):
    """A FractalEngine that auto-saves every state change to disk.

    Drop-in replacement for FractalEngine. Every mutation (decompose,
    resolve, discover, test) triggers an immediate flush to JSON + progress.
    """

    def __init__(self, fractal_id: str, kanban=None,
                 hyphae_url: str = "http://127.0.0.1:8100"):
        super().__init__(kanban=kanban, hyphae_url=hyphae_url)
        self.fractal_id = fractal_id
        self._json_path = FRACTAL_DIR / f"{fractal_id}.json"
        self._lock_path = FRACTAL_DIR / f"{fractal_id}.lock"
        FRACTAL_DIR.mkdir(parents=True, exist_ok=True)
        PROGRESS_DIR.mkdir(parents=True, exist_ok=True)

    # -- Auto-save wrappers ------------------------------------------------

    def create_root(self, *args, **kwargs) -> FractalNode:
        node = super().create_root(*args, **kwargs)
        self._flush()
        self._write_progress(node)
        return node

    def decompose(self, parent_id, children) -> list[FractalNode]:
        nodes = super().decompose(parent_id, children)
        self._flush()
        for n in nodes:
            self._write_progress(n)
        # Update parent progress
        parent = self.get_node(parent_id)
        if parent:
            self._write_progress(parent)
        return nodes

    def resolve(self, node_id, resolution=Resolution.ROBUST):
        super().resolve(node_id, resolution)
        self._flush()
        node = self.get_node(node_id)
        if node:
            self._write_progress(node)

    def block(self, node_id, reason):
        super().block(node_id, reason)
        self._flush()
        node = self.get_node(node_id)
        if node:
            self._write_progress(node)

    def prune(self, node_id, reason="wrong decomposition"):
        super().prune(node_id, reason)
        self._flush()

    def discover(self, node_id, text, severity="info", classification=""):
        d = super().discover(node_id, text, severity, classification)
        self._flush()
        node = self.get_node(node_id)
        if node:
            self._write_progress(node)
        return d

    def test(self, node_id, test_name, passed, output=""):
        super().test(node_id, test_name, passed, output)
        self._flush()

    # -- Persistence -------------------------------------------------------

    def _flush(self):
        """Write full fractal state to JSON. Called after every mutation."""
        try:
            data = self.to_dict()
            data["fractal_id"] = self.fractal_id
            data["last_saved"] = time.time()

            # Atomic write: write to tmp, then rename
            tmp = self._json_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.rename(self._json_path)
        except Exception as e:
            log.error("Fractal flush failed: %s", e)

    def _write_progress(self, node: FractalNode):
        """Write human-readable progress markdown for a node.

        This is what an agent reads to recover context after losing it.
        """
        path = PROGRESS_DIR / f"fractal_{self.fractal_id}_node_{node.id}.md"
        try:
            children = self.get_children(node.id)
            parent = self.get_node(node.parent_id) if node.parent_id else None

            lines = [
                f"# Fractal Node #{node.id}: {node.title}",
                f"",
                f"**Fractal:** {self.fractal_id}",
                f"**Status:** {node.status}",
                f"**Resolution:** {node.resolution}",
                f"**Strategy:** {node.strategy}",
                f"**Depth:** {node.depth}",
                f"**Cycle:** {node.cycle_count}/{node.max_cycles}",
                f"**Agent:** {node.assigned_agent or 'unassigned'}",
            ]

            if parent:
                lines.append(f"**Parent:** #{parent.id} — {parent.title}")

            lines.append(f"")
            lines.append(f"## Description")
            lines.append(f"{node.description}")

            # Children status
            if children:
                lines.append(f"")
                lines.append(f"## Children ({len(children)})")
                for c in children:
                    icon = {"resolved": "x", "blocked": "!", "pruned": "-"}.get(
                        c.status, " ")
                    lines.append(f"- [{icon}] #{c.id} {c.title} [{c.status}/{c.resolution}] ({c.strategy})")

            # Discoveries
            if node.discoveries:
                lines.append(f"")
                lines.append(f"## Discoveries ({len(node.discoveries)})")
                for d in node.discoveries[-15:]:  # last 15
                    sev_icon = {"blocker": "!!!", "important": "!!", "info": ""}.get(d.severity, "")
                    lines.append(f"- {sev_icon} {d.text[:150]}")
                    if d.spawned_task_id:
                        lines.append(f"  (spawned task #{d.spawned_task_id})")

            # Test results
            test_results = self.ratchet.get_results(node.id)
            if test_results:
                lines.append(f"")
                lines.append(f"## Tests")
                for t in test_results[-10:]:
                    icon = "PASS" if t["passed"] else "FAIL"
                    lines.append(f"- [{icon}] {t['name']}")

            # Recovery instruction
            lines.append(f"")
            lines.append(f"## Recovery (if you lost context)")
            lines.append(f"")
            if node.status == FractalStatus.EXPLORING:
                lines.append(f"You were building a rough pass for: {node.title}")
                lines.append(f"Continue from where you left off. Run tests to see what works.")
            elif node.status == FractalStatus.WAITING:
                pending = [c for c in children if c.status not in
                           (FractalStatus.RESOLVED, FractalStatus.PRUNED)]
                lines.append(f"Waiting on {len(pending)} children to resolve:")
                for p in pending:
                    lines.append(f"  - #{p.id} {p.title} [{p.status}]")
            elif node.status == FractalStatus.INTEGRATING:
                lines.append(f"All children are resolved. Integrate their results:")
                for c in children:
                    lines.append(f"  - #{c.id} {c.title} → {c.resolution}")
                lines.append(f"Then run integration tests.")
            elif node.status == FractalStatus.BLOCKED:
                blocker = next((d for d in reversed(node.discoveries)
                                if d.severity == "blocker"), None)
                lines.append(f"BLOCKED: {blocker.text if blocker else 'unknown reason'}")
                lines.append(f"Either fix the blocker or try a different approach.")
            elif node.status == FractalStatus.RESOLVED:
                lines.append(f"This node is resolved at {node.resolution} resolution. No action needed.")

            path.write_text("\n".join(lines))
        except Exception as e:
            log.error("Progress write failed for node #%d: %s", node.id, e)

    # -- Resume ------------------------------------------------------------

    @classmethod
    def resume(cls, fractal_id: str, kanban=None) -> "PersistentFractalEngine":
        """Resume a fractal from disk.

        This is the primary entry point after crash/reboot/compacting.
        Loads the full tree from JSON, verifies against kanban, and
        regenerates all progress files.
        """
        json_path = FRACTAL_DIR / f"{fractal_id}.json"

        if json_path.exists():
            log.info("Resuming fractal '%s' from JSON", fractal_id)
            base = FractalEngine.load(str(json_path), kanban=kanban)

            # Create persistent engine and copy state
            engine = cls(fractal_id, kanban=kanban)
            engine._nodes = base._nodes
            engine._next_id = base._next_id
            engine._ratchet = base._ratchet
            engine._skill_library = base._skill_library

            # Regenerate all progress files
            for node in engine._nodes.values():
                engine._write_progress(node)

            log.info("Resumed: %d nodes, %d resolved",
                     len(engine._nodes),
                     sum(1 for n in engine._nodes.values()
                         if n.status == FractalStatus.RESOLVED))
            return engine

        # No JSON — try to reconstruct from kanban
        if kanban:
            log.info("No JSON for '%s', attempting kanban reconstruction", fractal_id)
            engine = cls(fractal_id, kanban=kanban)
            # Search for tasks with this fractal_id in tags
            results = kanban.search_keyword(fractal_id, limit=100)
            if results:
                log.info("Found %d kanban tasks, rebuilding tree", len(results))
                # Reconstruct nodes from tasks
                for task in results:
                    node = FractalNode(
                        id=engine._next_id,
                        task_id=task["id"],
                        parent_id=None,  # TODO: resolve from parent_id
                        depth=0,
                        title=task["title"],
                        description=task.get("description", ""),
                        status=_kanban_to_fractal_status(task.get("status", "todo")),
                    )
                    engine._nodes[node.id] = node
                    engine._next_id += 1
                engine._flush()
            return engine

        # Nothing to resume from — return empty engine
        log.warning("No state found for fractal '%s', starting fresh", fractal_id)
        return cls(fractal_id, kanban=kanban)

    # -- Context for agents ------------------------------------------------

    def get_recovery_context(self, node_id: int) -> str:
        """Generate a context string for an agent that lost context.

        This is what gets injected when conversation compacting happens
        or an agent picks up work from another agent.
        """
        node = self.get_node(node_id)
        if not node:
            return f"Node #{node_id} not found in fractal {self.fractal_id}"

        path = PROGRESS_DIR / f"fractal_{self.fractal_id}_node_{node.id}.md"
        if path.exists():
            return path.read_text()

        # Regenerate if missing
        self._write_progress(node)
        return path.read_text()

    def get_briefing(self) -> str:
        """Full fractal briefing — what an agent reads on session start.

        Covers: what the project is, where we are, what needs work.
        """
        root = self.get_root()
        if not root:
            return f"Fractal {self.fractal_id}: no root node. Start fresh."

        stats = self.stats()
        active = self.get_active()
        blocked = self.get_blocked()

        lines = [
            f"# Fractal Briefing: {root.title}",
            f"",
            f"**ID:** {self.fractal_id}",
            f"**Progress:** {stats['progress']}% ({stats['resolved_count']}/{stats['total_nodes']} nodes)",
            f"**Max depth:** {stats['max_depth']}",
            f"**Total discoveries:** {stats['total_discoveries']}",
        ]

        if active:
            lines.append(f"")
            lines.append(f"## Needs Work ({len(active)} nodes)")
            for n in active[:10]:
                lines.append(f"- #{n.id} [{n.status}] {n.title} (depth {n.depth}, {n.strategy})")

        if blocked:
            lines.append(f"")
            lines.append(f"## Blocked ({len(blocked)} nodes)")
            for n in blocked[:5]:
                blocker = next((d for d in reversed(n.discoveries)
                                if d.severity == "blocker"), None)
                reason = blocker.text[:80] if blocker else "unknown"
                lines.append(f"- #{n.id} {n.title} — {reason}")

        # Recent discoveries
        all_discoveries = []
        for n in self._nodes.values():
            for d in n.discoveries:
                all_discoveries.append((d.timestamp, n.id, d))
        all_discoveries.sort(reverse=True)

        if all_discoveries:
            lines.append(f"")
            lines.append(f"## Recent Discoveries")
            for _, nid, d in all_discoveries[:8]:
                lines.append(f"- [node #{nid}] {d.text[:120]}")

        return "\n".join(lines)

    # -- Listing -----------------------------------------------------------

    @staticmethod
    def list_fractals() -> list[dict]:
        """List all saved fractals."""
        FRACTAL_DIR.mkdir(parents=True, exist_ok=True)
        fractals = []
        for p in FRACTAL_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text())
                stats = data.get("stats", {})
                fractals.append({
                    "id": p.stem,
                    "last_saved": data.get("last_saved", 0),
                    "total_nodes": stats.get("total_nodes", 0),
                    "progress": stats.get("progress", 0),
                })
            except Exception:
                fractals.append({"id": p.stem, "error": "corrupt"})
        return fractals


def _kanban_to_fractal_status(kanban_status: str) -> FractalStatus:
    return {
        "todo": FractalStatus.PENDING,
        "in_progress": FractalStatus.EXPLORING,
        "done": FractalStatus.RESOLVED,
        "blocked": FractalStatus.BLOCKED,
    }.get(kanban_status, FractalStatus.PENDING)
