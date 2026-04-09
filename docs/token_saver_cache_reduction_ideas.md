# Token Saver — Cache Cost Reduction Ideas

**Date:** 2026-04-07
**Context:** ~73% of billed token spend is cached input (cache_read + cache_write on the stable prefix). Cache reads are 10% of input price, but they fire on *every turn*, so the cost is `prefix_bytes × turn_count`. The game is to shrink that product.

---

## Claude's ideas (harness-level, attack the prefix)

### 1. Tool schema diet — biggest lever, almost nobody does this
Tool definitions sit in the cached prefix on every turn. Claude Code ships ~15 tools with verbose JSONSchema descriptions; Bash alone is ~3KB of prose. 200 turns × 3KB × 10% = ~600KB of billed cache_reads **for tool defs Claude memorized by turn 3**.

- **Lazy tool loading.** Claude Code already has a `ToolSearch` / deferred-tool pattern — this session shows 17 deferred tools. That pattern should be *aggressive*, not conservative. Start sessions with Read/Edit/Grep/Glob/Bash only; defer everything else.
- **Post-first-use stubbing.** After Edit has been called once, replace its 2KB description with a 1-line stub. Claude doesn't need the manual anymore.
- **Schema compression.** "The absolute path to the file to read" → "abs path". 40-60% byte reduction on schemas, zero capability loss.

### 2. CLAUDE.md is billed on every turn
Current CLAUDE.md is ~6KB. Billed as cache_read ~200×/session = ~1.2MB. Most of it is instructions Claude internalized by turn 3.

- **Tiered CLAUDE.md.** ~500-byte "always on" core (Hyphae URL, LocalEdit prefix) + ~5KB "reference" injected only when a trigger fires (first hyphae question, first edit).
- **Kill redundant prose.** The LocalEdit section is 1.5KB of pep talk; `Use #LOCALEDIT: path | instruction for mechanical edits` is 60 bytes.

### 3. Proactive conversation history pruning
Every past turn's tool result sits in the cached prefix forever. A Read from turn 40 is still being cache_read on turn 200. The built-in auto-compactor only fires near context limit — way too late for cost.

- **Old-result eviction.** After turn N, replace old Read results with `<read file=X lines=1-500 [evicted, re-read if needed]>`. 500-line file evicted at turn 50 saves 150 × 10KB × 10% = ~150KB per file.
- **History-retroactive distillation.** diff_compressor / error_distiller currently fire on *new* results; they should sweep historical entries too.

### 4. Stop re-injecting session_start content on every subagent
Every `Task` subagent launch rebuilds a prefix from scratch — full cache_write on tools + system. 5 Explore agents = 5× tool-schema write cost.

- Subagents get a *minimal* tool set (Explore = Read/Grep/Glob only; no Bash, no WebFetch, no Edit).
- Share subagent prefixes across invocations within the 5-min cache window.

### 5. Raise cache TTL to 1h where it pays
1h cache = 2× write cost, 12× window. For sessions with >5 min thinking gaps (most), 5-min cache churns constantly — every gap = full re-write of the prefix. One-line config change, measurable immediately.

---

## Ben's ideas (distillation-layer, attack content quality)

### 6. spaCy / deterministic semantic compression & pruning
Free, fast (~10K tok/sec CPU), deterministic. Perfect for the boring 80%.

- **Dependency-parse pruning.** Drop subordinate clauses, adjectives, adverbs from tool-result prose while keeping SVO. "The file at /home/om/openkeel/foo.py was successfully read and contains 247 lines of Python code implementing..." → "foo.py: 247L, Python, implements...". ~60% shrink.
- **Semantic dedup of history.** Sentence embeddings (spaCy `en_core_web_md` or `sentence-transformers`) to flag when turn 80's result is ≥0.9 cosine to turn 20's — evict the older, leave a pointer. Catches "Claude Read the same file 4 times".
- **NER-based entity ledger.** Extract file paths, function names, IPs, error codes from long outputs; keep a compact ledger and drop prose. When Claude later asks about `init_db`, inject the ledger row.
- **POS-filtered grep clusters.** Existing grep_cluster engine keeps only noun phrases + verbs in cluster labels.

