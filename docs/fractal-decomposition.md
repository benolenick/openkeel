# Fractal Task Decomposition — Design Document

**Author:** Ben + Claude
**Date:** 2026-04-06
**Status:** Design / Pre-implementation

## The Idea

When an agent gets a job — "build a scraper for X" — it shouldn't just plan, execute, and ship. It should work like a fractal: start with the big shape, then recursively zoom in, adding detail and resolution at every level. Each zoom is a build-test-refine cycle. The agent discovers what it doesn't know by building, then decomposes the unknown parts further.

The fractal property: **the process at every level looks the same.** Decompose, build, test, discover, decompose again. The agent decides when a branch has enough resolution (works, stop zooming) vs needs more (failing, zoom deeper).

This is fundamentally different from linear agent execution (plan → execute → done). This is: rough pass → evaluate → discover gaps → zoom into gaps → repeat until the fractal stabilizes.

## Core Loop (Universal at Every Zoom Level)

```
FRACTAL_CYCLE(task, depth):
  1. UNDERSTAND  — What is this task? What do I know? What don't I know?
  2. ROUGH PASS  — Build the simplest version that could work
  3. TEST        — Does it work? What breaks? What's missing?
  4. DISCOVER    — What new sub-problems emerged from testing?
  5. DECOMPOSE   — Create child tasks for each discovered sub-problem
  6. RECURSE     — FRACTAL_CYCLE(child, depth+1) for each child
  7. INTEGRATE   — Combine child results back into this level
  8. EVALUATE    — Is this level resolved? If not, go to step 2
```

The recursion terminates when:
- All tests pass at this level (branch resolved)
- Max depth reached (ask for help or mark blocked)
- Diminishing returns detected (good enough)

---

## Approach 1: The Zoom Engine (Pure Depth-First)

The simplest model. One agent dives deep into a problem, zooming in where needed.

### How it works

```
Job: "Build a scraper for RealEstate.com"

Depth 0: [Build scraper]
  → Agent does rough pass: fetches homepage, parses one listing
  → Test: works for one page, breaks on pagination, no auth handling
  → Decompose:

  Depth 1: [Handle pagination] [Handle auth] [Parse all fields]
    → Agent tackles pagination first
    → Rough pass: follow "next" links
    → Test: works for 3 pages, then hits infinite scroll
    → Decompose:

    Depth 2: [Handle infinite scroll] [Detect end-of-results]
      → Agent discovers it's a React SPA with API calls
      → Rough pass: intercept XHR, extract API endpoint
      → Test: works! Returns JSON. This branch resolves.

    Depth 2 resolved ✓ — bubble up to Depth 1

  Depth 1: [Handle auth]
    → Rough pass: session cookies
    → Test: 403 after 10 requests — it's rate-limited, not auth
    → Re-classify: this isn't auth, it's rate limiting
    → Decompose:

    Depth 2: [Implement rate limiting] [Add retry logic]
      → Both resolve quickly. Bubble up.

  Depth 1: [Parse all fields]
    → Rough pass: extract price, address, beds/baths
    → Test: missing sqft on some listings (different layout)
    → Decompose:

    Depth 2: [Handle layout variant A] [Handle layout variant B]
      → Both resolve. Bubble up.

Depth 0: All children resolved. Integration test. Ship.
```

### Architecture

- **Single agent, single thread.** Depth-first traversal.
- **Progress saved per node.** Each zoom level has a progress file (ProgressTracker already does this).
- **Backtracking.** If a child fails, the parent can re-approach from a different angle.
- **Skill capture.** When a pattern resolves cleanly (e.g., "handle infinite scroll via XHR interception"), save it to SkillLibrary for reuse.

### Pros
- Simple. One agent, clear ownership.
- Easy to follow the thread of work.
- Natural for problems that need deep investigation.

### Cons
- Slow. Sequential. One branch at a time.
- Agent context window fills up at deep levels.
- No parallelism.

### Maps to OpenKeel
- Tasks with `parent_id` = tree structure
- Add `fractal_depth` and `fractal_status` fields to tasks
- ProgressTracker maintains context per node
- SkillLibrary captures reusable solutions

---

## Approach 2: The Swarm (Breadth-First, Multi-Agent)

Multiple agents work the fractal in parallel. Each agent owns a branch.

### How it works

```
Job: "Build a scraper for RealEstate.com"

Coordinator (depth 0):
  → Rough pass, discovers 4 sub-problems
  → Spawns 4 agents, one per branch:
    Agent A → [Pagination]     → discovers infinite scroll → resolves
    Agent B → [Auth/Rate-limit] → discovers rate limiting → resolves
    Agent C → [Field parsing]   → discovers layout variants → spawns 2 sub-agents
    Agent D → [Output format]   → resolves immediately (CSV + JSON)

  → Coordinator waits for all branches
  → Agent C's sub-agents resolve
  → Coordinator integrates, runs full test, ships
```

