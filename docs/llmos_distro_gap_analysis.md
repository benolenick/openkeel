# LLMOS vs Ubuntu Desktop — Gap Analysis

**Goal:** Identify exactly what Ubuntu's desktop provides that LLMOS needs to match or replace, so we can ship a distro.

**Strategy:** Base on Ubuntu LTS. Keep the kernel, systemd, apt, drivers, firmware. Replace GNOME Shell with LLMOS + Calcifer. Fill gaps below.

---

## Ubuntu Desktop Components → LLMOS Status

### Display & Window Management

| Ubuntu (GNOME) | LLMOS Status | Gap |
|---|---|---|
| Mutter (Wayland compositor) | NONE — web-based prototype | CRITICAL: Need a compositor. Options: Mutter, wlroots/Sway, Hyprland, or Tauri kiosk |
| GNOME Shell (desktop shell) | YES — llmos_v2.html + daemon | Shell logic exists, needs native rendering |
| Window tiling/snapping | PARTIAL — drag/resize in prototype | Need snap-to-edge, half-screen tiling |
| Multi-monitor | NONE | Need xrandr/wlr-randr integration, per-monitor taskbar |
| HiDPI scaling | PARTIAL — Guardian protects scaling | Need runtime scaling change + fractional scaling |
| Screen rotation | NONE | Low priority |

### Session & Login

| Ubuntu | LLMOS Status | Gap |
|---|---|---|
| GDM (login screen) | NONE | Need a greeter — Calcifer sleeping on the login screen, wakes up when you log in |
| gnome-session (session manager) | PARTIAL — daemon starts subsystems | Need proper XDG session registration |
| Screen lock (gnome-screensaver) | NONE | Need lock screen with Calcifer sleeping animation |
| User switching | NONE | Low priority for v1 |
| Suspend/resume hooks | NONE | Need: save state on suspend, restore on resume, Calcifer sleep→wake |

### Taskbar & System Tray

| Ubuntu | LLMOS Status | Gap |
|---|---|---|
| Top panel / dash | YES — taskbar with pins, clock, systray | Done |
| App launcher | YES — start menu + Spotlight search | Done |
| Notification center | YES — notification panel + Calcifer delivery | Done |
| WiFi indicator + picker | PARTIAL — D-Bus bridge can connect, no UI picker | Need: click WiFi icon → see networks → connect |
| Bluetooth indicator + picker | PARTIAL — same as WiFi | Need: click BT icon → see devices → pair |
| Sound indicator + mixer | PARTIAL — D-Bus bridge controls volume | Need: click sound → slider + output device picker |
| Battery indicator | NONE | Need: battery icon in taskbar, percentage, time remaining |
| Power menu (shutdown/restart/suspend) | PARTIAL — start menu has shutdown | Need: suspend, hibernate, log out |
| Night light indicator | YES — in Settings | Done |
| Keyboard layout switcher | NONE | Need: taskbar indicator showing current layout, click to switch |
| Date/time calendar popup | PARTIAL — clock shows date | Need: click clock → calendar view |

### File Management

| Ubuntu (Nautilus) | LLMOS Status | Gap |
|---|---|---|
| Browse files/folders | MOCKUP — static Files app | CRITICAL: Need real filesystem operations |
| Copy/Move/Rename/Delete | NONE in GUI | Need: right-click context menu, drag-and-drop, keyboard shortcuts |
| Trash/Recycle bin | NONE | Need: delete → trash, empty trash, restore |
| File search | YES — indexer with FTS5 | Done (backend), need GUI integration |
| Thumbnails (images, videos) | NONE | Need thumbnail generation for file grid view |
| Mount external drives | PARTIAL — D-Bus bridge detects USB | Need: auto-mount, eject button, notification |
| Network drives (SMB/NFS) | NONE | Low priority for v1 |
| Archive handling (zip/tar) | NONE in GUI | Calcifer can do it via chat, need right-click → compress/extract |
| File properties dialog | NONE | Low priority |

### Settings

