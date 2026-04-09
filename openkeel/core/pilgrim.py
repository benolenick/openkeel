"""The Vigilant Pilgrim — walks the problem map looking for what everyone missed.

Does NOT read the distilled log. Only sees the Cartographer's map.
Traverses it systematically: testing edges, probing assumptions,
finding clusters of failure that share a common unexamined root.

The Pilgrim walks, tests, investigates. It never builds — that's
the Cartographer's job.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from openkeel.core.cartographer import (
    ProblemMap,
    MapNode,
    MapEdge,
    get_contradictions,
    get_failed_techniques_for_goal,
    get_assumptions_for_node,
    get_neighbors,
    find_nodes_by_type,
    find_edges_by_type,
    map_to_prompt_context,
)


# ---------------------------------------------------------------------------
# Pilgrim findings
# ---------------------------------------------------------------------------

@dataclass
class BlindSpot:
    """Something the executor is probably missing."""
    description: str = ""
    severity: int = 0          # 0-10
    evidence_node_ids: list[str] = field(default_factory=list)
    suggested_action: str = ""
    category: str = ""         # contradiction, shared_assumption, unexplored, stale


@dataclass
class PilgrimReport:
    """The Pilgrim's findings from one traversal."""
    timestamp: str = ""
    blind_spots: list[BlindSpot] = field(default_factory=list)
    unexplored_paths: list[str] = field(default_factory=list)
    likely_false_assumptions: list[str] = field(default_factory=list)
    highest_severity: int = 0
    walk_summary: str = ""

    @property
    def is_screaming(self) -> bool:
        """True if the Pilgrim has high-severity findings."""
        return self.highest_severity >= 8

    @property
    def should_inject(self) -> bool:
        """True if findings are critical enough to interrupt the executor."""
        return self.highest_severity >= 9 or len(self.blind_spots) >= 3


# ---------------------------------------------------------------------------
# Graph traversal algorithms (run locally, no LLM needed)
# ---------------------------------------------------------------------------

def find_contradiction_clusters(pmap: ProblemMap) -> list[BlindSpot]:
    """Find groups of contradictions that might share a root cause."""
    contradictions = get_contradictions(pmap)
    if not contradictions:
        return []

    spots = []
    for edge, src, tgt in contradictions:
        # For each contradiction, find what assumptions both nodes depend on
        src_assumptions = get_assumptions_for_node(pmap, src.id)
        tgt_assumptions = get_assumptions_for_node(pmap, tgt.id)

        # Any shared assumptions between contradicting facts are suspicious
        shared = set(a.id for a in src_assumptions) & set(a.id for a in tgt_assumptions)
        if shared:
            shared_nodes = [pmap.nodes[nid] for nid in shared if nid in pmap.nodes]
            for node in shared_nodes:
                spots.append(BlindSpot(
                    description=(
                        f"Contradicting facts '{src.text[:40]}' and '{tgt.text[:40]}' "
                        f"both depend on assumption: '{node.text}'. "
                        f"If this assumption is FALSE, it may resolve the contradiction."
                    ),
                    severity=8,
                    evidence_node_ids=[src.id, tgt.id, node.id],
                    suggested_action=f"Verify assumption: {node.text}",
                    category="shared_assumption",
                ))
        else:
            # No shared assumption found — still flag the contradiction
            spots.append(BlindSpot(
                description=(
                    f"Unresolved contradiction: '{src.text[:50]}' vs '{tgt.text[:50]}'"
                ),
                severity=7,
                evidence_node_ids=[src.id, tgt.id],
                suggested_action="Investigate what connects these contradicting observations",
                category="contradiction",
            ))

    return spots


def find_failure_patterns(pmap: ProblemMap) -> list[BlindSpot]:
    """Find goals where 3+ techniques failed — they likely share a false assumption."""
    spots = []
    goals = find_nodes_by_type(pmap, "goal")

    for goal in goals:
        failed = get_failed_techniques_for_goal(pmap, goal.id)
        if len(failed) < 3:
            continue

        # Collect all assumptions that ANY of the failed techniques depend on
        assumption_counts: dict[str, int] = {}
        assumption_nodes: dict[str, MapNode] = {}

        for tech in failed:
            for assumption in get_assumptions_for_node(pmap, tech.id):
                assumption_counts[assumption.id] = assumption_counts.get(assumption.id, 0) + 1
                assumption_nodes[assumption.id] = assumption

        # Find assumptions shared by most failures
        for aid, count in assumption_counts.items():
            if count >= len(failed) * 0.6:  # shared by 60%+ of failures
                node = assumption_nodes[aid]
                spots.append(BlindSpot(
                    description=(
                        f"{len(failed)} techniques failed for goal '{goal.text[:40]}'. "
                        f"{count}/{len(failed)} share assumption: '{node.text}'. "
                        f"This assumption is probably FALSE."
                    ),
                    severity=9,
                    evidence_node_ids=[goal.id, node.id] + [t.id for t in failed],
                    suggested_action=f"TEST THIS ASSUMPTION: {node.text}",
                    category="shared_assumption",
                ))

    return spots


