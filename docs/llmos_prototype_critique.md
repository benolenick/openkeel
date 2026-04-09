# LLMOS Prototype Critique

**File:** `docs/llmos_prototype.html`
**Reviewed:** 2026-04-06
**Reviewer:** Critic agent (Claude Opus)

---

## What's working well

- **Visual language is solid.** Dark theme, Inter font, green accent, rounded corners — it reads as modern OS, not "web app pretending to be OS." The ChatGPT influence is the right call for the input bar.
- **Config Guardian is the killer feature.** The drift detection cards with inline diffs, "Restore & lock" buttons, and the proactive toast notification ("GNOME 46.1 reset your scaling — I restored it") — this is the single most compelling thing in the prototype. This alone sells LLMOS over stock Linux.
- **App Manager shows real thought.** Residual file detection, per-app breakdown of config/cache/data, "Remove app (keep games)" — this solves a problem every Linux user has. The "+340 MB residue" badge is a nice touch.
- **The chat responses embed controls inline.** The scaling slider inside the chat response is exactly right — don't send users to Settings, bring Settings to them.

---

## Critical problems

### 1. The "it just works" to "I want to dig into anything" spectrum doesn't exist yet

The prototype has two modes: click things, or type in the bar. There's no **depth gradient**. A real spectrum looks like:

| Layer | User | Example |
|-------|------|---------|
| L0: Just works | Anyone | LLMOS silently restores your scaling after an update. You never know it happened. |
| L1: Notification | Curious | Toast: "I restored your display scaling." Tap to dismiss. |
| L2: Explanation | Interested | Tap "View details" → see what changed, why, when. Plain English. |
| L3: Controls | Intermediate | From the detail view, toggle "Always protect" or "Let it change." |
| L4: Internals | Technical | "Show me the dconf key" → see `org.gnome.desktop.interface scaling-factor`, the systemd timer that watches it, the Hyphae history of every drift event. |
| L5: Code | Power user | "Show me the Guardian source" → opens the actual Python file in the terminal/editor. |

**The prototype only has L1 and L3.** There's no path from toast → full technical detail. The Guardian cards show drift but don't let you drill into the underlying system mechanism. The chat window is L2 at best — it gives canned responses, not actual system data.

**Fix:** Every card, every notification, every chat response needs a "dig deeper" affordance. Not a menu — just a subtle expansion. Click the scaling card → see the dconf path. Click again → see the journald log entry. Click again → open a terminal pre-filled with the relevant command.

### 2. The input bar is a dead end after first interaction

When you type something, it either launches an app or opens a maximized chat window. The desktop **disappears**. This is wrong for two reasons:

- **Quick queries shouldn't take over the screen.** "What's my IP?" should answer inline below the input bar, like Spotlight on macOS. Only longer conversations should open a chat window.
- **The bar vanishes when you need it most.** Once a chat opens, the bar is gone. You have to go Home to get it back. The bar should be omnipresent — either as a persistent element at the top of every window, or as a Super-key overlay (like macOS Spotlight / Windows PowerToys Run).

**Fix:** Two response modes:
- **Inline/ephemeral**: answer appears below the bar, fades after 10s or on click-away. For quick lookups, calculations, app launches.
- **Session**: opens a chat panel (not necessarily maximized) for multi-turn conversations.

The bar should detect which mode is appropriate. One sentence answer = inline. Needs follow-up = session.

### 3. Chat windows maximize by default — wrong mental model

Line 911: `toggleMaximize(id)` — every chat goes fullscreen. This makes LLMOS feel like "ChatGPT the OS" instead of "an OS with intelligence." Users should be able to:
- Have a chat open alongside the file manager (split view)
- Resize the chat to a sidebar
- Pop the chat out as a small floating panel

**Fix:** Chat windows should open as a right-side panel (40% width), not fullscreen. Fullscreen should be opt-in via maximize button. The conversation is a helper, not the main event.

### 4. No awareness of active context

The input bar and chat have zero knowledge of what the user is doing. If I have the file manager open showing `/home/om/Documents`, typing "compress this folder" should know which folder. If I have the system monitor open, "why is ollama using so much memory?" should already have the process info.

The prototype's `getAIResponse()` is entirely keyword-based with no window/context awareness.

**Fix:** The chat system needs a `getActiveContext()` function that reads:
- Which window is focused
- What's selected/visible in that window
- Recent user actions (opened file, changed setting, etc.)