### Architecture

- **Coordinator agent** at depth 0 decomposes and assigns.
- **Worker agents** claim branches and recurse independently.
- **Handoff protocol** between levels (already built — handoff packets in OpenKeel).
- **Fan-out / fan-in** — coordinator waits for children, then integrates.
- **Directive system** — coordinator can re-prioritize or redirect agents mid-flight.

### Pros
- Fast. Parallel execution across branches.
- Natural for problems with independent sub-tasks.
- Agents can specialize (one might be better at parsing, another at auth).

### Cons
- Coordination overhead. Agents may duplicate work or conflict.
- Integration is hard — merging code from 4 agents can be messy.
- Needs robust conflict resolution (git merge, file locking, etc).

### Maps to OpenKeel
- Coordinator = agent with `role: coordinator`
- Workers claim tasks via existing `/api/task/<id>/claim`
- Directives for re-prioritization
- War room tracks the overall project state
- Activity feed shows parallel progress

---

## Approach 3: The Spiral (Iterative Deepening)

Instead of going deep on one branch, the agent does multiple passes over the ENTIRE problem, increasing resolution each time.

### How it works

```
Job: "Build a scraper for RealEstate.com"

Pass 1 (Resolution: sketch):
  → Fetch one page, extract one field, print to stdout
  → Result: "I can reach the site, data is in <div class='listing'>"
  → Discovered: pagination exists, auth might be needed, multiple field types

Pass 2 (Resolution: prototype):
  → Handle 10 pages, extract all visible fields, save to JSON
  → Result: working prototype, but breaks on page 11 (rate limit)
  → Discovered: rate limits, two layout variants, some fields are JS-rendered

Pass 3 (Resolution: robust):
  → Add rate limiting, handle both layouts, use Playwright for JS
  → Result: scrapes 100 listings successfully
  → Discovered: some listings have been removed (404s), need retry + dedup

Pass 4 (Resolution: production):
  → Add error handling, dedup, resume capability, structured logging
  → Result: scrapes full site, handles all edge cases
  → Ship.
```

### Architecture

- **Same agent, multiple passes.** Each pass covers the whole surface at increasing depth.
- **Resolution levels** are explicit: sketch → prototype → robust → production.
- **Each pass generates a test suite** that the next pass must still satisfy (ratchet — never regress).
- **Discovery log** captures what each pass revealed, feeding the next pass's focus areas.

### Pros
- Always have a working (if rough) version at every stage.
- Natural for problems where you don't know what you don't know.
- Easy to stop early — "prototype is good enough, ship it."
- Test ratchet prevents regression.

### Cons
- May revisit the same code many times (refactoring overhead).
- Not great for problems with one deep bottleneck (e.g., the whole thing hinges on cracking auth).

### Maps to OpenKeel
- Roadmap milestones = resolution levels (sketch, prototype, robust, production)
- Each pass creates a snapshot (git tag or branch)
- Test ratchet = milestone acceptance criteria
- Discovery log = Hyphae facts tagged per pass

---

## Approach 4: The Organism (Adaptive, Hybrid)

The most sophisticated model. Combines all three approaches. The system adapts its strategy based on what it discovers.

### How it works

The agent starts with a Spiral pass (broad, shallow). Based on what it discovers, it switches strategy per-branch:

- **Independent branches** → fan out to parallel agents (Swarm)
- **Deep, tangled branches** → single agent goes deep (Zoom)
- **Unknown territory** → another Spiral pass at higher resolution
- **Solved patterns** → pull from SkillLibrary, skip decomposition

```
Job: "Build a scraper for RealEstate.com"

Phase 1 — Spiral (sketch):
  → Rough pass, discover the shape of the problem
  → Classify branches:
    [Pagination]  → KNOWN PATTERN (SkillLibrary: "infinite scroll via XHR")
    [Auth]        → UNKNOWN, DEEP → assign Zoom agent
    [Parsing]     → INDEPENDENT, PARALLEL → fan out 2 agents
    [Output]      → TRIVIAL → resolve inline

Phase 2 — Execute per-branch strategy:
  → Pagination: instant (skill replay)
  → Auth: Zoom agent discovers it's actually rate-limiting, resolves
  → Parsing: 2 agents handle layout A and B in parallel
  → Output: done

Phase 3 — Spiral (integration):
  → Full integration pass at prototype resolution
  → Run end-to-end test
  → Discover: race condition when two scrapers write to same file
  → Zoom into that specific issue, resolve

Phase 4 — Spiral (production):
  → Hardening pass. Error handling, logging, resume.
  → Ship.
```