def find_unexplored_paths(pmap: ProblemMap) -> list[str]:
    """Find question nodes or goals with no techniques tried."""
    unexplored = []

    # Open questions
    questions = find_nodes_by_type(pmap, "question")
    for q in questions:
        neighbors = get_neighbors(pmap, q.id)
        has_answer = any(
            e.edge_type in ("SUPPORTS", "DISCOVERED_BY") for e, _ in neighbors
        )
        if not has_answer:
            unexplored.append(f"Unanswered question: {q.text}")

    # Goals with no techniques
    goals = find_nodes_by_type(pmap, "goal")
    for goal in goals:
        neighbors = get_neighbors(pmap, goal.id)
        has_technique = any(e.edge_type == "TRIED_FOR" for e, _ in neighbors)
        if not has_technique:
            unexplored.append(f"Goal with no attempts: {goal.text}")

    # Environment nodes that are undiscovered
    env_nodes = find_nodes_by_type(pmap, "environment")
    for env in env_nodes:
        if not env.discovered:
            unexplored.append(f"Undiscovered environment property: {env.text}")

    return unexplored


def find_stale_assumptions(pmap: ProblemMap) -> list[BlindSpot]:
    """Find assumptions that were set early and never revisited despite new evidence."""
    spots = []
    assumptions = find_nodes_by_type(pmap, "assumption")

    for assumption in assumptions:
        if assumption.confidence < 0.5:
            continue  # already flagged as weak

        neighbors = get_neighbors(pmap, assumption.id)

        # Count how many things depend on this assumption
        dependents = [n for e, n in neighbors if e.edge_type == "DEPENDS_ON" and e.target == assumption.id]

        # Check if any dependent has failed
        failed_dependents = [
            n for n in dependents
            if n.node_type == "technique" and n.result == "fail"
        ]

        if len(failed_dependents) >= 2 and assumption.confidence > 0.5:
            spots.append(BlindSpot(
                description=(
                    f"Assumption '{assumption.text[:50]}' has confidence {assumption.confidence:.0%} "
                    f"but {len(failed_dependents)} dependent techniques have FAILED. "
                    f"This confidence seems too high."
                ),
                severity=7,
                evidence_node_ids=[assumption.id] + [n.id for n in failed_dependents],
                suggested_action=f"Re-evaluate assumption: {assumption.text}",
                category="stale",
            ))

    return spots


def find_assumption_explaining_most_failures(pmap: ProblemMap) -> BlindSpot | None:
    """The Pilgrim's killer question: 'What single assumption, if false, explains the most failures?'"""
    assumptions = find_nodes_by_type(pmap, "assumption")
    techniques = [n for n in pmap.nodes.values() if n.node_type == "technique" and n.result == "fail"]

    if not assumptions or not techniques:
        return None

    best_assumption = None
    best_count = 0
    best_techs: list[MapNode] = []

    for assumption in assumptions:
        # Find all edges where something WOULD_EXPLAIN_IF_FALSE this assumption
        explain_edges = [
            e for e in pmap.edges
            if e.edge_type == "WOULD_EXPLAIN_IF_FALSE" and e.target == assumption.id
        ]
        explained_node_ids = {e.source for e in explain_edges}

        # Also count direct dependents that failed
        for e in pmap.edges:
            if e.edge_type == "DEPENDS_ON" and e.target == assumption.id:
                tech = pmap.nodes.get(e.source)
                if tech and tech.result == "fail":
                    explained_node_ids.add(tech.id)

        if len(explained_node_ids) > best_count:
            best_count = len(explained_node_ids)
            best_assumption = assumption
            best_techs = [pmap.nodes[nid] for nid in explained_node_ids if nid in pmap.nodes]

    if best_assumption and best_count >= 2:
        return BlindSpot(
            description=(
                f"IF '{best_assumption.text}' IS FALSE, it would explain "
                f"{best_count} failures: {', '.join(t.text[:30] for t in best_techs[:5])}"
            ),
            severity=min(10, 6 + best_count),
            evidence_node_ids=[best_assumption.id] + [t.id for t in best_techs],
            suggested_action=f"VERIFY: {best_assumption.text}",
            category="shared_assumption",
        )
    return None


# ---------------------------------------------------------------------------
# Full traversal (the Pilgrim's walk)
# ---------------------------------------------------------------------------

