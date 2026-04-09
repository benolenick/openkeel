# OpenKeel — Project Instructions

Compact core. Legacy version at `CLAUDE.md.pre-v6.bak`. Load extras on demand.

## Hyphae — Project Memory (MANDATORY)

Hyphae runs at `http://127.0.0.1:8100`. Use it for past-work questions before guessing.

**Recall** (current project default scope):
```bash
curl -s -X POST http://127.0.0.1:8100/recall -H "Content-Type: application/json" \
  -d '{"query":"<terms>", "top_k":3}'
```
Use `"scope":{}` to search all projects. Keep `top_k` at 3 unless you really need more.

**Remember** on non-obvious decision / bug fix / discovery:
```bash
curl -s -X POST http://127.0.0.1:8100/remember -H "Content-Type: application/json" \
  -d '{"text":"<fact>", "source":"agent"}'
```

## Kanban — http://127.0.0.1:8200

Boards: `default` (project work), `monitor` (health checks), `todo` (Ben's quick list), `ops-watch` (legacy).

- Move: `curl -sX POST /api/task/{id}/move -d '{"status":"done"}'`
- Create: `curl -sX POST /api/task -d '{"title":"...","status":"todo","project":"...","board":"default"}'`
- Monitor board: status report via `/api/task/{id}/report` + Hyphae fact. Mark idle/stuck pipelines `blocked` immediately.

**Ben's to-dos**: if he says "I should...", "remind me to...", silently add to `todo` board.

## Edit Tool — Keep Edits Minimal

Cost scales with `len(old_string) + len(new_string)`.
- `old_string` = smallest unique substring (1-3 lines usually)
- Multiple small edits beat one huge edit
- Don't pad with unchanged context lines
- LocalEdit (`#LOCALEDIT: path | instruction`) exists but is rarely used; regular Edit tool is the default.

## Token Saver v6 (current)

Cache-saver proxy at `127.0.0.1:8787` is live as a systemd user service. It routes turns 3-way:
- "think hard" / "ultrathink" / "architect" / "audit" / "security" → **Opus**
- Short trivial Q without tool history → **Haiku**
- Everything else → **Sonnet** (default)

**One honest metric:** `python3 -m openkeel.token_saver.one_metric` — weekly `pool_units` (weighted: opus×1.0, sonnet×0.2, haiku×0.04). If it's not going down, nothing is working. Ignore all other savings claims.

All pre-v6 "51%/79%/40% savings" figures were inflated counterfactuals. Don't cite them.

## Amyloidosis Corpus — http://127.0.0.1:8101

~9,500 cardiac amyloidosis papers. Connect when Ben asks about his uncle's condition.
Server: `tools/amyloidosis_hyphae.py`. DB: `~/.hyphae/amyloidosis.db`.
Uncle: cardiac amyloidosis (type TBD). Treatment briefing sent to ck201@rogers.com (2026-03-29).

## Session Context

Project: `openkeel`. If lost, run `openkeel recall "<topic>"`. Save decisions with `openkeel remember "<fact>"`.

<!-- OPENKEEL:START -->

## OpenKeel Session Context

**Project:** openkeel
**Session started:** 2026-04-09 15:47

**Instructions:** If you lose track of what you're doing, run `openkeel recall "<topic>"` to search project memory. When you make a decision or discover something important, run `openkeel remember "<fact>" -p openkeel` to save it.

<!-- OPENKEEL:END -->