| Ubuntu (gnome-control-center) | LLMOS Status | Gap |
|---|---|---|
| Display settings | YES — in Settings | Done |
| Sound settings | PARTIAL — volume control | Need: input/output device picker, per-app volume |
| Network settings | PARTIAL — WiFi connect via D-Bus | Need: full network config UI (IP, DNS, proxy) |
| Bluetooth settings | PARTIAL | Need: pair/forget devices UI |
| Keyboard settings | YES — Guardian protects layout | Done |
| Mouse/Touchpad settings | NONE | Need: speed, natural scroll, tap-to-click |
| Printer settings | NONE | Need: discover printers, add, set default |
| User accounts | NONE | Low priority |
| Date & Time | NONE in GUI | Need: timezone picker, NTP toggle |
| Region & Language | NONE | Need: locale, input method |
| Accessibility | NONE | Need: screen reader, high contrast, large text, keyboard accessibility |
| Power settings | NONE | Need: screen timeout, suspend timeout, power button behavior |
| Privacy settings | PARTIAL — context bus has toggles | Need: location services, screen sharing, app permissions |
| Default apps | PARTIAL — Guardian protects MIME | Need: picker for browser, email, music, video, photos |
| Online accounts | NONE | Low priority |
| Sharing (screen, file) | NONE | Low priority |

### Core Apps (Ubuntu ships these)

| App | LLMOS Status | Gap |
|---|---|---|
| Firefox (browser) | LAUNCHES — D-Bus bridge | Not ours to build, just include in distro |
| Nautilus (files) | MOCKUP | Need real file manager (see above) |
| GNOME Terminal | MOCKUP — static terminal | Need: real PTY, input handling, scrollback, tabs |
| Text Editor (gedit/gnome-text-editor) | NONE | Can launch system editor, or build simple one |
| Settings | YES — 7 pages in v2 | Mostly done |
| Calculator | NONE | Calcifer can do math inline — arguably better |
| Screenshot tool | NONE | Need: PrtSc → capture → annotate → save/copy |
| Image viewer (Eye of GNOME) | NONE | Can launch system viewer |
| Document viewer (Evince) | NONE | Can launch system viewer |
| Software center (GNOME Software) | PARTIAL — App Manager with residue detection | Need: browsable catalog, screenshots, reviews |
| System Monitor | YES — in prototype | Done (mockup, needs live data) |
| Disk utility | NONE | Low priority, Calcifer can diagnose via chat |
| Logs viewer | PARTIAL — Internals panel | Has log viewer, needs filtering |

### Background Services

| Ubuntu Service | LLMOS Status | Gap |
|---|---|---|
| NetworkManager | INHERIT from Ubuntu | Just use it |
| PulseAudio/PipeWire | INHERIT | Just use it |
| systemd | INHERIT | Daemon runs as systemd service |
| CUPS (printing) | INHERIT | Just include in base |
| Avahi (mDNS) | INHERIT | Just include |
| GNOME Keyring | YES — vault wraps libsecret | Done |
| PackageKit (updates) | PARTIAL — Update Guardian | Guardian checks safety, need GUI "updates available" flow |
| Firmware updates (fwupd) | NONE | Low priority, let GNOME Software handle it |
| Flatpak/Snap support | YES — apps.py supports both | Done |
| Automatic backups (Deja Dup) | NONE | Nice to have — "Calcifer, back up my stuff" |
| Unattended upgrades | REPLACED — Update Guardian is smarter | Done, better than Ubuntu's |

### What Ubuntu Has That LLMOS Replaces (Better)

| Ubuntu | LLMOS Replacement | Why it's better |
|---|---|---|
| Toast notifications that vanish | Persistent notification pipeline | Never miss an alert |
| dconf settings that reset | Config Guardian | Auto-detects and restores |
| `apt autoremove` misses stuff | App Residue Detector | Finds 17.7GB of orphans |
| No system memory | Hyphae | Remembers everything forever |
| No proactive monitoring | Tiered watchdog + crash predictor | Predicts failures before they happen |
| Manual update decisions | Update Guardian | Checks compatibility, blocks bad updates |
| Static desktop | Calcifer | A living system that shows state |
| Command line OR GUI | Workshop (Forge/Anvil/Loom/Lens) | Unified AI workspace |
| No AI integration | Native LLM + token saver + agents | AI is the foundation, not a bolt-on |

---

## Priority Tiers for Shipping

### Tier 0: Ship as `pip install llmos` (works now with polish)
- [ ] Package as pip installable
- [ ] `llmos serve` opens browser to the web UI
- [ ] Daemon auto-starts, connects to Ollama
- [ ] All existing features work in browser

### Tier 1: Ship as Tauri desktop app
- [ ] Wrap web UI in Tauri (native window, system tray Calcifer)
- [ ] Global keyboard shortcuts (Super for omnibar)
- [ ] System tray with Calcifer icon + token counter
- [ ] Native notifications (not just browser)

