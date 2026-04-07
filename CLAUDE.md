## Hyphae — Project Memory (MANDATORY)

Hyphae is the long-term memory system for this project. The API runs at `http://127.0.0.1:8100`. You MUST use it as described below.

### Session startup

At the **start of every conversation**, before doing any work, recall context relevant to the current session and project:

```bash
curl -s -X POST http://127.0.0.1:8100/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "openkeel project status recent work", "top_k": 10}'
```

This gives you awareness of recent decisions, ongoing work, and known issues.

### Before answering questions about past work

Whenever the user asks about **past work, campaigns, infrastructure, previous decisions, architecture choices, or anything that might have prior context**, search Hyphae BEFORE answering:

```bash
curl -s -X POST http://127.0.0.1:8100/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "<relevant search terms>", "top_k": 10}'
```

Do NOT rely on your own training data or assumptions for project-specific history. Hyphae is the source of truth.

### When something is unfamiliar — search across all projects

If the user asks about something you do not recognize or that might live in a different project, search Hyphae with **no project scope** to check all projects:

```bash
curl -s -X POST http://127.0.0.1:8100/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "<search terms>", "top_k": 10, "scope": {}}'
```

### Saving new knowledge

Whenever you **make a decision, discover something important, resolve a tricky bug, or learn a non-obvious fact** about the codebase or infrastructure, save it to Hyphae immediately:

```bash
curl -s -X POST http://127.0.0.1:8100/remember \
  -H "Content-Type: application/json" \
  -d '{"text": "<concise fact or decision>", "source": "agent"}'
```

Keep remembered facts concise and self-contained so they are useful when recalled later.

## Kanban Board — Project Tracker (http://127.0.0.1:8200)

The OpenKeel Command Board tracks all active projects, tasks, and automations. **Update it when working on tasks.**

### When to update the board

- **Starting work** on an existing task → move it to `in_progress`: `curl -s -X POST http://127.0.0.1:8200/api/task/{id}/move -H "Content-Type: application/json" -d '{"status":"in_progress"}'`
- **Completing a task** → move it to `done`: `curl -s -X POST http://127.0.0.1:8200/api/task/{id}/move -H "Content-Type: application/json" -d '{"status":"done"}'`
- **Discovering new work** during a session → create a card: `curl -s -X POST http://127.0.0.1:8200/api/task -H "Content-Type: application/json" -d '{"title":"...","description":"...","status":"todo","priority":"medium","type":"task","project":"...","board":"default"}'`
- **Hitting a blocker** → move to `blocked`: `curl -s -X POST http://127.0.0.1:8200/api/task/{id}/move -H "Content-Type: application/json" -d '{"status":"blocked"}'`

### Board layout
- `default` board: Active project tasks (todo/in_progress/done/blocked)
- `monitor` board: Always-running automations (healthy = done, broken = blocked)
- `ops-watch` board: Legacy, migrated to monitor

A Stop hook (`kanban_sync.py`) also runs automatically to catch completed work.
A Stop hook (`monitor_watchdog.py`) checks monitor-board tasks for staleness and auto-blocks them if Hyphae facts indicate issues.

### Monitor board — MANDATORY progress reporting

When working on or checking a task tracked on the `monitor` board (automations, pipelines, long-running processes):

1. **Always report progress** via the task report endpoint:
   ```
   curl -s -X POST http://127.0.0.1:8200/api/task/{id}/report \
     -H "Content-Type: application/json" \
     -d '{"agent_name":"claude","status":"done|blocked","report":"<what happened>"}'
   ```
