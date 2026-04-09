"""Fractal Engine — core infrastructure for recursive task decomposition.

Provides the shared data structures, tree management, test ratchet,
discovery log, and resolution snapshots that all four methods use.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable

log = logging.getLogger("openkeel.fractal")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FractalStatus(StrEnum):
    """Lifecycle of a fractal node."""
    PENDING = "pending"          # Not yet started
    EXPLORING = "exploring"      # Agent is working on rough pass
    TESTING = "testing"          # Running tests against rough pass
    DECOMPOSING = "decomposing"  # Breaking into children
    WAITING = "waiting"          # Waiting for children to resolve
    INTEGRATING = "integrating"  # Combining child results
    RESOLVED = "resolved"        # This node is done
    BLOCKED = "blocked"          # Stuck — needs help or re-approach
    PRUNED = "pruned"            # Abandoned (wrong decomposition)


class Resolution(StrEnum):
    """Quality level of the current implementation."""
    NONE = "none"                # Not started
    SKETCH = "sketch"            # Barely works, happy path only
    PROTOTYPE = "prototype"      # Works for common cases
    ROBUST = "robust"            # Handles edge cases, has error handling
    PRODUCTION = "production"    # Hardened, tested, documented


class Strategy(StrEnum):
    """Which fractal method to use for a branch."""
    ZOOM = "zoom"                # Depth-first, single agent
    SWARM = "swarm"              # Breadth-first, multi-agent
    SPIRAL = "spiral"            # Iterative deepening
    ORGANISM = "organism"        # Adaptive (picks per-branch)
    SKILL_REPLAY = "skill_replay"  # Known pattern from library
    TRIVIAL = "trivial"          # Resolve inline, no decomposition needed


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass
class Discovery:
    """Something learned during a fractal cycle."""
    text: str
    severity: str = "info"       # info | important | blocker
    source_node_id: int | None = None
    timestamp: float = field(default_factory=time.time)
    classification: str = ""     # known | unknown | blocker | trivial
    spawned_task_id: int | None = None  # if this discovery created a child task

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "severity": self.severity,
            "source_node_id": self.source_node_id,
            "timestamp": self.timestamp,
            "classification": self.classification,
            "spawned_task_id": self.spawned_task_id,
        }


# ---------------------------------------------------------------------------
# Test Ratchet
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    """A single test result at a fractal node."""
    name: str
    passed: bool
    output: str = ""
    timestamp: float = field(default_factory=time.time)


class TestRatchet:
    """Once a test passes, it must never regress.

    Each fractal node accumulates tests. When a child resolves and
    bubbles up, the parent re-runs its tests to ensure nothing broke.
    """

    def __init__(self):
        self._results: dict[int, list[TestResult]] = {}  # node_id -> results

    def record(self, node_id: int, test_name: str, passed: bool, output: str = ""):
        if node_id not in self._results:
            self._results[node_id] = []
        self._results[node_id].append(TestResult(
            name=test_name, passed=passed, output=output,
        ))

    def check_regression(self, node_id: int) -> list[str]:
        """Return names of tests that previously passed but now fail."""
        results = self._results.get(node_id, [])
        # Group by test name, check if latest is fail but any previous was pass
        by_name: dict[str, list[TestResult]] = {}
        for r in results:
            by_name.setdefault(r.name, []).append(r)

        regressions = []
        for name, runs in by_name.items():
            ever_passed = any(r.passed for r in runs[:-1])
            latest_failed = not runs[-1].passed
            if ever_passed and latest_failed:
                regressions.append(name)
        return regressions

    def acceptance_met(self, node_id: int) -> bool:
        """True if all recorded tests pass for this node."""
        results = self._results.get(node_id, [])
        if not results:
            return False
        # Check latest result for each test name
        latest: dict[str, bool] = {}
        for r in results:
            latest[r.name] = r.passed
        return all(latest.values())

    def get_results(self, node_id: int) -> list[dict]:
        return [{"name": r.name, "passed": r.passed, "output": r.output[:200],
                 "timestamp": r.timestamp}
                for r in self._results.get(node_id, [])]


# ---------------------------------------------------------------------------
# Fractal Node
# ---------------------------------------------------------------------------

@dataclass
class FractalNode:
    """A node in the fractal task tree.

    Each node represents a unit of work at some depth level.
    Nodes can have children (decomposition), discoveries, and test results.
    """
    id: int
    task_id: int                        # linked kanban task ID
    parent_id: int | None = None        # parent fractal node ID
    depth: int = 0
    title: str = ""
    description: str = ""

    status: FractalStatus = FractalStatus.PENDING
    resolution: Resolution = Resolution.NONE
    strategy: Strategy = Strategy.ZOOM

    # Work tracking
    assigned_agent: str = ""
    cycle_count: int = 0                # how many rough-pass cycles at this node
    max_cycles: int = 5                 # give up after this many attempts
    max_depth: int = 7                  # don't decompose deeper than this

    # Timing
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    resolved_at: float | None = None

    # Discovery + testing
    discoveries: list[Discovery] = field(default_factory=list)
    children: list[int] = field(default_factory=list)  # child node IDs

    # Metadata
    context: dict = field(default_factory=dict)  # arbitrary per-node data
    skill_id: str | None = None         # if resolved via skill replay

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "resolution": self.resolution,
            "strategy": self.strategy,
            "assigned_agent": self.assigned_agent,
            "cycle_count": self.cycle_count,
            "max_cycles": self.max_cycles,
            "max_depth": self.max_depth,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "resolved_at": self.resolved_at,
            "discoveries": [d.to_dict() for d in self.discoveries],
            "children": self.children,
            "context": self.context,
            "skill_id": self.skill_id,
        }


# ---------------------------------------------------------------------------
# Fractal Engine
# ---------------------------------------------------------------------------

class FractalEngine:
    """Manages the fractal task tree and orchestrates method execution.

    The engine is method-agnostic — it provides tree operations, test ratchet,
    discovery logging, and node lifecycle management. The actual decomposition
    strategy is delegated to the method (Zoom, Swarm, Spiral, Organism).
    """

    def __init__(self, kanban=None, hyphae_url: str = "http://127.0.0.1:8100"):
        self._nodes: dict[int, FractalNode] = {}
        self._next_id = 1
        self._ratchet = TestRatchet()
        self._kanban = kanban
        self._hyphae_url = hyphae_url
        self._skill_library: dict[str, dict] = {}  # skill_id -> {pattern, solution}
        self._listeners: list[Callable] = []  # event callbacks

    # -- Tree operations ----------------------------------------------------

    def create_root(self, title: str, description: str = "",
                    strategy: Strategy = Strategy.ZOOM,
                    task_id: int | None = None) -> FractalNode:
        """Create the root node of a new fractal decomposition."""
        # Optionally create a kanban task
        if task_id is None and self._kanban:
            task_id = self._kanban.add_task(
                title=title,
                description=description,
                type="feature",
                board="default",
            )

        node = FractalNode(
            id=self._next_id,
            task_id=task_id or 0,
            depth=0,
            title=title,
            description=description,
            strategy=strategy,
        )
        self._nodes[node.id] = node
        self._next_id += 1
        self._emit("node_created", node)
        log.info("Fractal root created: #%d '%s' [%s]", node.id, title, strategy)
        return node

    def decompose(self, parent_id: int, children: list[dict]) -> list[FractalNode]:
        """Decompose a node into children.

        Args:
            parent_id: The node being decomposed
            children: List of {"title": str, "description": str, "strategy": str (optional)}

        Returns:
            List of created child nodes
        """
        parent = self._nodes.get(parent_id)
        if not parent:
            raise ValueError(f"Node {parent_id} not found")

        if parent.depth >= parent.max_depth:
            log.warning("Max depth reached at node #%d (depth %d)", parent_id, parent.depth)
            parent.status = FractalStatus.BLOCKED
            parent.discoveries.append(Discovery(
                text=f"Max fractal depth ({parent.max_depth}) reached — needs manual intervention",
                severity="blocker",
                source_node_id=parent_id,
            ))
            return []

        created = []
        for i, child_spec in enumerate(children):
            # Create kanban subtask
            child_task_id = None
            if self._kanban:
                child_task_id = self._kanban.add_task(
                    title=child_spec["title"],
                    description=child_spec.get("description", ""),
                    parent_id=parent.task_id,
                    type="task",
                    board="default",
                )

            child = FractalNode(
                id=self._next_id,
                task_id=child_task_id or 0,
                parent_id=parent_id,
                depth=parent.depth + 1,
                title=child_spec["title"],
                description=child_spec.get("description", ""),
                strategy=Strategy(child_spec.get("strategy", parent.strategy)),
                max_depth=parent.max_depth,
                max_cycles=parent.max_cycles,
            )
            self._nodes[child.id] = child
            parent.children.append(child.id)
            self._next_id += 1
            created.append(child)
            self._emit("node_created", child)

        parent.status = FractalStatus.WAITING
        log.info("Node #%d decomposed into %d children at depth %d",
                 parent_id, len(created), parent.depth + 1)
        return created

    def resolve(self, node_id: int, resolution: Resolution = Resolution.ROBUST):
        """Mark a node as resolved. Bubbles up to check if parent can resolve."""
        node = self._nodes.get(node_id)
        if not node:
            return

        node.status = FractalStatus.RESOLVED
        node.resolution = resolution
        node.resolved_at = time.time()

        # Update kanban
        if self._kanban and node.task_id:
            self._kanban.move(node.task_id, "done")

        self._emit("node_resolved", node)
        log.info("Node #%d resolved [%s]", node_id, resolution)

        # Check if parent can resolve
        if node.parent_id:
            self._check_parent_resolution(node.parent_id)

    def block(self, node_id: int, reason: str):
        """Mark a node as blocked."""
        node = self._nodes.get(node_id)
        if not node:
            return
        node.status = FractalStatus.BLOCKED
        node.discoveries.append(Discovery(
            text=reason, severity="blocker", source_node_id=node_id,
        ))
        if self._kanban and node.task_id:
            self._kanban.move(node.task_id, "blocked")
        self._emit("node_blocked", node)

    def prune(self, node_id: int, reason: str = "wrong decomposition"):
        """Abandon a node and its subtree."""
        node = self._nodes.get(node_id)
        if not node:
            return
        node.status = FractalStatus.PRUNED
        # Recursively prune children
        for child_id in node.children:
            self.prune(child_id, reason)
        self._emit("node_pruned", node)

    def _check_parent_resolution(self, parent_id: int):
        """Check if all children of a parent are resolved."""
        parent = self._nodes.get(parent_id)
        if not parent:
            return

        children = [self._nodes[cid] for cid in parent.children if cid in self._nodes]
        active = [c for c in children if c.status != FractalStatus.PRUNED]

        if all(c.status == FractalStatus.RESOLVED for c in active):
            # All children done — parent moves to integration
            parent.status = FractalStatus.INTEGRATING
            self._emit("node_integrating", parent)
            log.info("Node #%d: all children resolved, ready for integration", parent_id)

    # -- Discovery ----------------------------------------------------------

    def discover(self, node_id: int, text: str, severity: str = "info",
                 classification: str = "") -> Discovery:
        """Log a discovery at a node."""
        node = self._nodes.get(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        d = Discovery(
            text=text,
            severity=severity,
            source_node_id=node_id,
            classification=classification,
        )
        node.discoveries.append(d)

        # Save to Hyphae if important
        if severity in ("important", "blocker"):
            self._remember(f"Fractal discovery at depth {node.depth} [{node.title}]: {text}")

        self._emit("discovery", d)
        return d

    # -- Test ratchet -------------------------------------------------------

    @property
    def ratchet(self) -> TestRatchet:
        return self._ratchet

    def test(self, node_id: int, test_name: str, passed: bool, output: str = ""):
        """Record a test result and check for regressions."""
        self._ratchet.record(node_id, test_name, passed, output)
        regressions = self._ratchet.check_regression(node_id)
        if regressions:
            node = self._nodes.get(node_id)
            if node:
                node.discoveries.append(Discovery(
                    text=f"REGRESSION: {', '.join(regressions)}",
                    severity="blocker",
                    source_node_id=node_id,
                ))
            log.warning("Regression at node #%d: %s", node_id, regressions)

    # -- Skill library ------------------------------------------------------

    def register_skill(self, skill_id: str, pattern: str, solution: str,
                       metadata: dict | None = None):
        """Register a reusable solution pattern."""
        self._skill_library[skill_id] = {
            "pattern": pattern,
            "solution": solution,
            "metadata": metadata or {},
            "use_count": 0,
            "created_at": time.time(),
        }
        self._remember(f"Skill registered: {skill_id} — {pattern}")

    def find_skill(self, problem_description: str) -> dict | None:
        """Find a matching skill for a problem. Returns None if no match."""
        desc_lower = problem_description.lower()
        best = None
        best_score = 0
        for skill_id, skill in self._skill_library.items():
            pattern_words = set(skill["pattern"].lower().split())
            desc_words = set(desc_lower.split())
            overlap = len(pattern_words & desc_words)
            score = overlap / max(len(pattern_words), 1)
            if score > best_score and score > 0.3:
                best = {**skill, "id": skill_id}
                best_score = score
        return best

    def replay_skill(self, node_id: int, skill_id: str):
        """Apply a known skill to resolve a node."""
        node = self._nodes.get(node_id)
        skill = self._skill_library.get(skill_id)
        if not node or not skill:
            return
        node.skill_id = skill_id
        skill["use_count"] += 1
        node.discoveries.append(Discovery(
            text=f"Resolved via skill replay: {skill_id}",
            severity="info",
            source_node_id=node_id,
            classification="known",
        ))
        self._emit("skill_replayed", node)

    # -- Tree queries -------------------------------------------------------

    def get_node(self, node_id: int) -> FractalNode | None:
        return self._nodes.get(node_id)

    def get_root(self) -> FractalNode | None:
        """Get the root node (depth 0)."""
        for node in self._nodes.values():
            if node.depth == 0:
                return node
        return None

    def get_children(self, node_id: int) -> list[FractalNode]:
        node = self._nodes.get(node_id)
        if not node:
            return []
        return [self._nodes[cid] for cid in node.children if cid in self._nodes]

    def get_leaves(self) -> list[FractalNode]:
        """Get all leaf nodes (no children)."""
        return [n for n in self._nodes.values() if not n.children]

    def get_active(self) -> list[FractalNode]:
        """Get all nodes that need work."""
        active_statuses = {FractalStatus.PENDING, FractalStatus.EXPLORING,
                           FractalStatus.TESTING, FractalStatus.INTEGRATING}
        return [n for n in self._nodes.values() if n.status in active_statuses]

    def get_blocked(self) -> list[FractalNode]:
        return [n for n in self._nodes.values() if n.status == FractalStatus.BLOCKED]

    def tree_view(self) -> dict:
        """Full fractal tree as nested dict for visualization."""
        roots = [n for n in self._nodes.values() if n.parent_id is None]
        if not roots:
            return {}
        return self._build_tree(roots[0])

    def _build_tree(self, node: FractalNode) -> dict:
        d = node.to_dict()
        d["test_results"] = self._ratchet.get_results(node.id)
        d["children_nodes"] = [
            self._build_tree(self._nodes[cid])
            for cid in node.children if cid in self._nodes
        ]
        return d

    # -- Stats --------------------------------------------------------------

    def stats(self) -> dict:
        """Aggregate statistics about the fractal."""
        nodes = list(self._nodes.values())
        if not nodes:
            return {"total_nodes": 0}

        by_status = {}
        by_depth = {}
        by_strategy = {}
        total_discoveries = 0
        total_cycles = 0

        for n in nodes:
            by_status[n.status] = by_status.get(n.status, 0) + 1
            by_depth[n.depth] = by_depth.get(n.depth, 0) + 1
            by_strategy[n.strategy] = by_strategy.get(n.strategy, 0) + 1
            total_discoveries += len(n.discoveries)
            total_cycles += n.cycle_count

        resolved = [n for n in nodes if n.status == FractalStatus.RESOLVED]
        max_depth = max(n.depth for n in nodes)

        return {
            "total_nodes": len(nodes),
            "max_depth": max_depth,
            "by_status": {str(k): v for k, v in by_status.items()},
            "by_depth": by_depth,
            "by_strategy": {str(k): v for k, v in by_strategy.items()},
            "total_discoveries": total_discoveries,
            "total_cycles": total_cycles,
            "resolved_count": len(resolved),
            "progress": round(len(resolved) / len(nodes) * 100) if nodes else 0,
            "skills_available": len(self._skill_library),
        }

    # -- Events -------------------------------------------------------------

    def on(self, callback: Callable):
        """Register an event listener."""
        self._listeners.append(callback)

    def _emit(self, event: str, data: Any):
        for cb in self._listeners:
            try:
                cb(event, data)
            except Exception:
                pass

    # -- Hyphae integration -------------------------------------------------

    def _remember(self, text: str):
        """Save to Hyphae."""
        try:
            import urllib.request
            data = json.dumps({"text": text, "source": "fractal"}).encode()
            req = urllib.request.Request(
                f"{self._hyphae_url}/remember",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            pass

    def _recall(self, query: str) -> list[dict]:
        """Search Hyphae."""
        try:
            import urllib.request
            data = json.dumps({"query": query, "top_k": 5}).encode()
            req = urllib.request.Request(
                f"{self._hyphae_url}/recall",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=3)
            return json.loads(resp.read()).get("results", [])
        except Exception:
            return []

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "nodes": {nid: n.to_dict() for nid, n in self._nodes.items()},
            "skills": self._skill_library,
            "stats": self.stats(),
        }

    def save(self, path: str):
        """Persist the fractal to disk."""
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)

    @classmethod
    def load(cls, path: str, kanban=None) -> "FractalEngine":
        """Load a fractal from disk."""
        with open(path) as f:
            data = json.load(f)
        engine = cls(kanban=kanban)
        for nid_str, nd in data.get("nodes", {}).items():
            node = FractalNode(
                id=nd["id"],
                task_id=nd["task_id"],
                parent_id=nd.get("parent_id"),
                depth=nd["depth"],
                title=nd["title"],
                description=nd.get("description", ""),
                status=FractalStatus(nd["status"]),
                resolution=Resolution(nd.get("resolution", "none")),
                strategy=Strategy(nd.get("strategy", "zoom")),
                assigned_agent=nd.get("assigned_agent", ""),
                cycle_count=nd.get("cycle_count", 0),
                max_cycles=nd.get("max_cycles", 5),
                max_depth=nd.get("max_depth", 7),
                created_at=nd.get("created_at", 0),
                started_at=nd.get("started_at"),
                resolved_at=nd.get("resolved_at"),
                children=nd.get("children", []),
                context=nd.get("context", {}),
                skill_id=nd.get("skill_id"),
            )
            for dd in nd.get("discoveries", []):
                node.discoveries.append(Discovery(**dd))
            engine._nodes[node.id] = node
        engine._next_id = max(engine._nodes.keys(), default=0) + 1
        engine._skill_library = data.get("skills", {})
        return engine
