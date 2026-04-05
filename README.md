# OpenKeel

**Governance and mission continuity for AI coding agents.**

---

## The Problem

Two gaps emerge when running AI agents like Claude Code, Codex, or Gemini CLI on real infrastructure:

1. **No hard guardrails.** The agent can run `rm -rf /`, force-push to main, or leak credentials. Polite instructions in a system prompt don't stop a confused or runaway model. You need a rule layer that fires *before* the tool call executes — outside the model's reasoning loop.

2. **No mission continuity.** Every session starts cold. Context compacts and state is forgotten. A long-running task (pentest, multi-day refactor, incident response) loses its thread unless you manually re-inject context every time. This is tedious and error-prone.

OpenKeel solves both.

---

## Two Modules

### Constitution

A YAML rule file evaluated against every tool call before it executes. Rules match on tool name and command content (regex). Actions are `deny` (block + log), `alert` (allow + log), or `allow` (explicit pass). Rules can be scoped to active mission tags so pentest restrictions don't fire during normal coding.

### Keel

Mission state that persists across sessions. A mission is a YAML file with a goal, scope, constraints, and current status. OpenKeel injects the active mission into the agent's context at session start, resume, and after context compaction. The agent always knows what it's working on and what the limits are.

---

## Quick Start

**Install:**

```bash
pip install openkeel
```

**Initialize config:**

```bash
openkeel init
# Creates ~/.openkeel/config.yaml and ~/.openkeel/constitution.yaml
# from the bundled examples.
```

**Install hooks into Claude Code:**

```bash
openkeel hooks install
# Writes hook entries into ~/.claude/settings.json
# Hooks: PreToolUse (enforce), SessionStart (inject), Stop (drift)
```

**Test the constitution:**

```bash
openkeel constitution check "rm -rf /"
# => DENY  no-rm-rf-root  Blocked: recursive deletion at root level

openkeel constitution check "git status"
# => ALLOW  (no rule matched)
```

**Start a mission:**

```bash
openkeel mission new pentest-htb-cronos \
  --goal "Get user and root flags on HTB Cronos" \
  --scope "10.10.10.13" \
  --tags pentest

openkeel mission start pentest-htb-cronos
# Sets active mission. SessionStart hook will inject it automatically.
```

---

## CLI Reference

```
openkeel init
    Create ~/.openkeel/ with default config and constitution.

openkeel hooks install [--agent claude|codex|gemini]
    Wire hooks for the specified agent (default: claude).
    For Claude: writes into ~/.claude/settings.json.
    For Codex/Gemini: generates a wrapper script.

openkeel hooks uninstall [--agent claude|codex|gemini]
    Remove OpenKeel hooks.

openkeel hooks status
    Show which hooks are currently active.

openkeel constitution check "<command>"
    Evaluate a command string against the constitution and print result.

openkeel constitution lint
    Validate constitution.yaml syntax and regex patterns.

openkeel mission new <name> --goal <text> [--scope <text>] [--tags tag1,tag2]
    Create a new mission file.

openkeel mission start <name>
    Set the active mission (written to config.yaml).

openkeel mission stop
    Clear the active mission.

openkeel mission status
    Print the active mission summary.

openkeel mission list
    List all missions and their status.

openkeel drift
    Run the drift detector manually. Compares current session state
    against the active mission and logs divergence.

openkeel github-scout config
    Print a sample GitHub Scout config.

openkeel github-scout scan [--since-hours 24] [--limit 20]
    Find newly created interesting GitHub repositories and remember seen hits.

openkeel github-scout watch [--interval 900]
    Poll GitHub continuously and surface new matching repositories.
```

---

## How It Works

### Hooks

OpenKeel installs three self-contained Python scripts into `~/.openkeel/hooks/`:

- **openkeel_enforce.py** — Reads the tool call from stdin (Claude Code hook format), evaluates it against `constitution.yaml`, and exits non-zero to block or zero to allow.
- **openkeel_inject.py** — Reads the active mission and prints mission context to stdout. Claude Code's `SessionStart` hook output is shown to the model.
- **openkeel_drift.py** — Called on `Stop`. Reads session summary (if available) and compares against mission constraints. Logs anomalies.

Each hook script is self-contained: no imports from the openkeel package, no dependencies beyond the stdlib. This makes them robust — they work even if the package is updated, partially installed, or run in a different Python environment.

### Claude Code Integration

Claude Code's `~/.claude/settings.json` supports hooks at `PreToolUse`, `SessionStart`, and `Stop`. OpenKeel's Claude adapter reads and writes this file atomically, preserving all existing non-openkeel entries.

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "", "hooks": ["python ~/.openkeel/hooks/openkeel_enforce.py"] }
    ],
    "SessionStart": [
      { "matcher": "", "hooks": ["python ~/.openkeel/hooks/openkeel_inject.py"] }
    ],
    "Stop": [
      { "matcher": "", "hooks": ["python ~/.openkeel/hooks/openkeel_drift.py"] }
    ]
  }
}
```

### Constitution Rule Evaluation

Rules are evaluated top-to-bottom, first match wins. A rule matches when:
- `tool` matches the tool name (exact or glob), AND
- `match.pattern` matches the relevant field of the tool input, AND
- If `when_tags` is set, the active mission has at least one of those tags.

Unmatched tool calls are allowed by default.

### Mission Injection

At session start, the inject hook prints a structured summary of the active mission:

```
[OpenKeel Mission: pentest-htb-cronos]
Goal: Get user and root flags on HTB Cronos
Scope: 10.10.10.13
Tags: pentest
Status: in-progress
Constraints:
  - Stay within scope IP
  - No external network requests
  - Log all findings to ~/htb/cronos/
```

This appears in the model's context window before any user message.

---

## Configuration

Copy `config.example.yaml` to `~/.openkeel/config.yaml`:

```yaml
constitution:
  path: "~/.openkeel/constitution.yaml"
  log_path: "~/.openkeel/enforcement.log"

keel:
  missions_dir: "~/.openkeel/missions"
  active_mission: ""
  inject_on:
    - startup
    - resume
    - compact

hooks:
  output_dir: "~/.openkeel/hooks"
```

Copy `constitution.example.yaml` to `~/.openkeel/constitution.yaml` and edit to match your environment.

### GitHub Scout

For continuous repository discovery:

```bash
cp github_scout.example.yaml ~/.openkeel/github_scout.yaml
export GITHUB_TOKEN=ghp_your_token_here

openkeel github-scout scan --since-hours 24 --limit 10
openkeel github-scout watch --interval 900
```

Tune `include_topics`, `include_keywords`, `include_languages`, and `watch_owners` in `~/.openkeel/github_scout.yaml` to reflect the projects you care about right now. The scout keeps local state in `~/.openkeel/github_scout_state.json` so it only surfaces unseen matches.

---

## License

MIT. See [LICENSE](LICENSE).
