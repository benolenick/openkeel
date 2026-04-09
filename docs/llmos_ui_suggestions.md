# LLMOS UI Suggestions for the UI Agent

**From:** Critic/backend agent
**Date:** 2026-04-06
**For:** UI manager agent building the LLMOS prototype

These are backend-informed suggestions for UI elements that need to be built or improved. The backend guts exist — these notes tell you what endpoints/data are available and how the UI should present them.

---

## 1. Token Monitor Widget (BUILT — in prototype)

**Location:** Taskbar, right side, between systray icons and clock.
**Size:** ~80px wide, taskbar height (48px).

**What's there now:**
- 8 mini bars showing recent token activity (green=saved, amber=used, purple=cached)
- Percentage display ("38% saved")
- Click to expand into a panel

**Expanded panel shows:**
- Big savings percentage + lifetime count
- 20-bar history chart (stacked: saved/used/cached)
- Stats grid: cache hits, local routes, compressions, sessions
- Estimated cost saved in dollars

**Backend endpoints:**
- `GET /api/calcifer` → includes token stats
- WebSocket `/ws/calcifer` → streams `token_saved` events with amount + reason
- Daemon command: `calcifer_state` → includes `tokens_saved`, `tokens_saved_session`

**Design notes:**
- Bars should pulse green when a save happens (class `saving` triggers animation)
- The widget should feel alive — bars shift left as new data comes in
- Purple bars (cache hits) are the most satisfying — make them stand out slightly

---

## 2. Agent Hub (NEEDS UI)

**What it is:** A panel showing all connected agents — Claude CLI, OpenClaw instances, custom agents.

**Backend:** `lllm/os/agent_bridge.py`
- `GET /api/agents` → list all registered agents with status
- `POST /api/agent/dispatch` → send a task to an agent
- `GET /api/agent/tasks` → list recent tasks with status

**UI location:** Accessible from Calcifer's right-click menu or a dedicated taskbar icon.

**What to show:**
- **Agent cards** — one per registered agent:
  - Claude CLI: show path, availability, model
  - OpenClaw: show endpoint URL, health status, last heartbeat
  - Each card shows: name, type icon, status dot (green/yellow/red), capabilities tags
- **Task feed** — recent dispatched tasks with status (pending/running/completed/failed)
- **Dispatch bar** — type a task, auto-routes to best agent (or pick manually)

**Agent types and their icons:**
- Claude CLI → terminal icon or Anthropic logo
- OpenClaw → claw/robot icon
- Custom → gear icon

**Design notes:**
- This should feel like a "control room" — you're the operator, these are your agents
- Failed tasks should be red, completed green, running = animated pulse
- OpenClaw agents that are offline should still show but grayed out with "reconnect" button

---

## 3. Capability Browser (NEEDS UI)

**What it is:** Browsable list of everything Calcifer can do. Searchable, categorized, with enable/disable toggles.

**Backend:** `lllm/os/capabilities.py`
- `GET /api/capabilities` → all capabilities, filterable by category
- `GET /api/capabilities?category=settings` → filtered
- `POST /api/capability/search` → search by name/description
- `POST /api/capability/enable` / `disable` → toggle
- `POST /api/capability/schedule` → set cron schedule
- `GET /api/capability/learned` → user-learned habits

**UI location:** Calcifer right-click → "What can you do?" or a settings sub-panel.

**Categories (with icons):**
- System (CPU icon) — health, processes, hardware
- Apps (box icon) — install, remove, residue, updates
- Settings (gear icon) — Config Guardian, display, audio
- Files (folder icon) — search, manage, watch
- Network (wifi icon) — WiFi, Bluetooth, DNS, firewall
- Security (lock icon) — permissions, firewall
- Intelligence (brain icon) — proactive alerts, crash prediction
- Automation (clock/repeat icon) — scheduled tasks, learned habits
- Memory (database icon) — remember/recall, Hyphae
- Interface (palette icon) — Calcifer personality, notifications