This context gets prepended to every LLM query. This is what makes LLMOS feel intelligent vs. being a search box.

### 5. No proactive surface

The prototype has a single toast notification that fires on a timer. But the core thesis of LLMOS is **proactive intelligence** — it should be surfacing things without being asked:

- "Your disk will be full in ~3 days at current rate"
- "Firefox hasn't been updated in 45 days — there are 2 security patches"
- "The NVIDIA driver update from yesterday broke CUDA for 12% of users on your GPU — I'm holding it back"

These shouldn't be toasts that disappear. They should accumulate in an **ambient feed** — maybe a subtle indicator on the taskbar that shows "3 things to review" with a panel that slides up.

**Fix:** Add a notification center / proactive feed. The bell icon in the taskbar should open a panel showing all pending observations from the watchdog/analyzers. Each one expandable with the L0-L5 depth gradient.

### 6. Settings app is just a clone — misses the LLMOS thesis

The Settings window is a standard GNOME-style settings panel. For LLMOS, settings should be conversational:

- Open Settings → see a summary: "Everything looks good. 12 configs protected, display scaling was restored today."
- Type in the settings search: "make text bigger" → it adjusts scaling and explains what it did
- Every setting shows its protection status (Guardian badge)
- Every setting shows its history ("Changed 3 times in the last month")

The current version is a static form. It doesn't leverage Hyphae or the LLM at all.

### 7. Terminal is decorative

The terminal shows a static prompt with no input handling. For the prototype that's fine, but architecturally: the terminal and chat should be **the same thing with different modes**.

- Chat mode: natural language in, structured response out
- Terminal mode: commands in, stdout out
- Hybrid: type natural language in terminal → LLMOS translates to command, shows it, asks to execute

The sidebar should show both chat sessions and terminal sessions in the same list — they're all conversations with the system.

### 8. No keyboard-driven flow

There are no keyboard shortcuts. Power users will want:
- `Super` → opens/focuses the bar (like Spotlight)
- `Super + T` → terminal
- `Super + E` → files
- `Ctrl + J` → toggle chat panel
- Arrow keys in the bar → navigate suggestions
- `Tab` to accept inline completion

The prototype is mouse-only.

---

## Missing features for "it just works" → "highly technical" spectrum

### For "it just works" users:
- **Onboarding flow**: first-boot wizard that asks "What do you use your computer for?" and configures defaults
- **Activity-based suggestions**: "You haven't backed up in 2 weeks. Want me to set that up?" (proactive, not nagging)
- **Plain English errors**: when something fails, the chat should explain it. No stack traces, no error codes — just "Your WiFi disconnected because the driver crashed. I restarted it. This has happened 3 times — want me to switch to a more stable driver?"

### For technical users:
- **Inspector mode**: hover any UI element → see what system component it maps to, what config file, what process
- **Command palette**: `Ctrl+Shift+P` → fuzzy-search every action in the OS (VS Code style)
- **Live system graph**: a visual map of running services, their dependencies, resource usage — clickable, drillable
- **Hyphae browser**: search and browse all system memory — "show me every time my display scaling changed" → timeline view

### For the bridge between them:
- **"Explain this" button**: on every error, every notification, every config card. Opens a chat with the context pre-loaded.
- **Breadcrumbs**: from any deep technical view, show the path back to the simple view. "Display Settings > Config Guardian > dconf key > journal log" — click any breadcrumb to go back to that level of abstraction.
- **Progressive terminal**: type a natural language command, see the bash equivalent, learn over time. Eventually the user types bash directly because they've been taught.

---

## Architecture notes for implementation

The prototype is a single HTML file with hardcoded responses. For the real thing:

1. **Backend**: Flask/FastAPI daemon on localhost. The HTML/JS frontend talks to it via WebSocket.
2. **Context bus**: every window reports its state to the daemon. The daemon passes this context to the LLM with every query.
3. **Tool registry**: each "app" registers its capabilities (settings can change display, files can compress/delete, terminal can run commands). The LLM knows what tools are available and calls them.
4. **Hyphae integration**: every query checks Hyphae first. "Why is my fan loud?" → Hyphae recalls "fan became loud after NVIDIA 560 driver update on March 30" → LLM connects this to the current state.
5. **Token saver**: the context bus and tool responses go through token saver before hitting the LLM. Compress system state into efficient summaries. Don't send 500 lines of `journalctl` — send the 3 relevant lines.
6. **Streaming**: all LLM responses stream. The inline bar response should start appearing within 200ms (local model). Show a thinking indicator, then stream the answer.

