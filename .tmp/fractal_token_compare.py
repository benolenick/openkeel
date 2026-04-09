"""Sequential fractal test: knowledge-blocks vs graphs vs manifolds vs OpenKeel token-saver stack.

Runs spiral (sequential) method, depth-capped at 2.
"""
import sys, json, time
sys.path.insert(0, "/home/om/openkeel")

from openkeel.fractal.runner import FractalRunner

TITLE = "Compare token-saving methods vs OpenKeel stack"
DESC = """Evaluate whether OpenKeel's manifold-based memory + LocalEdit + task routing + Cartographer stack
is stronger at token savings than mainstream 2025/2026 methods.

Methods to benchmark against OpenKeel:
  A) Knowledge blocks / RAG chunking (semantic top-k injection)
  B) GraphRAG / LightRAG / Graphiti (entity-graph traversal)
  C) Manifold / embedding-space recall (OpenKeel Hyphae's approach)
  D) Conversation compression / summarization
  E) Sub-agent offloading / task routing (OpenKeel + aider/RouteLLM)
  F) Local edit delegation (OpenKeel LocalEdit → gemma4)
  G) KV-cache reuse + prompt prefix caching
  H) Speculative decoding / draft models

For each method, at depth 1 produce:
  - Mechanism (1 line)
  - Token savings ceiling (rough %)
  - Where OpenKeel already does this
  - Where OpenKeel beats it
  - Where it beats OpenKeel
  - Verdict: stronger / equal / weaker vs OpenKeel

At depth 2, decompose ONLY the methods where the verdict is unclear,
and design a concrete measurable test (input size, metric, expected delta).

STOP at depth 2. Do not decompose further. This is analysis, not implementation.
Return a final summary table and an overall verdict on: is the manifold-based
OpenKeel stack stronger than graph-based competitors?
"""

def depth_cap_callback(iteration, node, engine):
    # Hard stop any node at depth >= 2 from spawning subtasks
    if node.depth >= 2:
        node.subtasks_planned = []

runner = FractalRunner.start(
    title=TITLE,
    description=DESC,
    method="spiral",          # sequential
    agent="claude",
    agent_model="sonnet",
    fractal_id="token-saver-compare",
)

t0 = time.time()
result = runner.run(max_iterations=18, callback=depth_cap_callback)
elapsed = time.time() - t0

print("\n" + "="*60)
print(f"FRACTAL COMPLETE in {elapsed:.1f}s")
print("="*60)
print(json.dumps(result, indent=2, default=str))

stats = runner.engine.stats()
print("\nSTATS:", json.dumps(stats, indent=2, default=str))

# Dump all node outputs
print("\n" + "="*60)
print("NODE OUTPUTS")
print("="*60)
for node in runner.engine._nodes.values():
    print(f"\n--- #{node.id} depth={node.depth} [{node.status}] {node.title} ---")
    if getattr(node, "output", None):
        print(node.output[:2000])
    if getattr(node, "discoveries", None):
        print("discoveries:", node.discoveries)