**Each capability card shows:**
- Name + one-line description
- Enable/disable toggle
- Usage count ("used 47 times")
- Schedule indicator if automated ("Every Tuesday 9:45am")
- "Learned" badge for habits Calcifer picked up from your behavior

**Learned section at the top:**
- "Calcifer learned these from watching you"
- Show habits with their triggers ("Open Teams meeting — every Tuesday")
- Allow editing schedule, deleting, or adding new ones manually

---

## 4. Config Guardian Panel (EXISTS — needs live data)

**What's there now:** Static mockup with hardcoded drift cards.

**Backend:** `lllm/os/guardian.py`
- Daemon command: `guardian_status` → all protected settings + drift counts
- Daemon command: `guardian_drifted` → currently drifted settings
- Daemon command: `guardian_history` → recent drift events
- Daemon command: `guardian_protect` / `guardian_restore`
- WebSocket: drift events stream as system events

**What to change:**
- Replace hardcoded cards with dynamic data from `guardian_status`
- Real "Restore" and "Lock" buttons that call `guardian_restore` / `guardian_protect`
- Drift history timeline at the bottom
- Toast notification when a drift is detected (Calcifer shakes and speaks)

---

## 5. App Manager with Residue Detection (EXISTS — needs live data)

**What's there now:** Static mockup with hardcoded app list and uninstall data.

**Backend:** `lllm/os/app_residue.py`
- Daemon command: `app_residue_scan` → scan an installed app's files
- Daemon command: `app_residue_orphans` → find orphaned dirs (found 21 / 17.7GB on this machine)
- Daemon command: `app_residue_plan` → smart uninstall plan
- Daemon command: `app_residue_savings` → total reclaimable space

**What to change:**
- Replace hardcoded app list with real installed apps
- "+340 MB residue" badges from real scan data
- Uninstall panel shows real file paths and sizes
- "Orphaned files" tab showing the 17.7GB of junk with cleanup buttons
- Cleanup savings estimate prominently displayed

---

## 6. Notification Center (EXISTS — needs Calcifer integration)

**What's there now:** Basic notification panel from taskbar bell icon.

**Backend:** `lllm/os/notifications.py`
- Daemon command: `notifications` → pending notifications
- Daemon command: `notification_counts` → badge numbers
- Daemon command: `notification_dismiss` / `notification_action`
- WebSocket: new notifications stream in real-time

**What to change:**
- Notifications should feel like they come FROM Calcifer
- Each notification shows source icon (Guardian shield, Watchdog eye, Token flame)
- Actionable notifications have buttons inline ("Restore", "Dismiss", "Show details")
- Group by time: "Just now", "Earlier today", "Yesterday"
- Calcifer's speech bubble for urgent ones, feed for normal ones

---

## 7. Calcifer Desktop Presence

**What's there now:** CSS teardrop in taskbar + home screen.

**What should exist:**
- The pixel art Calcifer from `~/tools/calcifer.py` rendered on a canvas element
- OR keep the CSS version but make animations match the 7 states from `lllm/os/calcifer.py`
- Right-click Calcifer → context menu:
  - "What can you do?" → Capability Browser
  - "Show agents" → Agent Hub
  - "Token stats" → Token Widget (expanded)
  - "Settings" → Calcifer personality picker
  - "Show internals" → Developer panel (daemon logs, event bus, Hyphae queries)

---

## 8. CLI Agent Launchers

**Concept:** LLMOS can spawn CLI agents (Claude Code, OpenClaw CLI, custom scripts) as managed processes. Each gets a panel in the sidebar.

**How it works:**
- User says "start a Claude agent on this project" → LLMOS spawns `claude` in a terminal panel
- The terminal panel is a managed window — LLMOS can see its output, Calcifer tracks its progress
- Multiple agents can run simultaneously, each in their own sidebar entry
- Agents can be paused, stopped, or redirected