**Limit:** can't decide what matters to *this* task. Blunt instrument — great pre-pass, dangerous as sole pass.

### 7. 4× Qwen2.5-3B on the RTX 3090 — distillation worker pool
Qwen2.5-3B @ Q4 ≈ 2GB VRAM; four instances fit on a 3090 with KV cache headroom. ~200 tok/s each = ~800 tok/s aggregate, running in parallel with Claude during idle turns (~80% of wall clock on conversational sessions → effectively zero latency).

- **Worker 1 — history distiller.** Rewrites old tool results in place. Prompt: *"Rewrite this tool result keeping only facts referenced in later turns."* Async, background, runs on oldest uncompressed entry every turn.
- **Worker 2 — relevance scorer.** Given current user message, scores every history entry 0-10 for "likely needed this turn". <3 → evict to pointer. 3-7 → compressed version. 8+ → verbatim. This is what spaCy can't do.
- **Worker 3 — tool-result pre-chewer.** Intercepts large outputs *before* they enter history. 500-line Read → 50-line semantic skeleton + "full text on re-Read". Upgrade to existing `semantic_skeleton` engine, swapping ctags for language understanding.
- **Worker 4 — static prefix compressor.** One-shot at session start: rewrites CLAUDE.md + tool schemas into minimal form given project. Output cached to disk keyed by `(CLAUDE.md hash, tool set hash)` — compute once, reuse forever.

**Limit:** Qwen-3B hallucinates and drops details. Every pass is lossy. Guardrails:
- Never compress the same entry twice.
- Hash-addressable store of originals so Claude can fetch ground truth on demand.
- Validate compressed output is a strict subset of input (no new entities) via second cheap NER pass.

---

## The pipeline — how spaCy + Qwen compose

These aren't alternatives, they're a pipeline:

```
raw tool result
  → spaCy pre-pass (deterministic; drop fat, extract entities)   [free]
  → qwen relevance score vs current task                          [cheap]
  → qwen distill if score is mid-range                            [cheap]
  → evict to pointer if score is low                              [free]
  → inject into Claude history                                    [billed]
```

spaCy handles volume (everything, every turn); Qwen handles judgment (ambiguous middle only). Never pay Qwen for obvious boilerplate; never trust spaCy for semantic calls. The 3090 workers run in idle gaps between Claude turns → latency ≈ 0.

---

## The missing piece — stop the bleeding at the source

Every idea above compresses *what's already in history*. None stop Claude from re-requesting data it already has.

### 8. Worker 5 — "Claude already saw this" guard
Semantic-search the existing history on every proposed Read/Grep/Bash call. If the answer is already there, return a pointer instead of running the tool. This is the trick that turns a 200-turn session into a 200-turn session with a 50-turn-sized prefix. May be the single biggest win of the whole plan.

---

## Priority ranking (expected ROI)

| # | Idea | Cost to build | Expected win | Risk |
|---|---|---|---|---|
| 1 | Tool schema diet + lazy loading | Low | **High** | Low |
| 8 | "Already saw this" guard (Qwen Worker 5) | Medium | **High** | Medium |
| 3 | Proactive history eviction | Low | **High** | Low |
| 6 | spaCy pre-pass pipeline | Low | Medium | Low |
| 2 | Tiered CLAUDE.md | Low | Medium | Low |
| 7 | Qwen worker pool (W1–W4) | High | Medium-High | Medium |
| 4 | Subagent prefix sharing | Medium | Medium | Low |
| 5 | 1h cache TTL | Trivial | Low-Medium | Low |

**Recommended first shot:** #1 + #3 (ship this week, measurable in a day) followed by #6 (spaCy pre-pass, ship next week) — then evaluate whether #7/#8 (Qwen pool) is still needed once the easy wins land.