---

## The Workshop — Native AI Workspace

OpenKeel is now **The Workshop** — the native app for all AI/agent work inside LLMOS. Blacksmith/fire-themed to match Calcifer.

**Implementation:** `lllm/os/workshop.py` (580 lines, 15 daemon commands)

### Four workbenches

| Workbench | Purpose | Icon metaphor |
|---|---|---|
| **Forge** | Chat with AI (local or Claude, streamed) | Hammer + flame |
| **Anvil** | CLI agent workspace (spawn, manage, monitor) | Anvil |
| **Loom** | Automations (OpenClaw, scheduled tasks, webhooks) | Thread/weave |
| **Lens** | System intelligence (tokens, Guardian, insights) | Magnifier |

### How it fits

- The home screen input bar is a lightweight Forge — deep conversations say "Open in Workshop"
- `Super+W` opens Workshop, start menu has it pinned
- Calcifer lives in both places — desktop ambient + Workshop active assistant
- "Workshop" replaces "OpenKeel" as the brand for all AI infrastructure
- Token saver, fractal engine, agent bridge, capabilities — all "Workshop internals"

### Agent installation flow

Users add agents through Workshop Settings → Agents → "Add Agent":

1. **Pre-configured agents** (one-click install):
   - Claude CLI — detects if installed, offers to install via npm/pip, auto-authenticates
   - OpenClaw — enter endpoint URL, API key, test connection
   - Local models — shows available Ollama models, one-click pull

2. **Custom agents** — provide: name, command/endpoint, capabilities, working directory

3. **Auto-discovery** — Workshop scans for known agent CLIs on PATH (claude, aider, continue, cursor) and offers to register them

The install flow for Claude CLI:
- Click "Add Claude CLI" → Workshop runs `which claude || npm install -g @anthropic-ai/claude-code`
- If not authenticated → opens browser for auth flow, detects when complete
- Registers as Anvil agent → available immediately

---

## Calcifer — The Ambient AI Mascot

### The Calcifer Principle

Calcifer is named directly after Calcifer from Howl's Moving Castle. He wasn't a pet — he was **infrastructure with a face**. He literally powered the castle, had personality, grumbled when overworked, but you'd be devastated if he went out.

Calcifer's deal is: **he lives inside your computer, he feeds on the work he does, and in exchange he keeps everything running.** Token saving isn't a feature — it's Calcifer eating. The efficiency aura isn't a progress bar — it's how well-fed he is. When the system is healthy and tokens are being saved, Calcifer is content. When the GPU is maxed and the disk is full, he's stressed and overworked.

People don't "use" Calcifer. They **live with** him. Over time he learns their machine so well that problems get solved before anyone notices.

### Visual design — already built

**Implementation:** `~/tools/calcifer.py` — 8-bit pixel art flame rendered in tkinter.

- Warm brown-orange teardrop body, fire-red flame prongs on top and sides
- Dark round eyes with white shine dots, simple mouth (closed/open states)
- Sits on a log (the machine he inhabits)
- Embers float up constantly — he's always alive, always burning
- Color palette: `#c0522e` (shadow) → `#d4764a` (body) → `#e08850` (highlight) → `#ee5533` (flame tips)

### Existing animations (from calcifer.py)

| Animation | Trigger | What it looks like |
|---|---|---|
| **idle** | Default state | Gentle wobble, occasional blink, embers floating up |
| **jump** | Token save burst, good news | Hops up and bounces back down |
| **shake** | Error detected, frustration | Rapid side-to-side vibration (angry Calcifer) |
| **shrink** | Scared/startled, unexpected event | Gets small, then pops back |
| **flare** | Excited, big achievement | Gets brighter, extra flames, open mouth, double embers |
| **sleep** | Screensaver, idle system | Droops down, eyes always closed, zzz particles float up |
| **dance** | Celebration, milestone | Rhythmic left-right sway with bounce |
| **blink** | Random (every 15-50 frames) | Eyes close briefly, natural behavior |
| **mouth open** | Talking, responding | Mouth opens when speaking/active |

### What Calcifer does — the full spec