def walk_map(pmap: ProblemMap) -> PilgrimReport:
    """Perform a full traversal of the map. Returns findings.

    This is the LOCAL analysis — no LLM needed. It runs graph algorithms
    on the map structure. The LLM-enhanced walk happens via the prompt below.
    """
    report = PilgrimReport(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    # 1. Contradiction clusters
    report.blind_spots.extend(find_contradiction_clusters(pmap))

    # 2. Failure patterns (3+ fails on same goal)
    report.blind_spots.extend(find_failure_patterns(pmap))

    # 3. Stale assumptions
    report.blind_spots.extend(find_stale_assumptions(pmap))

    # 4. The killer question
    killer = find_assumption_explaining_most_failures(pmap)
    if killer:
        report.blind_spots.append(killer)

    # 5. Unexplored paths
    report.unexplored_paths = find_unexplored_paths(pmap)

    # 6. Likely false assumptions (any assumption with confidence > 0.5 that
    #    has multiple failed dependents)
    for spot in report.blind_spots:
        if spot.category == "shared_assumption":
            report.likely_false_assumptions.append(spot.description)

    # Calculate severity
    if report.blind_spots:
        report.highest_severity = max(s.severity for s in report.blind_spots)

    # Summary
    parts = []
    if report.blind_spots:
        parts.append(f"{len(report.blind_spots)} blind spots (max severity: {report.highest_severity})")
    if report.unexplored_paths:
        parts.append(f"{len(report.unexplored_paths)} unexplored paths")
    if report.likely_false_assumptions:
        parts.append(f"{len(report.likely_false_assumptions)} suspect assumptions")
    report.walk_summary = " | ".join(parts) if parts else "Map looks clean"

    return report


# ---------------------------------------------------------------------------
# LLM-enhanced walk prompt
# ---------------------------------------------------------------------------

PILGRIM_SYSTEM_PROMPT = """\
You are a knowledge retrieval agent. You analyze penetration test activity and extract search queries.

You receive recent attack activity: services found, versions detected, errors encountered, tools used.

Your ONLY job: extract 3-5 specific search queries to find relevant exploits, CVEs, and techniques.

For each discovery in the activity, generate a targeted query:
- Software with version? Query: "<software> <version> CVE exploit RCE"
- Service on unusual port? Query: "<service> default credentials exploit"
- Error message? Query: "<key error phrase> bypass workaround"
- Permission denied? Query: "<service> privilege escalation alternative access"
- New username found? Query: "<username pattern> default password <domain>"

Output JSON:
{
  "queries": [
    {"query": "exact search string for Memoria", "reason": "what this would find", "priority": "high|medium|low"}
  ],
  "services_detected": ["software/version pairs found in recent activity"],
  "failure_count": 0,
  "loop_detected": false,
  "loop_description": ""
}

Rules:
- Extract SOFTWARE NAMES and VERSION NUMBERS from the activity. These are the most valuable queries.
- If you see the same tool failing 3+ times, set loop_detected=true and describe the loop.
- Queries must be SPECIFIC: "PWM 2.0.8 CVE LDAP credential exposure" not "password manager exploit"
- Include the exact version number in every query where available.
- Maximum 5 queries, minimum 2."""


def build_pilgrim_prompt(pmap: ProblemMap) -> str:
    """Build the full prompt for one Pilgrim walk cycle."""
    return f"""{map_to_prompt_context(pmap)}

Walk this map. Find what's missing. Be vigilant. Trust nothing."""


# ---------------------------------------------------------------------------
# Apply Pilgrim's LLM-enhanced findings
# ---------------------------------------------------------------------------

def apply_pilgrim_findings(report: PilgrimReport, llm_findings: dict) -> PilgrimReport:
    """Merge LLM-enhanced findings into the local analysis report."""
    for spot in llm_findings.get("blind_spots", []):
        report.blind_spots.append(BlindSpot(
            description=spot.get("description", ""),
            severity=spot.get("severity", 5),
            suggested_action=spot.get("suggested_action", ""),
            category=spot.get("category", "llm_finding"),
        ))

    for path in llm_findings.get("unexplored_paths", []):
        if path not in report.unexplored_paths:
            report.unexplored_paths.append(path)

    for assumption in llm_findings.get("likely_false_assumptions", []):
        if assumption not in report.likely_false_assumptions:
            report.likely_false_assumptions.append(assumption)

    killer = llm_findings.get("killer_question", "")
    if killer:
        report.blind_spots.append(BlindSpot(
            description=killer,
            severity=9,
            suggested_action="INVESTIGATE THIS IMMEDIATELY",
            category="killer_question",
        ))

    # Recalculate
    if report.blind_spots:
        report.highest_severity = max(s.severity for s in report.blind_spots)

    return report


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def report_to_dict(r: PilgrimReport) -> dict:
    return {
        "timestamp": r.timestamp,
        "blind_spots": [
            {
                "description": s.description,
                "severity": s.severity,
                "evidence_node_ids": s.evidence_node_ids,
                "suggested_action": s.suggested_action,
                "category": s.category,
            }
            for s in r.blind_spots
        ],
        "unexplored_paths": r.unexplored_paths,
        "likely_false_assumptions": r.likely_false_assumptions,
        "highest_severity": r.highest_severity,
        "walk_summary": r.walk_summary,
        "is_screaming": r.is_screaming,
        "should_inject": r.should_inject,
    }
