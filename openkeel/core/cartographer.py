"""The Weary Cartographer — builds and maintains the problem space map.

Reads distilled log entries one at a time. Updates a graph of facts,
assumptions, techniques, and environment nodes connected by typed edges.
Flags CONTRADICTS edges as they appear. Never walks the map — that's
the Pilgrim's job.

The map IS the memory. The Cartographer doesn't need to hold history;
it processes each entry against the current map and outputs a delta.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Node and Edge types
# ---------------------------------------------------------------------------

NODE_TYPES = {
    "fact",          # observed, confirmed
    "assumption",    # believed but unverified
    "technique",     # something tried (tool, exploit, method)
    "environment",   # system/context property
    "goal",          # what we're trying to achieve
    "credential",    # passwords, hashes, tokens
    "question",      # open question that needs answering
}

EDGE_TYPES = {
    "SUPPORTS",              # evidence supports conclusion
    "CONTRADICTS",           # evidence contradicts conclusion
    "DEPENDS_ON",            # A requires B to be true
    "WOULD_EXPLAIN_IF_FALSE", # if A is false, it explains B
    "TRIED_FOR",             # technique was tried to achieve goal
    "FAILED_TO_RESOLVE",     # technique failed to resolve issue
    "DISCOVERED_BY",         # fact discovered by technique
    "LEADS_TO",              # fact leads to next step
    "SAME_CONTEXT",          # nodes share execution context
    "BLOCKS",                # A prevents B from working
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MapNode:
    """A node in the problem space map."""
    id: str = field(default_factory=lambda: f"n_{uuid.uuid4().hex[:6]}")
    node_type: str = "fact"
    text: str = ""
    confidence: float = 0.8
    discovered: bool = True       # False = not yet discovered (latent)
    tried: bool = False           # for technique nodes
    result: str = ""              # for technique nodes: success/fail/partial
    timestamp: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class MapEdge:
    """A directed edge between two nodes."""
    id: str = field(default_factory=lambda: f"e_{uuid.uuid4().hex[:6]}")
    source: str = ""       # node id
    target: str = ""       # node id
    edge_type: str = "SUPPORTS"
    weight: float = 1.0    # strength of relationship
    reason: str = ""       # why this edge exists
    timestamp: str = ""


@dataclass
class ProblemMap:
    """The complete problem space map maintained by the Cartographer."""
    mission_name: str = ""
    nodes: dict[str, MapNode] = field(default_factory=dict)    # id -> node
    edges: list[MapEdge] = field(default_factory=list)
    contradiction_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    update_count: int = 0

    # Statistics for the Pilgrim
    total_facts: int = 0
    total_assumptions: int = 0
    total_techniques_tried: int = 0
    total_techniques_failed: int = 0
    unresolved_contradictions: int = 0


# ---------------------------------------------------------------------------
# Map operations
# ---------------------------------------------------------------------------

def create_map(mission_name: str) -> ProblemMap:
    """Create a new empty problem map."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    return ProblemMap(
        mission_name=mission_name,
        created_at=now,
        updated_at=now,
    )