### Tier 2: Ship as desktop environment (installable on Ubuntu)
- [ ] Real terminal with PTY (xterm.js or native)
- [ ] Real file manager operations (copy/move/delete/trash)
- [ ] WiFi/Bluetooth/Sound taskbar pickers
- [ ] Battery indicator
- [ ] Screen lock + login greeter (Calcifer)
- [ ] Wayland compositor integration (Hyprland or Mutter)
- [ ] Window snapping/tiling
- [ ] Screenshot tool
- [ ] Multi-monitor support

### Tier 3: Ship as full distro ISO
- [ ] Ubuntu LTS base
- [ ] Custom Calamares installer with Calcifer onboarding
- [ ] Pre-installed Ollama + default model
- [ ] NVIDIA driver auto-detection + install
- [ ] First-boot: choose personality, set up accounts, install agents
- [ ] Recovery mode (TTY fallback if shell crashes)
- [ ] OEM image for hardware partners

---

---

## Fractal Decomposition — Two Depths

### Depth 0: The Three Pillars

Based on gemma4's priority analysis + practical assessment, the path to a shippable desktop is:

```
Depth 0: LLMOS Desktop App
├── [P1] Tauri Shell (native window for the web UI)
├── [P2] Real Terminal (xterm.js + PTY backend)
└── [P3] Real File Manager (backend API + frontend wiring)
```

Decision: **Skip building a Wayland compositor.** Use Tauri to wrap the existing web UI as a native desktop app. The compositor question becomes "which WM do we recommend users run alongside LLMOS" (answer: any — GNOME, KDE, Hyprland, i3) rather than "which one do we build." This saves 6+ months of work.

### Depth 1: Sub-tasks per pillar

#### P1: Tauri Shell

```
P1: Tauri Shell
├── P1.1  Scaffold Tauri project (cargo create-tauri-app)
│         - Cargo.toml with tauri, serde, tokio
│         - tauri.conf.json: window config, system tray, permissions
│         - Point devPath at existing Flask GUI server (localhost:7800)
│
├── P1.2  System tray with Calcifer
│         - Calcifer icon in system tray (idle/alert/working states)
│         - Right-click menu: "Open LLMOS", "Token Stats", "Settings", "Quit"
│         - Left-click: toggle main window
│         - Badge/tooltip showing token savings
│
├── P1.3  Global keyboard shortcuts
│         - Super → focus omnibar
│         - Super+W → open Workshop
│         - Super+T → open terminal
│         - Ctrl+Space → Spotlight search
│         - Ctrl+J → toggle chat panel
│         - Register via Tauri's global_shortcut API
│
├── P1.4  Native notifications
│         - Bridge daemon notifications to OS notifications via Tauri
│         - Calcifer speech bubbles as native toast
│
├── P1.5  Auto-start daemon
│         - Tauri app starts the LLMOS daemon on launch
│         - Health check loop: if daemon dies, restart it
│         - Connects to daemon socket for all backend calls
│
└── P1.6  Build & package
          - `cargo tauri build` → .deb for Ubuntu, .AppImage for portability
          - Include Ollama check/install in first-run
          - Desktop file (.desktop) for app launcher integration
```

**Dependencies:** Rust toolchain, Node.js (for Tauri CLI), existing Flask GUI + daemon
**Complexity:** Medium — Tauri is well-documented, most of this is config
**Estimated new code:** ~500 lines Rust, ~200 lines config

#### P2: Real Terminal

```
P2: Real Terminal
├── P2.1  Add xterm.js to the web frontend
│         - npm install xterm xterm-addon-fit xterm-addon-webgl
│         - Or CDN include in the HTML prototype
│         - Initialize terminal in the Terminal window's .win-body
│
├── P2.2  WebSocket PTY backend
│         - New endpoint in gui/server.py: /ws/terminal
│         - On connect: fork a PTY (pty.openpty() or pty.fork())
│         - Spawn bash (or user's $SHELL) attached to the PTY
│         - Bridge: PTY stdout → WebSocket → xterm.js
│         - Bridge: xterm.js keystrokes → WebSocket → PTY stdin
│
├── P2.3  Terminal features
│         - Resize handling (xterm.js fit addon → SIGWINCH to PTY)
│         - Multiple terminal tabs/sessions
│         - Copy/paste (Ctrl+Shift+C/V)
│         - Scrollback buffer (xterm.js handles this)
│         - Color theme matching LLMOS dark theme
│
├── P2.4  Calcifer terminal integration
│         - Natural language input detection: if user types English sentence
│           instead of a command, offer to translate to bash
│         - "Did you mean: `find /home -name '*.pdf'`?" inline suggestion
│         - Terminal context feeds into context_bus (working directory, last command)
│
└── P2.5  Shell history to Hyphae
          - Log commands + outcomes to Hyphae
          - "What command did I use to fix DNS last week?" → Calcifer recalls
```