### Architecture

- **Strategy classifier** — after each pass, classifies branches by type:
  - `KNOWN` → SkillLibrary replay
  - `TRIVIAL` → resolve inline
  - `INDEPENDENT` → fan out (Swarm)
  - `DEEP` → assign single agent (Zoom)
  - `UNKNOWN` → another Spiral pass
- **Adaptive depth** — system tracks which branches are absorbing the most cycles and adjusts
- **Cross-pollination** — if Agent A discovers something relevant to Agent B's branch, directive fires

### Pros
- Most efficient. Uses the right strategy for each sub-problem.
- Scales naturally — trivial stuff resolves fast, hard stuff gets attention.
- SkillLibrary means the system gets faster over time.
- Cross-pollination prevents redundant discovery.

### Cons
- Most complex to build.
- Strategy classifier needs to be good (bad classification = wasted work).
- Debugging is harder when different branches use different strategies.

### Maps to OpenKeel
- Governance dispatcher classifies branch strategy
- SkillLibrary already exists for pattern replay
- Directives enable cross-pollination
- War room tracks adaptive state changes
- DriftDetector monitors if a branch is stuck and should switch strategy

---

## Shared Infrastructure (Needed for All Approaches)

### 1. Fractal Task Tree
Extend the existing task schema:

```sql
ALTER TABLE tasks ADD COLUMN fractal_depth INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN fractal_status TEXT DEFAULT 'pending';
  -- pending | exploring | decomposed | resolved | blocked
ALTER TABLE tasks ADD COLUMN resolution TEXT DEFAULT '';
  -- sketch | prototype | robust | production
ALTER TABLE tasks ADD COLUMN discovery_log TEXT DEFAULT '';
  -- JSON: what was learned at this node
ALTER TABLE tasks ADD COLUMN strategy TEXT DEFAULT 'zoom';
  -- zoom | swarm | spiral | skill_replay | trivial
```

### 2. Test Ratchet
Each node in the fractal tree has acceptance criteria. Once a test passes, it must never regress.

```python
class TestRatchet:
    def record_pass(self, task_id: int, test_name: str, result: bool)
    def check_regression(self, task_id: int) -> list[str]  # returns failed tests
    def acceptance_met(self, task_id: int) -> bool
```

### 3. Discovery Log
Each fractal cycle produces discoveries. These feed into the next level's decomposition.

```python
class DiscoveryLog:
    def log(self, task_id: int, discovery: str, severity: str)
    def get_discoveries(self, task_id: int) -> list[dict]
    def classify(self, discovery: str) -> str  # known | unknown | blocker
```

### 4. Resolution Snapshots
At each resolution level, save the state so you can always roll back.

```python
class ResolutionSnapshot:
    def save(self, task_id: int, resolution: str)  # git tag + state dump
    def restore(self, task_id: int, resolution: str)
    def diff(self, task_id: int, from_res: str, to_res: str) -> str
```

### 5. Fractal Visualizer
The roadmap tab in Command Board becomes a fractal tree view. Each node shows:
- Status (exploring / resolved / blocked)
- Strategy (zoom / swarm / spiral)
- Resolution level
- Progress bar
- Discovery count
- Test ratchet status

---

## Method Selection Guide

Each method is a tool — pick the one that fits the problem:

| Method | Best For | Agent Count | Speed | Depth |
|--------|----------|-------------|-------|-------|
| **Zoom** | Deep investigation, debugging, reverse engineering, tightly coupled code | 1 | Slow | Deep |
| **Swarm** | Parallel workstreams, frontend+backend+infra, build tasks | Many | Fast | Shallow |
| **Spiral** | Greenfield, exploration, "I don't know what I don't know", prototyping | 1 | Medium | Broad |
| **Organism** | Large complex projects, mixed sub-problems, long-running work | Adaptive | Adaptive | Adaptive |

**Rules of thumb:**
- If you can draw the branches upfront → **Swarm**
- If you can't → **Spiral** first, then Zoom where it gets hard
- If the problem is one deep hole → **Zoom**
- If it's a big project with everything → **Organism**

## Implementation

Built as `openkeel/fractal/` module:
- `engine.py` — FractalEngine, node tree, test ratchet, discovery log, skill library, serialization
- `methods.py` — ZoomMethod, SwarmMethod, SpiralMethod, OrganismMethod
- Integrates with kanban (tasks/subtasks), Hyphae (memory), agent system (dispatch)

The key insight: **the fractal isn't just a task structure. It's a way of thinking.** The agent doesn't plan the whole tree upfront. It discovers the tree by building. Each level of the fractal emerges from testing the level above.