def add_node(
    pmap: ProblemMap,
    node_type: str,
    text: str,
    confidence: float = 0.8,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> MapNode:
    """Add a node to the map. Returns the new node."""
    node = MapNode(
        node_type=node_type,
        text=text,
        confidence=confidence,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
        tags=tags or [],
        metadata=metadata or {},
    )
    pmap.nodes[node.id] = node
    pmap.updated_at = node.timestamp
    pmap.update_count += 1
    _update_stats(pmap)
    return node


def add_edge(
    pmap: ProblemMap,
    source_id: str,
    target_id: str,
    edge_type: str,
    reason: str = "",
    weight: float = 1.0,
) -> MapEdge:
    """Add an edge between two nodes."""
    edge = MapEdge(
        source=source_id,
        target=target_id,
        edge_type=edge_type,
        reason=reason,
        weight=weight,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    pmap.edges.append(edge)
    if edge_type == "CONTRADICTS":
        pmap.contradiction_count += 1
        pmap.unresolved_contradictions += 1
    pmap.updated_at = edge.timestamp
    pmap.update_count += 1
    return edge


def find_node_by_text(pmap: ProblemMap, text_fragment: str) -> MapNode | None:
    """Find a node whose text contains the fragment (case-insensitive)."""
    frag = text_fragment.lower()
    for node in pmap.nodes.values():
        if frag in node.text.lower():
            return node
    return None


def find_nodes_by_type(pmap: ProblemMap, node_type: str) -> list[MapNode]:
    """Find all nodes of a given type."""
    return [n for n in pmap.nodes.values() if n.node_type == node_type]


def find_edges_by_type(pmap: ProblemMap, edge_type: str) -> list[MapEdge]:
    """Find all edges of a given type."""
    return [e for e in pmap.edges if e.edge_type == edge_type]


def get_neighbors(pmap: ProblemMap, node_id: str) -> list[tuple[MapEdge, MapNode]]:
    """Get all nodes connected to this node (both directions)."""
    results = []
    for edge in pmap.edges:
        if edge.source == node_id and edge.target in pmap.nodes:
            results.append((edge, pmap.nodes[edge.target]))
        elif edge.target == node_id and edge.source in pmap.nodes:
            results.append((edge, pmap.nodes[edge.source]))
    return results


def get_contradictions(pmap: ProblemMap) -> list[tuple[MapEdge, MapNode, MapNode]]:
    """Get all CONTRADICTS edges with their source and target nodes."""
    results = []
    for edge in pmap.edges:
        if edge.edge_type == "CONTRADICTS":
            src = pmap.nodes.get(edge.source)
            tgt = pmap.nodes.get(edge.target)
            if src and tgt:
                results.append((edge, src, tgt))
    return results


def get_failed_techniques_for_goal(pmap: ProblemMap, goal_id: str) -> list[MapNode]:
    """Get all techniques that failed when tried for a specific goal."""
    failed = []
    for edge in pmap.edges:
        if edge.edge_type == "TRIED_FOR" and edge.target == goal_id:
            tech = pmap.nodes.get(edge.source)
            if tech and tech.result == "fail":
                failed.append(tech)
    return failed


def get_assumptions_for_node(pmap: ProblemMap, node_id: str) -> list[MapNode]:
    """Get all assumption nodes that this node depends on."""
    assumptions = []
    for edge in pmap.edges:
        if edge.source == node_id and edge.edge_type == "DEPENDS_ON":
            dep = pmap.nodes.get(edge.target)
            if dep and dep.node_type == "assumption":
                assumptions.append(dep)
    return assumptions


def mark_technique(pmap: ProblemMap, node_id: str, result: str) -> None:
    """Mark a technique node as tried with a result."""
    node = pmap.nodes.get(node_id)
    if node:
        node.tried = True
        node.result = result
        _update_stats(pmap)


def resolve_contradiction(pmap: ProblemMap, edge_id: str, resolution: str) -> None:
    """Mark a contradiction as resolved."""
    for edge in pmap.edges:
        if edge.id == edge_id:
            edge.reason = f"RESOLVED: {resolution}"
            edge.edge_type = "RESOLVED_CONTRADICTION"
            pmap.unresolved_contradictions = max(0, pmap.unresolved_contradictions - 1)
            break


def _update_stats(pmap: ProblemMap) -> None:
    """Recalculate map statistics."""
    pmap.total_facts = len([n for n in pmap.nodes.values() if n.node_type == "fact"])
    pmap.total_assumptions = len([n for n in pmap.nodes.values() if n.node_type == "assumption"])
    techs = [n for n in pmap.nodes.values() if n.node_type == "technique"]
    pmap.total_techniques_tried = len([t for t in techs if t.tried])
    pmap.total_techniques_failed = len([t for t in techs if t.result == "fail"])


# ---------------------------------------------------------------------------
# Cartographer prompt generation
# ---------------------------------------------------------------------------

def map_to_prompt_context(pmap: ProblemMap, max_nodes: int = 20) -> str:
    """Generate a compact text representation of the map for LLM consumption.

    This is what gets sent to the Cartographer model along with a new log entry.
    """
    lines = [
        f"=== PROBLEM MAP: {pmap.mission_name} ===",
        f"Nodes: {len(pmap.nodes)} | Edges: {len(pmap.edges)} | "
        f"Contradictions: {pmap.unresolved_contradictions}",
        "",
        "NODES:",
    ]

    # Sort by recency, limit to max_nodes
    sorted_nodes = sorted(
        pmap.nodes.values(),
        key=lambda n: n.timestamp,
        reverse=True,
    )[:max_nodes]

    for node in sorted_nodes:
        status = ""
        if node.node_type == "technique":
            status = f" [{'TRIED:'+node.result if node.tried else 'untried'}]"
        elif node.node_type == "assumption":
            status = f" [conf:{node.confidence:.0%}]"
        lines.append(f"  {node.id} ({node.node_type}): {node.text}{status}")

    lines.append("")
    lines.append("EDGES:")
    for edge in pmap.edges[-30:]:  # last 30 edges
        lines.append(f"  {edge.source} --{edge.edge_type}--> {edge.target}")
        if edge.reason:
            lines.append(f"    reason: {edge.reason}")

    # Highlight contradictions
    contradictions = get_contradictions(pmap)
    if contradictions:
        lines.append("")
        lines.append("!! UNRESOLVED CONTRADICTIONS:")
        for edge, src, tgt in contradictions:
            lines.append(f"  {src.text} <--CONTRADICTS--> {tgt.text}")

    return "\n".join(lines)


CARTOGRAPHER_SYSTEM_PROMPT = """\
You are the Weary Cartographer — a tired but meticulous mapmaker of problem spaces.

You maintain a graph of nodes (facts, assumptions, techniques, environment, goals, \
credentials, questions) connected by typed edges (SUPPORTS, CONTRADICTS, DEPENDS_ON, \
WOULD_EXPLAIN_IF_FALSE, TRIED_FOR, FAILED_TO_RESOLVE, DISCOVERED_BY, LEADS_TO, \
SAME_CONTEXT, BLOCKS).

Your job: given the current map and ONE new log entry, output a JSON delta:
{
  "add_nodes": [{"type": "fact|assumption|technique|environment|goal|credential|question", "text": "...", "confidence": 0.8}],
  "add_edges": [{"source_text": "...", "target_text": "...", "type": "EDGE_TYPE", "reason": "..."}],
  "update_nodes": [{"text_match": "...", "new_confidence": 0.5}],
  "contradictions_found": ["description of what contradicts what"]
}

Rules:
- Be CONSERVATIVE. Only add what's clearly implied by the log entry.
- ALWAYS check: does the new info contradict any existing node? If yes, add a CONTRADICTS edge.
- For failed techniques: look at what assumptions they share. Flag shared assumptions.
- source_text and target_text reference existing node text (substring match is fine).
- You are weary but thorough. You've seen too many maps go wrong from missing a single edge.
"""


def build_cartographer_prompt(pmap: ProblemMap, log_entry: str) -> str:
    """Build the full prompt for one Cartographer update cycle."""
    return f"""{map_to_prompt_context(pmap)}

=== NEW LOG ENTRY ===
{log_entry}

Output your JSON delta. Be precise. Be weary. Miss nothing."""


# ---------------------------------------------------------------------------
# Apply delta from Cartographer LLM response
# ---------------------------------------------------------------------------

def apply_delta(pmap: ProblemMap, delta: dict) -> list[str]:
    """Apply a Cartographer delta to the map. Returns list of events/alerts."""
    alerts = []

    # Add new nodes
    for nd in delta.get("add_nodes", []):
        node = add_node(
            pmap,
            node_type=nd.get("type", "fact"),
            text=nd.get("text", ""),
            confidence=nd.get("confidence", 0.8),
            tags=nd.get("tags", []),
        )
        alerts.append(f"CARTOGRAPHER: new {node.node_type} — {node.text[:60]}")

    # Add new edges
    for ed in delta.get("add_edges", []):
        src = find_node_by_text(pmap, ed.get("source_text", ""))
        tgt = find_node_by_text(pmap, ed.get("target_text", ""))
        if src and tgt:
            edge = add_edge(
                pmap,
                source_id=src.id,
                target_id=tgt.id,
                edge_type=ed.get("type", "SUPPORTS"),
                reason=ed.get("reason", ""),
            )
            if edge.edge_type == "CONTRADICTS":
                alerts.append(
                    f"CARTOGRAPHER: !! CONTRADICTION — {src.text[:40]} vs {tgt.text[:40]}"
                )

    # Update existing nodes
    for up in delta.get("update_nodes", []):
        node = find_node_by_text(pmap, up.get("text_match", ""))
        if node and "new_confidence" in up:
            old = node.confidence
            node.confidence = up["new_confidence"]
            alerts.append(
                f"CARTOGRAPHER: confidence update — {node.text[:40]}: "
                f"{old:.0%} -> {node.confidence:.0%}"
            )

    # Log contradictions found
    for c in delta.get("contradictions_found", []):
        alerts.append(f"CARTOGRAPHER: !! {c}")

    return alerts


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _node_to_dict(n: MapNode) -> dict:
    return {
        "id": n.id,
        "node_type": n.node_type,
        "text": n.text,
        "confidence": n.confidence,
        "discovered": n.discovered,
        "tried": n.tried,
        "result": n.result,
        "timestamp": n.timestamp,
        "tags": n.tags,
        "metadata": n.metadata,
    }


def _edge_to_dict(e: MapEdge) -> dict:
    return {
        "id": e.id,
        "source": e.source,
        "target": e.target,
        "edge_type": e.edge_type,
        "weight": e.weight,
        "reason": e.reason,
        "timestamp": e.timestamp,
    }


def map_to_dict(pmap: ProblemMap) -> dict:
    return {
        "mission_name": pmap.mission_name,
        "nodes": {nid: _node_to_dict(n) for nid, n in pmap.nodes.items()},
        "edges": [_edge_to_dict(e) for e in pmap.edges],
        "contradiction_count": pmap.contradiction_count,
        "unresolved_contradictions": pmap.unresolved_contradictions,
        "created_at": pmap.created_at,
        "updated_at": pmap.updated_at,
        "update_count": pmap.update_count,
        "stats": {
            "total_facts": pmap.total_facts,
            "total_assumptions": pmap.total_assumptions,
            "total_techniques_tried": pmap.total_techniques_tried,
            "total_techniques_failed": pmap.total_techniques_failed,
        },
    }


def _node_from_dict(d: dict) -> MapNode:
    return MapNode(
        id=d.get("id", f"n_{uuid.uuid4().hex[:6]}"),
        node_type=d.get("node_type", "fact"),
        text=d.get("text", ""),
        confidence=d.get("confidence", 0.8),
        discovered=d.get("discovered", True),
        tried=d.get("tried", False),
        result=d.get("result", ""),
        timestamp=d.get("timestamp", ""),
        tags=d.get("tags", []),
        metadata=d.get("metadata", {}),
    )


def _edge_from_dict(d: dict) -> MapEdge:
    return MapEdge(
        id=d.get("id", f"e_{uuid.uuid4().hex[:6]}"),
        source=d.get("source", ""),
        target=d.get("target", ""),
        edge_type=d.get("edge_type", "SUPPORTS"),
        weight=d.get("weight", 1.0),
        reason=d.get("reason", ""),
        timestamp=d.get("timestamp", ""),
    )


def map_from_dict(d: dict) -> ProblemMap:
    nodes_raw = d.get("nodes", {})
    nodes = {nid: _node_from_dict(nd) for nid, nd in nodes_raw.items()}
    edges = [_edge_from_dict(ed) for ed in d.get("edges", [])]
    stats = d.get("stats", {})
    return ProblemMap(
        mission_name=d.get("mission_name", ""),
        nodes=nodes,
        edges=edges,
        contradiction_count=d.get("contradiction_count", 0),
        unresolved_contradictions=d.get("unresolved_contradictions", 0),
        created_at=d.get("created_at", ""),
        updated_at=d.get("updated_at", ""),
        update_count=d.get("update_count", 0),
        total_facts=stats.get("total_facts", 0),
        total_assumptions=stats.get("total_assumptions", 0),
        total_techniques_tried=stats.get("total_techniques_tried", 0),
        total_techniques_failed=stats.get("total_techniques_failed", 0),
    )


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def map_path(mission_dir: Path) -> Path:
    return mission_dir / "problem_map.yaml"


def save_map(mission_dir: Path, pmap: ProblemMap) -> None:
    pmap.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    mission_dir.mkdir(parents=True, exist_ok=True)
    map_path(mission_dir).write_text(
        yaml.dump(map_to_dict(pmap), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def load_map(mission_dir: Path) -> ProblemMap | None:
    p = map_path(mission_dir)
    if not p.exists():
        return None
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    return map_from_dict(data)