2. **Always save a Hyphae fact** with the current status so future sessions and the watchdog can pick it up.
3. **If a pipeline/automation is idle, stalled, or erroring** — move the monitor task to `blocked` immediately with an explanation. Do NOT leave it as `in_progress` if it's not actually doing work.
4. **If you discover a monitor task is stale** (description doesn't match reality) — update it immediately.

This is critical. Stale monitor tasks waste resources (GPUs burning power on idle processes) and mislead future agents.

### Ben's To-Do List (`todo` board)

If Ben mentions something that needs to get done — even casually ("I should...", "remind me to...", "we need to...", "don't let me forget...", "I gotta...", "oh yeah I still have to...") — **add it to the `todo` board immediately**:

```bash
curl -s -X POST http://127.0.0.1:8200/api/task \
  -H "Content-Type: application/json" \
  -d '{"title":"...","description":"...","status":"todo","priority":"medium","type":"task","project":"personal","board":"todo"}'
```

Do this silently — don't ask "should I add this?", just add it and briefly confirm. The `todo` board is Ben's personal quick-capture list, separate from the project task board (`default`).

## Amyloidosis Research Corpus (http://127.0.0.1:8101)

A dedicated Hyphae instance loaded with ~9,500 peer-reviewed papers on cardiac amyloidosis (ATTR, AL, treatments, diagnostics, pathophysiology, clinical trials). **Connect to it when the user asks about amyloidosis, their uncle's condition, or says "connect to amyloidosis".**

### How to use it

**Search the corpus:**
```bash
curl -s -X POST http://127.0.0.1:8101/recall \
  -H "Content-Type: application/json" \
  -d '{"query": "<search terms>", "top_k": 20}'
```

**Example queries:**
- Treatment options: `"novel treatment ATTR cardiac amyloidosis"`
- CRISPR/gene therapy: `"CRISPR gene editing transthyretin knockdown"`
- Drug comparisons: `"tafamidis vs patisiran cardiac outcomes"`
- Clearing deposits: `"amyloid deposit clearance cardiac regression"`
- Diagnostics: `"early diagnosis cardiac amyloidosis imaging biomarkers"`
- Prognosis: `"ATTR cardiomyopathy survival prognosis staging"`

**When connected**, always search this corpus BEFORE answering medical/treatment questions. Cross-reference multiple papers. Cite PMIDs when possible.

### Server management

The server runs from: `tools/amyloidosis_hyphae.py`
Database: `~/.hyphae/amyloidosis.db`
Start if down: `/home/om/Desktop/Hyphae/hyphae/.venv/bin/python /home/om/openkeel/tools/amyloidosis_hyphae.py &`

### Important context
- Ben's uncle has cardiac amyloidosis (type TBD — ATTR vs AL)
- Papers sourced from PubMed + PMC (full text where available)
- Corpus also overlaps with 58,629 amyloid-related papers in the Chemister corpus on jagg
- Email with treatment options + clinical trial links sent to Ben's dad at ck201@rogers.com (2026-03-29)

## LocalEdit — Token-Saving Edit Delegation (PREFERRED)

For **simple, mechanical edits** (value changes, renames, adding/removing a line, config tweaks), use LocalEdit instead of the Edit tool. It delegates the edit to a local LLM and saves ~95% of tokens.

**CURRENT SETUP (verified 2026-04-07):** The fast path runs `qwen2.5:3b` on jagg's RTX 3090 at **~200 tok/s** (faster than Claude Sonnet's ~60-100 tok/s streaming). A real LocalEdit call completes in ~0.7 seconds. Complex edits auto-escalate to `gemma4:26b` on the same 3090 (~30 tok/s). You should ALWAYS prefer LocalEdit for any mechanical change — it's both cheaper AND faster than doing it yourself.

**HONEST NOTE:** Historically LocalEdit has only fired ~20 times across 497 sessions because I (Claude) kept forgetting to use it. Don't be that Claude. When the edit fits on one line of plain English, use `#LOCALEDIT:` — no excuses.

### How to use

Run a Bash command with the `#LOCALEDIT:` prefix:

```bash
#LOCALEDIT: /path/to/file.py | Change TIMEOUT from 30 to 60
#LOCALEDIT: /path/to/file.py | Add "import os" after "import sys"
#LOCALEDIT: /path/to/file.py | Remove the line that says "# TODO: fix this"
#LOCALEDIT: /path/to/file.py | Rename the variable old_name to new_name
```

### When to use LocalEdit vs Edit tool

The local LLM auto-scales based on available GPU (currently Tier 2 — gemma4:26b on jagg). Use LocalEdit aggressively:

| Use LocalEdit | Use Edit tool |
|---|---|
| Change a value/constant | Writing brand new files from scratch |
| Rename a variable/string | Changes requiring pixel-perfect formatting |
| Add/remove lines | Edits where a mistake would be catastrophic |
| Config/import changes | |
| Add a function or method | |
| Multi-line refactors | |
| Swap out logic blocks | |
| Add error handling to a function | |
| Basically anything you can describe in a sentence | |

**Default to LocalEdit.** Only fall back to Edit when the instruction is too complex to describe in plain English, or when you need guaranteed precision.

### Important notes
- LocalEdit creates a `.localedit.bak` backup automatically
- Always verify the diff output — gemma4 is a small model and occasionally gets it wrong
- If LocalEdit fails, fall back to the Edit tool — don't retry more than once
- Keep instructions short and specific — "Change X to Y" works better than long descriptions

## Edit Tool — Keep Edits Small (MANDATORY)

Every Edit call costs tokens proportional to the size of `old_string` + `new_string`. **Keep edits as small as possible:**

- **old_string should be the MINIMUM needed for uniqueness** — 1-3 lines, not entire blocks. If one line is unique, send one line.
- **Never replace a 50-line block when you can make 3 separate 2-line edits.** Multiple small edits are cheaper than one huge edit.
- **Split large changes into multiple Edit calls.** Each call should change one thing. Example: adding 3 functions = 3 Edit calls, not one giant block replacement.
- **Don't include unchanged context lines** in old_string just for safety. Find the unique snippet.

Bad (55K tokens):
```
Edit(old_string="<entire 200-line function>", new_string="<entire 200-line function with 1 line changed>")
```

Good (200 tokens):
```
Edit(old_string="    timeout = 30", new_string="    timeout = 60")
```

<!-- OPENKEEL:START -->

## OpenKeel Session Context

**Project:** openkeel
**Session started:** 2026-04-07 00:09

**Instructions:** If you lose track of what you're doing, run `openkeel recall "<topic>"` to search project memory. When you make a decision or discover something important, run `openkeel remember "<fact>" -p openkeel` to save it.

<!-- OPENKEEL:END -->