**Dependencies:** xterm.js (JS library), Python pty module (stdlib)
**Complexity:** Medium — xterm.js + PTY bridge is well-trodden ground
**Estimated new code:** ~150 lines JS (xterm init), ~200 lines Python (PTY WebSocket handler)

#### P3: Real File Manager

```
P3: Real File Manager
├── P3.1  File operations backend API
│         - New endpoints in gui/server.py or new file_api.py:
│           GET  /api/files/list?path=...     → directory listing with metadata
│           POST /api/files/copy              → copy file/dir
│           POST /api/files/move              → move/rename
│           POST /api/files/delete            → move to trash (not rm)
│           POST /api/files/mkdir             → create directory
│           POST /api/files/trash             → list trash contents
│           POST /api/files/restore           → restore from trash
│           GET  /api/files/thumbnail?path=.. → thumbnail for images
│         - All operations use pathlib, shutil, send2trash
│         - Permission checks: don't allow operations outside $HOME without elevated trust
│
├── P3.2  Wire Files app to real backend
│         - Replace static file grid with dynamic listing from /api/files/list
│         - Click folder → navigate, update breadcrumb
│         - File icons based on extension
│         - Sort by name/size/date
│         - Grid view and list view toggle
│
├── P3.3  Right-click context menu
│         - Right-click file → Copy, Move, Rename, Delete, Compress, Open with...
│         - Right-click empty space → New folder, New file, Paste, Open terminal here
│         - Actions call the backend API
│
├── P3.4  Drag and drop
│         - Drag files between folders
│         - Drag onto omnibar → "Upload this" or "Compress this"
│         - Drag onto Calcifer → "What is this file?"
│
├── P3.5  Trash integration
│         - Delete → moves to ~/.local/share/Trash (XDG spec)
│         - Trash icon in sidebar with count
│         - Empty trash action
│
└── P3.6  Calcifer file intelligence
          - "Find that PDF I downloaded yesterday" → indexer search
          - "This folder is 4.2GB, 3.8GB is cache" → residue detector integration
          - Right-click → "Ask Calcifer about this file" → opens chat with file context
```

**Dependencies:** send2trash (pip), Pillow (thumbnails), existing indexer
**Complexity:** Medium — individual operations are simple, volume of endpoints is the work
**Estimated new code:** ~400 lines Python (file API), ~300 lines JS (frontend wiring)

### Depth 1 summary: secondary features (build after P1-P3)

```
Secondary (Tier 2 but lower priority):
├── WiFi/BT/Sound taskbar pickers → GUI shells over existing D-Bus bridge
├── Battery indicator → read /sys/class/power_supply, show in taskbar
├── Screen lock → simple overlay + PAM auth, Calcifer sleeping animation
├── Login greeter → separate lightweight app, PAM integration
├── Window tiling → Super+Left/Right = half screen (JS in prototype)
├── Screenshot → scrot/grim + annotation UI
├── Calendar popup → click clock → month view + events
├── Mouse/touchpad → libinput settings exposed in Settings panel
├── Power settings → systemd sleep targets + lid switch config
```

None of these block shipping. They're polish that can be added incrementally after the three pillars are solid.

---

## Recommended Build Order

1. **P2 (Terminal)** — smallest, highest impact, unblocks Anvil workbench
2. **P3 (File Manager)** — second highest daily-use impact
3. **P1 (Tauri Shell)** — makes it feel like a real app instead of a browser tab
4. **WiFi/BT/Sound pickers** — quick wins, existing backend
5. **Screen lock + greeter** — needed for multi-user
6. **Everything else** — iterative polish

Total estimated new code for P1+P2+P3: ~1,750 lines (500 Rust + 750 Python + 500 JS)

---

## What We DON'T Build (Use Ubuntu's)

- Kernel
- systemd
- NetworkManager
- PipeWire/PulseAudio
- CUPS (printing)
- Firmware updates (fwupd)
- Drivers (ubuntu-drivers)
- Package management (apt + snap + flatpak)
- Display drivers (mesa, nvidia)
- Fonts, themes, icons (inherit or ship custom set)
- LibreOffice, Firefox, Thunderbird (include as default apps)
