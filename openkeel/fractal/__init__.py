"""Fractal Task Decomposition — recursive problem-solving for agents.

Four methods, each suited to different problem shapes:

  Zoom    — Depth-first. One agent goes deep on one branch at a time.
  Swarm   — Breadth-first. Fan out to parallel agents per branch.
  Spiral  — Iterative deepening. Multiple passes at increasing resolution.
  Organism — Adaptive hybrid. Classifies branches and picks the best strategy.

All methods share the same core loop:
  UNDERSTAND → ROUGH PASS → TEST → DISCOVER → DECOMPOSE → RECURSE → INTEGRATE → EVALUATE
"""

from .engine import FractalEngine, FractalNode, FractalStatus, Resolution, Strategy
from .methods import ZoomMethod, SwarmMethod, SpiralMethod, OrganismMethod
from .persistence import PersistentFractalEngine
from .runner import FractalRunner

__all__ = [
    "FractalEngine",
    "FractalNode",
    "FractalStatus",
    "Resolution",
    "Strategy",
    "ZoomMethod",
    "SwarmMethod",
    "SpiralMethod",
    "OrganismMethod",
    "PersistentFractalEngine",
    "FractalRunner",
]