Calcifer is the **single interface to everything LLMOS does**:

#### 1. The face you see (system status)

Calcifer's animation IS your system status. No dashboard needed.

- System healthy → idle, content, embers floating gently
- GPU busy → **flare** (brighter, hotter, more flames — he's working hard)
- Error/problem → **shake** (something's wrong, he's frustrated)
- Something unexpected → **shrink** (startled, then recovers)
- Long idle / screensaver → **sleep** (zzz particles, drooped, eyes closed)
- Auto-fixed something → **jump** (satisfied hop)
- Milestone (1M tokens saved) → **dance** (celebratory sway)

#### 2. The mouth that speaks (conversational interface)

When you type in the bar, Calcifer answers. Not "an AI responds" — Calcifer responds. With personality, memory, and context about what you're doing now and what happened last Tuesday.

- "Why is my fan loud?" → Calcifer checks thermals, GPU load, Hyphae history, gives the answer
- "Open Firefox" → just does it, no chat needed
- "Uninstall Steam" → shows everything Steam left behind, asks what to keep

The input bar doesn't say "Ask Calcifer." It says "Message LLMOS." But the responses have Calcifer's personality, Calcifer's avatar, Calcifer's wit. Users anthropomorphize naturally: "Calcifer caught another config drift."

#### 3. The guardian that protects (Config Guardian)

Calcifer runs Config Guardian. When GNOME resets display scaling at 3am, Calcifer catches it, fixes it, tells you in the morning with a shrug:

*"They tried again. I handled it."*

- Watches dconf, systemd, MIME handlers, kernel params
- Remembers YOUR settings via Hyphae
- Auto-restores on drift, or asks if ambiguous
- Every config change saved as a Hyphae fact — survives reinstalls

#### 4. The engine that saves tokens (token saver)

Token saving is Calcifer eating. This is how he stays alive.

Every time token saver intercepts a redundant file read, caches a tool result, or routes a task to local Ollama instead of Claude — Calcifer fed. The efficiency aura fills. Particles burst. Users see their little guy getting stronger.

- Pre-tool interception (cache hits, file skeletons)
- Task routing (simple → local Ollama, complex → Claude)
- Context compression (summarize old conversation turns)
- **The token counter is Calcifer's health bar**
- **Flare animation** on big saves, **jump** on cache hits

#### 5. The watchdog that never sleeps (tiered monitoring)

Calcifer runs the tiered watchdog from ~/lllm:

- **Tier 0** (0.6ms): /proc + NVML — CPU, RAM, GPU, thermals
- **Tier 1** (60s): TCP pings to network services
- **Tier 2** (5min): subprocess health checks
- **Tier 3** (30min): LLM analysis — pattern matching against Hyphae history

When something's wrong, Calcifer doesn't pop up a modal. He **shakes**. His color shifts. You notice on your own terms.

#### 6. The memory that accumulates (Hyphae)

Calcifer IS Hyphae's interface. Every event, every fix, every pattern stored. When you ask "why does my WiFi keep dropping?", Calcifer doesn't just check current state — he pulls the last 6 times it happened, what fixed it, and whether there's a pattern.

Over months, Calcifer knows your computer better than you do.

#### 7. The bridge to the CLI (terminal integration)

When Calcifer can't do something in the GUI, he opens a terminal with the command pre-filled. "Install Docker" → shows the commands, explains what each does, offers to run them.

- For power users: natural language → bash translation
- For beginners: bash → English translation
- **Progressive terminal**: type natural language, see the bash equivalent, learn over time

#### 8. The auto-remediator (self-healing)

Calcifer doesn't just detect — he fixes when confident:

- DNS fails → flush cache, retry, save fix to Hyphae, **jump** animation
- Disk at 90% → identify biggest cache/log offenders, show you, offer to clean
- NVIDIA driver breaks → roll back, remember, block that version next time, **shake** then **jump**

When he auto-fixes: sparkle, one-liner. *"Flushed DNS cache. You're back online."*

### What Calcifer does NOT do

- **Not a chatbot.** No small talk. No "How can I help you today?" He waits until he has something real.
- **Never changes things silently.** Auto-remediation is visible. You always see what happened.
- **Never pretends to know.** If Hyphae has no history and the LLM isn't sure: "I don't know, but here's where to look."
- **Never nags.** One notification per issue. Dismiss it, he remembers. Only brings it up again if it gets worse.

### The depth spectrum

Same Calcifer, three depths. Casual users never need to know there's a token saver.

| Depth | What you see |
|---|---|
| **Casual** | Cute flame buddy, animations, occasionally speaks in plain English. Love him like a Tamagotchi. |
| **Intermediate** | Click Calcifer → see what he's doing. Token counts, cache hits, system checks. Start understanding the system. |
| **Power** | Right-click → "Show internals" → full dashboard: token saver stats, Hyphae query log, watchdog events, LLM routing decisions. Configure thresholds, add watchers, see every decision. |

### Personality: dry wit, competent, slightly tired of GNOME's nonsense

- Not silent (sterile), not enthusiastic (Clippy), not robotic
- Competent colleague with understated humor
- *"Your display scaling got reset again. Third time this month. I fixed it. You might want to file a GNOME bug, or just let me keep doing this forever."*
- Dead serious when it matters: *"Your backup drive is failing. Here's what to do right now."*
- **Personality slider in Settings**: Minimal / Friendly / Sarcastic

### Why this works where Clippy failed

| Clippy | Calcifer |
|---|---|
| Interrupted with bad guesses | Only speaks when watchdog/analyzers have something real |
| No memory — same tips every time | Backed by Hyphae — knows your entire system history |
| Couldn't actually do anything | Can auto-fix, configure, protect, investigate |
| Appeared randomly | Lives in a consistent place, you engage on your terms |
| Annoying because wrong | Endearing because right |
| A paperclip | A fire demon sitting on a log. Obviously better. |

---

---

## Desktop essentials still missing

### 1. Pinned apps (taskbar + desktop)

**Problem:** The taskbar only shows open windows. Users need persistent quick-launch pins — their browser, terminal, file manager always one click away, even when not running.

**What to build:**
- **Taskbar pins**: Left section of taskbar (after start button) holds pinned app icons. Running apps get an underline dot, pinned-but-not-running apps are static.
- **Desktop shortcuts**: Optional. Some users want icons on the desktop surface. Right-click desktop → "Add shortcut" or drag from start menu.
- **Pin from anywhere**: Right-click any running app in taskbar → "Pin to taskbar." Right-click in start menu → "Pin to desktop."
- **Backend**: Store pinned apps in `~/.config/lllm/pinned.json`. Load on startup. The capability registry already tracks frequently used apps — auto-suggest pins for top 5.

### 2. Wallpaper

**Problem:** Background is a flat CSS gradient. Users expect to set their own wallpaper.

**What to build:**
- **Wallpaper picker** in Settings → Appearance: browse images, set solid color, or keep gradient.
- **Store**: `~/.config/lllm/wallpaper` path saved, applied as CSS `background-image` on `.desktop`.
- **Calcifer integration**: "Set my wallpaper to that photo I took yesterday" → Calcifer searches recent files → applies it.
- **Config Guardian**: Protect wallpaper setting so updates don't reset it.
- **Dynamic wallpaper**: Optional — wallpaper shifts based on time of day (dark at night, light in morning). Calcifer can suggest this during onboarding.

### 3. Gaming support

**Problem:** Zero game awareness. No Steam/Lutris/Heroic integration, no game mode.

**What to build:**
- **Game launcher detection**: Detect installed launchers (Steam, Lutris, Heroic, Bottles) via desktop files and common paths.
- **Game mode**: When a game launches, Calcifer enters a special state:
  - Suppress non-urgent notifications
  - Boost GPU priority (nice -n -5 the game process)
  - Reduce watchdog frequency (don't burn CPU on monitoring during gaming)
  - Calcifer goes to a "focused" animation (still, attentive, not distracting)
  - Show FPS/GPU overlay if requested
- **Steam integration**: Read `~/.steam/steam/steamapps/` for installed games. Show them in the start menu under "Games" category.
- **Proactive**: "NVIDIA released driver 570. 3 users with your GPU reported frame drops. I'm holding it back." — the update guardian already does this, just needs game-specific awareness.
- **After gaming**: Calcifer reports: "You played Elden Ring for 2 hours. GPU peaked at 78°C. No thermal throttling."

### 4. Calcifer Tamagotchi System (BUILT — in prototype)

**Concept:** Calcifer's health IS your system's health. Not a fake pet — a real-data lens on your machine's state, with gamification that makes system maintenance feel rewarding.

**Vital Stats (mapped to real metrics):**

| Stat | What it measures | How it grows |
|---|---|---|
| **Warmth** (orange) | Token savings rate | Cache hits, local routing, efficient queries |
| **Vigilance** (green) | Config Guardian coverage | More settings protected, fewer drifts |
| **Memory** (purple) | Hyphae depth | More facts stored, better recall accuracy |
| **Stamina** (blue) | System uptime & health | Low temps, no crashes, services healthy |
| **Wit** (amber) | Response quality | User accepts suggestions vs dismisses them |

**Visual state changes:**
- Well-fed (high warmth): brighter flames, more embers, content expression
- Hungry (low warmth): dimmer, smaller, fewer embers — starving for tokens
- Alert (high vigilance): eyes sharp, quick reactions — on guard
- Sick (system unhealthy): disk at 95% → sweating; GPU overheating → angry red flames; Hyphae down → confused, can't remember

**Achievements (8 badges, unlock by real usage):**
- First Drift — Guardian blocks first config change
- Night Owl — Protected past midnight 50 times
- Cache King — 1,000 cache hits
- 1M Fed — 1M tokens saved (dance celebration)
- Librarian — Hyphae hits 10,000 memories
- Immune — Auto-remediated 10 issues without user intervention
- 100 Days — Uptime milestone
- Polyglot — Used 5+ different LLM models

**What it is NOT:**
- No breeding, eggs, or collection mechanics
- No death — Calcifer can get weak but never dies (he's infrastructure)
- No competitive element — about your machine, not leaderboards
- No artificial urgency — doesn't nag, just looks sad

**Implementation:** Stats panel in Calcifer's status popup (click him in taskbar). Stats update in real-time when events happen (feed → warmth up, guardian restore → vigilance up, chat → memory+wit up).

### 5. Native app deep integration

**Problem:** LLMOS launches apps but doesn't deeply integrate with them. Chrome tabs, file manager paths, terminal sessions are opaque.

**What exists now (context bus):**
- Window title parsing (gets URL from browser, file path from editor, directory from terminal)
- Active window tracking via xdotool
- Clipboard monitoring

**What's missing:**
- **Browser extension**: A tiny extension that reports the current URL, selected text, and page title to LLMOS via localhost WebSocket. This lets "summarize this page" or "save this article" work precisely.
- **File manager integration**: When Nautilus/Thunar is focused, know the current directory and selected files. Currently inferred from window title — works for most file managers but not all.
- **Terminal session tracking**: When a terminal is focused, know the running command and working directory. Currently reads `/proc/{pid}/cwd` which works but misses the active command.

### 5. Desktop widgets

**Problem:** The desktop surface is empty except for the input bar. Users may want persistent widgets — clock, weather, system monitor, Calcifer, token stats.

**What to build:**
- **Widget framework**: Draggable, resizable panels that live on the desktop surface (below windows, above wallpaper).
- **Built-in widgets**: System monitor (CPU/RAM/GPU bars), Token stats (the expanded panel but as a desktop widget), Calcifer (larger pixel art version), Clock, Weather (if network available).
- **Right-click desktop → "Add widget"** to place them.
- **Auto-hide when windows are open**: Widgets fade/hide when a window covers them, reappear when you show desktop.

---

## Priority order for next iteration

### Completed (Depth 0 + Depth 1)
- ~~Calcifer mascot~~ ✓ (CSS + pixel art + state machine)
- ~~Inline responses for the bar~~ ✓ (prototype wired)
- ~~Context awareness~~ ✓ (context_bus.py)
- ~~Proactive feed / notification center~~ ✓ (notifications.py)
- ~~Token saving visualization~~ ✓ (token monitor widget)
- ~~Settings integration with Guardian~~ ✓ (guardian.py)

### Next priorities
1. **Pinned apps on taskbar** — essential for daily use
2. **Wallpaper support** — makes it feel like a real desktop
3. **Chat as side panel, not fullscreen** — still maximizes by default
4. **Gaming awareness** — detect launchers, game mode, suppress notifications
5. **Depth gradient on Guardian cards** — click to expand to dconf paths
6. **Keyboard shortcuts** — Super for bar, Ctrl+J for chat
7. **Desktop widgets** — optional persistent panels
8. **Browser extension** — deep web browsing integration
9. **Terminal ↔ chat hybrid mode**