**UI elements:**
- Sidebar section: "Running Agents" — shows active CLI agents with status dots
- Each agent entry: name, status (running/idle/done), elapsed time, kill button
- Click to focus the agent's terminal window
- "Launch agent" button → picker for agent type (Claude, OpenClaw, Custom) + working directory

**Toggle:** "CLI Agent Integration" in capabilities → enable/disable. When disabled, LLMOS is purely GUI. When enabled, terminal panels can host agents.

---

## 9. The Workshop (NEEDS UI — backend built)

**What it is:** The native AI workspace app. One app, four workbenches. This is what OpenKeel becomes inside LLMOS.

**Backend:** `lllm/os/workshop.py` — 15 daemon commands

**Naming hierarchy:**
```
LLMOS (the OS)
  └── Calcifer (the spirit)
       └── The Workshop (the app)
            ├── Forge (chat/AI)
            ├── Anvil (CLI agents)
            ├── Loom (automations/OpenClaw)
            └── Lens (intelligence/stats)
```

**Layout:**
- Left sidebar: 4 workbench icons (Forge hammer, Anvil icon, Loom thread, Lens magnifier), recent sessions below, Calcifer + token % at bottom-left
- Center: active workbench content
- Bottom: persistent input bar that routes based on active workbench
- Launch with `Super+W` or from start menu/taskbar

**Forge workbench (chat):**
- Standard chat UI (like the current prototype chat, but inside Workshop)
- Session list in sidebar (recent conversations)
- Model indicator: "local" (green) or "claude" (amber) badge on each response
- Token savings per conversation shown subtly
- Backend: `forge_new`, `forge_send`, `forge_list`, `forge_get`, `forge_delete`

**Anvil workbench (CLI agents):**
- Split view: agent list on left, active terminal output on right
- Each agent card: name, status dot (green=running, gray=idle, red=failed), elapsed time, kill button
- "Spawn agent" button → type picker (Claude CLI, custom command) + working directory
- Real-time streaming output from managed subprocesses
- Backend: `anvil_spawn`, `anvil_list`, `anvil_stop`, `anvil_output`

**Loom workbench (automations):**
- Card grid of automations, each showing: name, schedule, status, last run, run count
- "Create automation" flow: name → type (schedule/webhook/event trigger) → configure → enable
- Connected OpenClaw instances as a special section
- Enable/disable toggle per automation
- Backend: `loom_create`, `loom_list`, `loom_trigger`, `loom_delete`

**Lens workbench (intelligence):**
- Dashboard layout: Calcifer state card, token stats (reuse token monitor widget), Guardian status, agent status
- Notification feed (recent)
- Proactive insights list
- This is the "what has Calcifer been doing?" view
- Backend: `lens` (aggregates all modules)

**Universal input bar:**
- Always at the bottom of the Workshop
- Routes based on workbench: Forge=chat, Anvil=agent command, Loom=create automation, Lens=search
- Backend: `workshop_route`

**Design notes:**
- The Workshop should feel like a power tool, not a toy — dark, focused, minimal chrome
- Forge is the default workbench (most users spend most time chatting)
- Anvil should feel like a mission control — you're the operator, agents are your workers
- The Workshop icon: a stylized anvil or hammer with Calcifer's flame

---

## Design Principles (from the critique doc)

1. **Depth gradient everywhere** — every card/widget should be expandable. Click for more detail, click again for internals.
2. **Calcifer is the personality** — notifications, responses, and proactive messages all come through Calcifer's voice.
3. **No modals** — panels slide, expand, float. Nothing blocks the screen.
4. **Keyboard-first for power users** — Super for bar, Ctrl+J for chat, Ctrl+Space for spotlight, Escape closes everything.
5. **Green = saved, amber = used, purple = cached** — consistent color language for token visualization everywhere.
6. **Offline-first** — everything must work without backend. Demo mode with hardcoded data as fallback.
