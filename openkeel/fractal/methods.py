"""Fractal decomposition methods — four strategies for attacking problems.

Each method implements the same interface but decomposes differently:

  ZoomMethod     — Depth-first. Dive deep, one branch at a time.
  SwarmMethod    — Breadth-first. Fan out agents across branches.
  SpiralMethod   — Iterative deepening. Full passes at increasing resolution.
  OrganismMethod — Adaptive. Classifies branches and picks the best strategy.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import (
    FractalEngine, FractalNode, FractalStatus,
    Resolution, Strategy, Discovery,
)

log = logging.getLogger("openkeel.fractal.methods")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class FractalMethod(ABC):
    """Base interface for all fractal methods.

    A method defines HOW to decompose and execute — the engine handles
    the tree structure, test ratchet, and discovery logging.
    """

    name: str = "base"
    description: str = ""

    def __init__(self, engine: FractalEngine, agent_fn: Callable | None = None):
        """
        Args:
            engine: The fractal engine managing the tree
            agent_fn: Callable(instruction: str, context: dict) -> dict
                     The function that actually does work (calls an LLM agent).
                     Returns {"output": str, "discoveries": list[str], "tests": list[dict]}
        """
        self.engine = engine
        self.agent_fn = agent_fn

    @abstractmethod
    def execute(self, node: FractalNode) -> FractalNode:
        """Execute one fractal cycle on a node.

        This is the core loop: understand → rough pass → test → discover → decompose.
        Returns the updated node.
        """
        ...

    @abstractmethod
    def next_node(self) -> FractalNode | None:
        """Pick the next node to work on. Returns None if no work available."""
        ...

    def run(self, root_id: int | None = None, max_iterations: int = 100) -> dict:
        """Run the fractal method until completion or max iterations.

        Returns execution summary.
        """
        iterations = 0
        start = time.time()

        while iterations < max_iterations:
            node = self.next_node()
            if node is None:
                break

            log.info("[%s] Iteration %d: working on #%d '%s' (depth %d)",
                     self.name, iterations, node.id, node.title, node.depth)

            self.execute(node)
            iterations += 1

        root = self.engine.get_root()
        elapsed = time.time() - start

        return {
            "method": self.name,
            "iterations": iterations,
            "elapsed_seconds": round(elapsed, 1),
            "root_status": root.status if root else "none",
            "root_resolution": root.resolution if root else "none",
            "stats": self.engine.stats(),
        }


# ---------------------------------------------------------------------------
# Method 1: Zoom — Depth-First Single Agent
# ---------------------------------------------------------------------------

class ZoomMethod(FractalMethod):
    """Depth-first decomposition. One agent dives deep into one branch at a time.

    Best for:
    - Problems that need deep investigation (reverse engineering, debugging)
    - When you need to fully understand one part before moving on
    - Small teams / single agent scenarios
    - Problems where branches are tightly coupled

    The agent follows the leftmost unresolved branch all the way down,
    then backtracks when it resolves or gets blocked.
    """

    name = "zoom"
    description = "Depth-first. One agent goes deep on one branch at a time."

    def next_node(self) -> FractalNode | None:
        """Depth-first: pick the deepest pending/exploring node."""
        active = self.engine.get_active()
        if not active:
            return None
        # Prioritize: deepest first, then by creation order
        active.sort(key=lambda n: (-n.depth, n.created_at))
        return active[0]

    def execute(self, node: FractalNode) -> FractalNode:
        """One zoom cycle: rough pass → test → discover → decompose or resolve."""
        node.cycle_count += 1

        if node.cycle_count > node.max_cycles:
            self.engine.block(node.id, f"Max cycles ({node.max_cycles}) exhausted")
            return node

        # Check if a skill matches
        skill = self.engine.find_skill(f"{node.title} {node.description}")
        if skill:
            log.info("Zoom: skill match for #%d: %s", node.id, skill["id"])
            self.engine.replay_skill(node.id, skill["id"])
            self.engine.resolve(node.id, Resolution.ROBUST)
            return node

        # Phase 1: Explore (rough pass)
        if node.status in (FractalStatus.PENDING, FractalStatus.EXPLORING):
            node.status = FractalStatus.EXPLORING
            if not node.started_at:
                node.started_at = time.time()

            if self.agent_fn:
                result = self.agent_fn(
                    f"Build a rough working version of: {node.title}\n\n{node.description}",
                    {"depth": node.depth, "cycle": node.cycle_count,
                     "parent_context": self._parent_context(node)},
                )
                # Process agent output
                for disc_text in result.get("discoveries", []):
                    self.engine.discover(node.id, disc_text, severity="important")
                for test in result.get("tests", []):
                    self.engine.test(node.id, test["name"], test["passed"],
                                     test.get("output", ""))

        # Phase 2: Test
        node.status = FractalStatus.TESTING
        if self.engine.ratchet.acceptance_met(node.id):
            self.engine.resolve(node.id, self._cycle_to_resolution(node.cycle_count))
            return node

        # Phase 3: Discover + Decompose
        blockers = [d for d in node.discoveries if d.severity == "blocker"]
        unknowns = [d for d in node.discoveries
                     if d.classification in ("unknown", "") and not d.spawned_task_id]

        if unknowns and node.depth < node.max_depth:
            # Decompose: create children for unresolved discoveries
            children_specs = []
            for disc in unknowns:
                children_specs.append({
                    "title": disc.text[:120],
                    "description": f"Discovered during zoom into: {node.title}\n{disc.text}",
                    "strategy": "zoom",
                })
                disc.classification = "decomposed"
            if children_specs:
                created = self.engine.decompose(node.id, children_specs)
                for child, disc in zip(created, unknowns):
                    disc.spawned_task_id = child.task_id

        elif blockers:
            self.engine.block(node.id, blockers[0].text)

        return node

    def _parent_context(self, node: FractalNode) -> str:
        if not node.parent_id:
            return ""
        parent = self.engine.get_node(node.parent_id)
        if not parent:
            return ""
        return f"Parent task: {parent.title}\nParent discoveries: " + \
               "; ".join(d.text[:80] for d in parent.discoveries[:5])

    def _cycle_to_resolution(self, cycle: int) -> Resolution:
        if cycle <= 1:
            return Resolution.SKETCH
        elif cycle <= 2:
            return Resolution.PROTOTYPE
        elif cycle <= 3:
            return Resolution.ROBUST
        return Resolution.PRODUCTION


# ---------------------------------------------------------------------------
# Method 2: Swarm — Breadth-First Multi-Agent
# ---------------------------------------------------------------------------

@dataclass
class SwarmAgent:
    """Tracks an agent working on a branch in the swarm."""
    name: str
    node_id: int
    status: str = "idle"  # idle | working | done
    started_at: float = 0.0


class SwarmMethod(FractalMethod):
    """Breadth-first decomposition. Fan out agents across branches.

    Best for:
    - Problems with clearly independent sub-tasks
    - When you have multiple agents available
    - Build tasks (frontend + backend + infra in parallel)
    - When speed matters more than deep understanding

    A coordinator decomposes at each level, assigns agents to branches,
    waits for all to resolve, then integrates and moves up.
    """

    name = "swarm"
    description = "Breadth-first. Fan out parallel agents across branches."

    def __init__(self, engine: FractalEngine, agent_fn: Callable | None = None,
                 max_parallel: int = 4):
        super().__init__(engine, agent_fn)
        self.max_parallel = max_parallel
        self._agents: dict[str, SwarmAgent] = {}
        self._dispatch_fn: Callable | None = None  # for dispatching to real agents

    def set_dispatch(self, fn: Callable):
        """Set the function that dispatches work to real agents.
        fn(agent_name: str, instruction: str, context: dict) -> None
        """
        self._dispatch_fn = fn

    def next_node(self) -> FractalNode | None:
        """Breadth-first: pick shallowest pending node, or integrating node."""
        # Priority: integrating nodes first (need to combine results)
        integrating = [n for n in self.engine._nodes.values()
                       if n.status == FractalStatus.INTEGRATING]
        if integrating:
            return min(integrating, key=lambda n: n.depth)

        # Then shallowest pending/exploring
        active = self.engine.get_active()
        if not active:
            return None
        active.sort(key=lambda n: (n.depth, n.created_at))
        return active[0]

    def execute(self, node: FractalNode) -> FractalNode:
        """Swarm cycle: decompose broadly, assign agents, wait, integrate."""
        node.cycle_count += 1

        # Integration phase: all children resolved
        if node.status == FractalStatus.INTEGRATING:
            return self._integrate(node)

        # Exploration phase
        if node.status in (FractalStatus.PENDING, FractalStatus.EXPLORING):
            node.status = FractalStatus.EXPLORING
            if not node.started_at:
                node.started_at = time.time()

            # Coordinator does initial analysis
            if self.agent_fn:
                result = self.agent_fn(
                    f"Analyze and decompose into independent sub-tasks: {node.title}\n\n"
                    f"{node.description}\n\n"
                    f"Return a list of independent sub-tasks that can be worked on in parallel.",
                    {"depth": node.depth, "mode": "decompose"},
                )

                # Create children from analysis
                subtasks = result.get("subtasks", [])
                if not subtasks:
                    # Agent couldn't decompose — treat as leaf, try to resolve directly
                    for test in result.get("tests", []):
                        self.engine.test(node.id, test["name"], test["passed"])
                    if self.engine.ratchet.acceptance_met(node.id):
                        self.engine.resolve(node.id)
                    return node

                children_specs = [
                    {"title": st.get("title", st.get("text", "subtask")),
                     "description": st.get("description", ""),
                     "strategy": "swarm" if len(subtasks) > 2 else "zoom"}
                    for st in subtasks[:self.max_parallel]
                ]
                created = self.engine.decompose(node.id, children_specs)

                # Dispatch to agents
                for i, child in enumerate(created):
                    agent_name = f"swarm-{node.id}-{i}"
                    child.assigned_agent = agent_name
                    self._agents[agent_name] = SwarmAgent(
                        name=agent_name, node_id=child.id,
                        status="working", started_at=time.time(),
                    )
                    if self._dispatch_fn:
                        self._dispatch_fn(agent_name,
                                          f"Work on: {child.title}\n{child.description}",
                                          {"fractal_node_id": child.id})

        return node

    def _integrate(self, node: FractalNode) -> FractalNode:
        """Combine results from all children."""
        children = self.engine.get_children(node.id)

        if self.agent_fn:
            child_summaries = []
            for c in children:
                summary = f"[{c.status}] {c.title}"
                if c.discoveries:
                    summary += f" — learned: {c.discoveries[-1].text[:80]}"
                child_summaries.append(summary)

            result = self.agent_fn(
                f"Integrate results for: {node.title}\n\n"
                f"Sub-task results:\n" + "\n".join(child_summaries),
                {"depth": node.depth, "mode": "integrate"},
            )

            for test in result.get("tests", []):
                self.engine.test(node.id, test["name"], test["passed"])

        if self.engine.ratchet.acceptance_met(node.id):
            self.engine.resolve(node.id, Resolution.ROBUST)
        else:
            # Integration failed — discoveries from children may help
            regressions = self.engine.ratchet.check_regression(node.id)
            if regressions:
                self.engine.discover(node.id,
                                     f"Integration regression: {', '.join(regressions)}",
                                     severity="blocker")
                self.engine.block(node.id, "Integration failed with regressions")

        return node

    def get_agent_status(self) -> list[dict]:
        return [{"name": a.name, "node_id": a.node_id, "status": a.status,
                 "started_at": a.started_at}
                for a in self._agents.values()]


# ---------------------------------------------------------------------------
# Method 3: Spiral — Iterative Deepening
# ---------------------------------------------------------------------------

@dataclass
class SpiralPass:
    """Record of one complete pass over the problem."""
    number: int
    resolution: Resolution
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    discoveries: list[str] = field(default_factory=list)
    tests_added: int = 0
    tests_passed: int = 0
    focus_areas: list[str] = field(default_factory=list)  # what this pass focused on


class SpiralMethod(FractalMethod):
    """Iterative deepening. Multiple passes over the whole problem at increasing resolution.

    Best for:
    - Greenfield projects where you don't know what you don't know
    - When you always want a working version at every stage
    - Exploratory work (research, prototyping)
    - When stopping early is acceptable ("prototype is good enough")

    Each pass covers the full surface. The test ratchet ensures you never
    regress. Discoveries from each pass feed focus areas for the next.
    """

    name = "spiral"
    description = "Iterative deepening. Full passes at increasing resolution."

    RESOLUTION_SEQUENCE = [
        Resolution.SKETCH,
        Resolution.PROTOTYPE,
        Resolution.ROBUST,
        Resolution.PRODUCTION,
    ]

    def __init__(self, engine: FractalEngine, agent_fn: Callable | None = None,
                 target_resolution: Resolution = Resolution.PRODUCTION):
        super().__init__(engine, agent_fn)
        self.target_resolution = target_resolution
        self.passes: list[SpiralPass] = []
        self._current_pass: int = 0

    def next_node(self) -> FractalNode | None:
        """Spiral always works on the root node (whole problem per pass)."""
        root = self.engine.get_root()
        if not root:
            return None
        if root.status == FractalStatus.RESOLVED:
            return None
        # Check if we've reached target resolution
        if root.resolution == self.target_resolution:
            return None
        return root

    def execute(self, node: FractalNode) -> FractalNode:
        """One spiral pass: work the whole problem at the current resolution level."""
        node.cycle_count += 1
        node.status = FractalStatus.EXPLORING
        if not node.started_at:
            node.started_at = time.time()

        # Determine current resolution target
        res_idx = min(self._current_pass, len(self.RESOLUTION_SEQUENCE) - 1)
        target_res = self.RESOLUTION_SEQUENCE[res_idx]

        # Create pass record
        current_pass = SpiralPass(
            number=self._current_pass,
            resolution=target_res,
            focus_areas=self._compute_focus_areas(node),
        )

        log.info("Spiral pass %d → %s (focus: %s)",
                 current_pass.number, target_res,
                 ", ".join(current_pass.focus_areas) or "full surface")

        if self.agent_fn:
            focus_str = ""
            if current_pass.focus_areas:
                focus_str = f"\n\nFocus areas (from previous discoveries):\n" + \
                            "\n".join(f"- {a}" for a in current_pass.focus_areas)

            prev_pass_str = ""
            if self.passes:
                last = self.passes[-1]
                prev_pass_str = f"\n\nPrevious pass ({last.resolution}) discovered:\n" + \
                                "\n".join(f"- {d}" for d in last.discoveries[-10:])

            instruction = (
                f"Pass {current_pass.number + 1} at {target_res} resolution.\n\n"
                f"Task: {node.title}\n{node.description}\n\n"
                f"Resolution guide:\n"
                f"  sketch = barely works, happy path only\n"
                f"  prototype = works for common cases\n"
                f"  robust = handles edge cases, has error handling\n"
                f"  production = hardened, tested, documented\n\n"
                f"Current target: {target_res}"
                f"{focus_str}{prev_pass_str}\n\n"
                f"Build/improve the implementation to {target_res} level. "
                f"Report what you built, what tests pass, and what you discovered."
            )

            result = self.agent_fn(instruction, {
                "depth": node.depth,
                "pass_number": current_pass.number,
                "target_resolution": target_res,
            })

            # Process results
            for disc_text in result.get("discoveries", []):
                self.engine.discover(node.id, disc_text, severity="important")
                current_pass.discoveries.append(disc_text)

            for test in result.get("tests", []):
                self.engine.test(node.id, test["name"], test["passed"],
                                 test.get("output", ""))
                current_pass.tests_added += 1
                if test["passed"]:
                    current_pass.tests_passed += 1

        # Evaluate pass
        current_pass.completed_at = time.time()
        self.passes.append(current_pass)

        regressions = self.engine.ratchet.check_regression(node.id)
        if regressions:
            self.engine.discover(node.id,
                                 f"REGRESSION in pass {current_pass.number}: {', '.join(regressions)}",
                                 severity="blocker")
            # Don't advance resolution — fix regressions first
            return node

        # Check if this pass succeeded
        if self.engine.ratchet.acceptance_met(node.id):
            node.resolution = target_res
            if target_res == self.target_resolution:
                self.engine.resolve(node.id, target_res)
            else:
                self._current_pass += 1
                log.info("Spiral: pass %d complete, advancing to %s",
                         current_pass.number,
                         self.RESOLUTION_SEQUENCE[min(self._current_pass,
                                                      len(self.RESOLUTION_SEQUENCE) - 1)])
        else:
            # Pass didn't meet acceptance — retry at same resolution
            log.info("Spiral: pass %d didn't meet acceptance, retrying %s",
                     current_pass.number, target_res)

        return node

    def _compute_focus_areas(self, node: FractalNode) -> list[str]:
        """Determine what this pass should focus on based on discoveries."""
        if not self.passes:
            return []  # First pass: cover everything

        # Gather unresolved discoveries
        focus = []
        for disc in node.discoveries:
            if disc.severity in ("important", "blocker") and not disc.spawned_task_id:
                focus.append(disc.text[:100])

        # Also focus on failed tests
        regressions = self.engine.ratchet.check_regression(node.id)
        for reg in regressions:
            focus.append(f"Fix regression: {reg}")

        return focus[:8]

    def get_pass_history(self) -> list[dict]:
        return [
            {
                "number": p.number,
                "resolution": p.resolution,
                "started_at": p.started_at,
                "completed_at": p.completed_at,
                "discoveries": len(p.discoveries),
                "tests_added": p.tests_added,
                "tests_passed": p.tests_passed,
                "focus_areas": p.focus_areas,
            }
            for p in self.passes
        ]


# ---------------------------------------------------------------------------
# Method 4: Organism — Adaptive Hybrid
# ---------------------------------------------------------------------------

class BranchClassification:
    """How the Organism classifies a branch."""
    KNOWN = "known"              # SkillLibrary has a solution → replay
    TRIVIAL = "trivial"          # Simple enough to resolve inline
    INDEPENDENT = "independent"  # No deps on siblings → swarm
    DEEP = "deep"                # Tightly coupled, needs investigation → zoom
    UNKNOWN = "unknown"          # Can't classify → spiral another pass


class OrganismMethod(FractalMethod):
    """Adaptive hybrid. Classifies branches and picks the best strategy per branch.

    Best for:
    - Large, complex projects with mixed sub-problems
    - When you have both simple and hard parts
    - Long-running projects where patterns emerge over time
    - When the SkillLibrary has been trained by previous jobs

    The Organism does a Spiral pass to discover the shape, then classifies
    each branch and assigns the optimal method. It continuously re-evaluates
    as new information emerges.
    """

    name = "organism"
    description = "Adaptive. Classifies branches and picks the best strategy."

    def __init__(self, engine: FractalEngine, agent_fn: Callable | None = None,
                 classify_fn: Callable | None = None):
        super().__init__(engine, agent_fn)
        # Custom classifier, or use default heuristic
        self._classify_fn = classify_fn or self._default_classify
        self._sub_methods: dict[int, FractalMethod] = {}  # node_id -> active method
        self._classifications: dict[int, str] = {}  # node_id -> classification

    def next_node(self) -> FractalNode | None:
        """Pick the highest-priority node based on adaptive scoring."""
        active = self.engine.get_active()
        if not active:
            return None

        # Score nodes: blocked children get priority (unblock first),
        # then integrating, then by depth/urgency
        def score(n: FractalNode) -> tuple:
            blocked_children = sum(
                1 for cid in n.children
                if cid in self.engine._nodes and
                self.engine._nodes[cid].status == FractalStatus.BLOCKED
            )
            is_integrating = 1 if n.status == FractalStatus.INTEGRATING else 0
            stall_score = n.cycle_count / max(n.max_cycles, 1)
            return (-is_integrating, -blocked_children, stall_score, n.depth)

        active.sort(key=score)
        return active[0]

    def execute(self, node: FractalNode) -> FractalNode:
        """Adaptive cycle: classify, pick method, execute."""
        node.cycle_count += 1
        if not node.started_at:
            node.started_at = time.time()

        # Phase 1: Initial exploration (if no children yet)
        if not node.children and node.status != FractalStatus.INTEGRATING:
            return self._explore_and_classify(node)

        # Phase 2: Integration (if all children done)
        if node.status == FractalStatus.INTEGRATING:
            return self._integrate(node)

        # Phase 3: Re-evaluate stuck branches
        for child_id in node.children:
            child = self.engine.get_node(child_id)
            if child and child.status == FractalStatus.BLOCKED:
                self._reclassify_and_retry(child)

        return node

    def _explore_and_classify(self, node: FractalNode) -> FractalNode:
        """Do a shallow exploration, then classify each discovered sub-problem."""
        node.status = FractalStatus.EXPLORING

        # Use agent to analyze the problem
        if self.agent_fn:
            result = self.agent_fn(
                f"Analyze this problem and break it into sub-problems. "
                f"For each sub-problem, assess:\n"
                f"- Is it a KNOWN pattern (common, well-understood)?\n"
                f"- Is it TRIVIAL (one-liner, obvious solution)?\n"
                f"- Is it INDEPENDENT (no deps on other sub-problems)?\n"
                f"- Is it DEEP (requires investigation, tightly coupled)?\n"
                f"- Is it UNKNOWN (need more exploration to understand)?\n\n"
                f"Problem: {node.title}\n{node.description}",
                {"depth": node.depth, "mode": "classify"},
            )

            subtasks = result.get("subtasks", [])
            if not subtasks:
                # Can't decompose — resolve directly
                self.engine.resolve(node.id, Resolution.SKETCH)
                return node

            children_specs = []
            for st in subtasks:
                classification = self._classify_fn(st, node)
                strategy = self._classification_to_strategy(classification)
                children_specs.append({
                    "title": st.get("title", "subtask"),
                    "description": st.get("description", ""),
                    "strategy": strategy,
                })

            created = self.engine.decompose(node.id, children_specs)

            # Set up sub-methods for each child
            for child in created:
                self._classifications[child.id] = child.strategy
                self._setup_sub_method(child)

                # Immediate resolution for trivial/known
                if child.strategy == Strategy.TRIVIAL:
                    self.engine.resolve(child.id, Resolution.PRODUCTION)
                elif child.strategy == Strategy.SKILL_REPLAY:
                    skill = self.engine.find_skill(child.title + " " + child.description)
                    if skill:
                        self.engine.replay_skill(child.id, skill["id"])
                        self.engine.resolve(child.id, Resolution.ROBUST)

        return node

    def _integrate(self, node: FractalNode) -> FractalNode:
        """Combine results, checking for cross-branch issues."""
        children = self.engine.get_children(node.id)

        if self.agent_fn:
            child_info = []
            for c in children:
                info = f"[{c.status}/{c.resolution}] {c.title} (strategy: {c.strategy})"
                if c.discoveries:
                    info += f"\n  Learned: {c.discoveries[-1].text[:100]}"
                child_info.append(info)

            result = self.agent_fn(
                f"Integrate all sub-problem solutions for: {node.title}\n\n"
                f"Results:\n" + "\n".join(child_info) + "\n\n"
                f"Check for cross-cutting issues (shared state, race conditions, "
                f"conflicting approaches). Run integration tests.",
                {"depth": node.depth, "mode": "integrate"},
            )

            for disc_text in result.get("discoveries", []):
                self.engine.discover(node.id, disc_text, severity="important")
            for test in result.get("tests", []):
                self.engine.test(node.id, test["name"], test["passed"],
                                 test.get("output", ""))

        if self.engine.ratchet.acceptance_met(node.id):
            # Determine resolution based on children
            child_resolutions = [c.resolution for c in children
                                  if c.status == FractalStatus.RESOLVED]
            min_res = min(child_resolutions, default=Resolution.SKETCH)
            self.engine.resolve(node.id, min_res)
        else:
            # Integration failed — discover what went wrong
            self.engine.discover(node.id,
                                 "Integration did not pass acceptance tests",
                                 severity="important")

        return node

    def _reclassify_and_retry(self, node: FractalNode):
        """A blocked node gets reclassified and retried with a different strategy."""
        old_strategy = node.strategy
        old_classification = self._classifications.get(node.id, "unknown")

        # Try a different approach
        if old_strategy == Strategy.ZOOM:
            node.strategy = Strategy.SPIRAL  # stuck going deep? try broad passes
        elif old_strategy == Strategy.SWARM:
            node.strategy = Strategy.ZOOM    # parallel didn't work? go serial deep
        elif old_strategy == Strategy.SPIRAL:
            node.strategy = Strategy.ZOOM    # broad passes not resolving? zoom in
        else:
            return  # already tried everything

        node.status = FractalStatus.PENDING
        node.cycle_count = 0  # reset cycles for new approach
        self._setup_sub_method(node)

        self.engine.discover(node.id,
                             f"Reclassified: {old_strategy} → {node.strategy} "
                             f"(was blocked under {old_strategy})",
                             severity="info")
        log.info("Organism: reclassified #%d from %s to %s",
                 node.id, old_strategy, node.strategy)

    def _default_classify(self, subtask: dict, parent: FractalNode) -> str:
        """Heuristic classification based on description keywords."""
        text = (subtask.get("title", "") + " " + subtask.get("description", "")).lower()

        # Check skill library first
        skill = self.engine.find_skill(text)
        if skill:
            return BranchClassification.KNOWN

        # Keyword heuristics
        trivial_signals = ["config", "rename", "move", "copy", "delete", "toggle",
                           "set", "update version", "change color", "add field"]
        deep_signals = ["debug", "reverse", "investigate", "auth", "security",
                        "encrypt", "protocol", "kernel", "driver", "race condition"]
        independent_signals = ["frontend", "backend", "api", "ui", "database",
                               "test", "docs", "deploy", "ci"]

        if any(s in text for s in trivial_signals):
            return BranchClassification.TRIVIAL
        if any(s in text for s in deep_signals):
            return BranchClassification.DEEP
        if any(s in text for s in independent_signals):
            return BranchClassification.INDEPENDENT

        return BranchClassification.UNKNOWN

    def _classification_to_strategy(self, classification: str) -> Strategy:
        return {
            BranchClassification.KNOWN: Strategy.SKILL_REPLAY,
            BranchClassification.TRIVIAL: Strategy.TRIVIAL,
            BranchClassification.INDEPENDENT: Strategy.SWARM,
            BranchClassification.DEEP: Strategy.ZOOM,
            BranchClassification.UNKNOWN: Strategy.SPIRAL,
        }.get(classification, Strategy.ZOOM)

    def _setup_sub_method(self, node: FractalNode):
        """Create the appropriate sub-method for a classified node."""
        if node.strategy == Strategy.ZOOM:
            self._sub_methods[node.id] = ZoomMethod(self.engine, self.agent_fn)
        elif node.strategy == Strategy.SWARM:
            self._sub_methods[node.id] = SwarmMethod(self.engine, self.agent_fn)
        elif node.strategy == Strategy.SPIRAL:
            self._sub_methods[node.id] = SpiralMethod(self.engine, self.agent_fn)

    def get_classifications(self) -> dict[int, str]:
        return dict(self._classifications)
