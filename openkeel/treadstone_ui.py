#!/usr/bin/env python3
"""OpenKeel Treadstone — Visual Attack Tree Manager.

A radial graph UI for managing the kill chain. Start from the center target,
discover services, add nodes, track phases. Human-driven.

Usage:
    python3 -m openkeel.treadstone_ui
    python3 -m openkeel.treadstone_ui --mission interpreter-htb
"""

import json
import math
import os
import re
import subprocess
import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from pathlib import Path
import uuid
import time
import threading
import urllib.request
import urllib.error

PHASES = ["recon", "research", "run", "review", "done", "failed"]
NODE_TYPES = ["target", "service", "machine", "credential", "finding", "pivot"]

PHASE_COLORS = {
    "recon": "#4A90D9", "research": "#F5A623", "run": "#D0021B",
    "review": "#7B68EE", "done": "#2ECC40", "failed": "#AAAAAA",
}

TYPE_SHAPES = {
    "target": "circle_large", "service": "circle", "machine": "diamond",
    "credential": "square", "finding": "triangle", "pivot": "hexagon",
}

LOG_COLORS = {
    "DISCOVERY": "#2ECC40", "ATTEMPT": "#F5A623",
    "CIRCUIT": "#D0021B", "OBSERVER": "#9B59B6",
}

OPENKEEL_DIR = Path.home() / ".openkeel"

OBSERVER_PORTS = {
    "Cartographer": 11444, "Pilgrim": 11445, "Oracle": 11444,
}

SMART_ROUTER_URL = "http://127.0.0.1:8004/query"
HYPHAE_RECALL_URL = "http://127.0.0.1:8100/recall"
HYPHAE_REMEMBER_URL = "http://127.0.0.1:8100/remember"
ORACLE_URL = "http://127.0.0.1:11444/api/generate"
ORACLE_MODEL = "qwen3.5:latest"

# Well-known service → research queries
SERVICE_RESEARCH = {
    "ssh": ["SSH version exploit", "SSH brute force default credentials", "SSH key enumeration"],
    "http": ["HTTP web application enumeration", "directory bruteforce gobuster", "web application vulnerabilities"],
    "https": ["HTTPS TLS certificate enumeration", "web application vulnerabilities"],
    "ftp": ["FTP anonymous login", "FTP version exploit", "FTP file enumeration"],
    "smb": ["SMB enumeration smbclient", "SMB version exploit EternalBlue", "SMB null session"],
    "mysql": ["MySQL default credentials", "MySQL UDF exploit", "MySQL enumeration"],
    "mssql": ["MSSQL xp_cmdshell", "MSSQL default credentials sa", "MSSQL enumeration"],
    "rdp": ["RDP BlueKeep exploit", "RDP brute force", "RDP NLA bypass"],
    "dns": ["DNS zone transfer", "DNS subdomain enumeration", "DNS cache poisoning"],
    "ldap": ["LDAP enumeration", "LDAP anonymous bind", "AD enumeration via LDAP"],
    "smtp": ["SMTP user enumeration VRFY", "SMTP relay", "SMTP version exploit"],
    "snmp": ["SNMP community string bruteforce", "SNMP enumeration snmpwalk"],
    "nfs": ["NFS share enumeration showmount", "NFS mount and read"],
    "redis": ["Redis unauthenticated access", "Redis RCE module load"],
    "postgresql": ["PostgreSQL default credentials", "PostgreSQL command execution"],
    "winrm": ["WinRM credential spray", "Evil-WinRM shell"],
}


def new_id():
    return uuid.uuid4().hex[:8]


def http_post(url, data, timeout=30):
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get(url, timeout=5):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


class Node:
    __slots__ = ("id", "label", "node_type", "phase", "x", "y", "notes", "hypotheses", "parent_id")

    def __init__(self, id=None, label="", node_type="service", phase="recon",
                 x=0, y=0, notes="", hypotheses=None, parent_id=None):
        self.id = id or new_id()
        self.label = label
        self.node_type = node_type
        self.phase = phase
        self.x = x
        self.y = y
        self.notes = notes
        self.hypotheses = hypotheses or []
        self.parent_id = parent_id

    def to_dict(self):
        return {
            "id": self.id, "label": self.label, "node_type": self.node_type,
            "phase": self.phase, "x": self.x, "y": self.y, "notes": self.notes,
            "hypotheses": self.hypotheses, "parent_id": self.parent_id,
        }

    @classmethod
    def from_dict(cls, d):
        keys = ("id", "label", "node_type", "phase", "x", "y", "notes", "hypotheses", "parent_id")
        return cls(**{k: d[k] for k in keys if k in d})


class Edge:
    __slots__ = ("src", "dst", "label")

    def __init__(self, src, dst, label=""):
        self.src = src
        self.dst = dst
        self.label = label

    def to_dict(self):
        return {"src": self.src, "dst": self.dst, "label": self.label}


class AttackGraph:
    def __init__(self, mission=""):
        self.mission = mission
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []

    def add_node(self, node: Node):
        self.nodes[node.id] = node
        if node.parent_id and node.parent_id in self.nodes:
            self.edges.append(Edge(node.parent_id, node.id))

    def remove_node(self, node_id):
        if node_id in self.nodes:
            del self.nodes[node_id]
        self.edges = [e for e in self.edges if e.src != node_id and e.dst != node_id]
        orphans = [n.id for n in self.nodes.values() if n.parent_id == node_id]
        for oid in orphans:
            self.remove_node(oid)

    def children(self, node_id):
        child_ids = {e.dst for e in self.edges if e.src == node_id}
        return [self.nodes[cid] for cid in child_ids if cid in self.nodes]

    def save(self, path):
        data = {
            "mission": self.mission,
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        g = cls(mission=data.get("mission", ""))
        for nd in data.get("nodes", []):
            g.nodes[nd["id"]] = Node.from_dict(nd)
        for ed in data.get("edges", []):
            g.edges.append(Edge(ed["src"], ed["dst"], ed.get("label", "")))
        return g


class TreadstoneUI(tk.Tk):
    def __init__(self, mission=""):
        super().__init__()
        self.title(f"Treadstone — {mission or 'Attack Tree'}")
        self.geometry("1400x900")
        self.configure(bg="#1a1a2e")

        self.mission = mission
        self.graph = AttackGraph(mission)
        self.selected_id = None
        self.drag_data = None

        # Pan & zoom state
        self.zoom_level = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._pan_drag = None
        self._right_click_start = None

        # Command bar history
        self.cmd_history = []
        self.cmd_history_idx = -1

        # Toast stack
        self._toasts = []

        # Observer health
        self.observer_status = {name: {"up": False, "ms": 0} for name in OBSERVER_PORTS}

        # Log lines read so far
        self._log_offset = 0
        self._nudge_offset = 0
        self.log_visible = True

        # File path
        self.mission_dir = OPENKEEL_DIR / "goals" / (mission or "default")
        self.save_path = self.mission_dir / "attack_graph.json"
        if self.save_path.exists():
            try:
                self.graph = AttackGraph.load(self.save_path)
            except Exception:
                pass

        if not self.graph.nodes:
            root = Node(label=mission or "TARGET", node_type="target", phase="recon", x=0, y=0)
            self.graph.add_node(root)

        self._build_ui()
        self._draw()

        self._auto_save()
        self._health_check()
        self._tail_logs()
        self._update_suggestion()
        self._suggestion_loop()

        # Keybindings
        self.bind("<Delete>", lambda e: self._delete_selected())
        self.bind("<Control-s>", lambda e: self._save())
        self.bind("<Control-n>", lambda e: self._cmd_add_child())
        self.bind("<Home>", lambda e: self._fit_view())
        self.bind("<Control-l>", lambda e: self._toggle_log())
        self.bind("<Control-k>", lambda e: self._focus_cmd())
        self.bind("<Key>", self._on_key)

    def _build_ui(self):
        # Top toolbar with target IP and action buttons
        toolbar = tk.Frame(self, bg="#0a0a1e")
        toolbar.pack(fill=tk.X, side=tk.TOP)

        # Target IP
        tk.Label(toolbar, text="TARGET:", bg="#0a0a1e", fg="#e94560",
                 font=("Consolas", 11, "bold")).pack(side=tk.LEFT, padx=(10, 5), pady=6)
        self._target_var = tk.StringVar(value=self._guess_target_ip())
        target_entry = tk.Entry(toolbar, textvariable=self._target_var, bg="#1a1a2e", fg="white",
                                insertbackground="white", font=("Consolas", 12), width=18, relief=tk.FLAT)
        target_entry.pack(side=tk.LEFT, padx=2, pady=4)

        # Separator
        tk.Frame(toolbar, width=2, bg="#3a3a5c").pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=4)

        # Action buttons
        btn_cfg = {"font": ("Consolas", 10, "bold"), "relief": "flat", "cursor": "hand2",
                   "fg": "white", "padx": 12, "pady": 4}

        self._scan_btn = tk.Button(toolbar, text="SCAN", bg="#D0021B", command=self._btn_scan, **btn_cfg)
        self._scan_btn.pack(side=tk.LEFT, padx=2, pady=4)

        tk.Button(toolbar, text="QUICK SCAN", bg="#c0392b", command=self._btn_quickscan, **btn_cfg).pack(side=tk.LEFT, padx=2, pady=4)

        tk.Button(toolbar, text="WEB SCAN", bg="#F5A623", command=self._btn_webscan, **btn_cfg).pack(side=tk.LEFT, padx=2, pady=4)

        tk.Frame(toolbar, width=2, bg="#3a3a5c").pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=4)

        tk.Button(toolbar, text="RESEARCH", bg="#4A90D9", command=self._btn_research_all, **btn_cfg).pack(side=tk.LEFT, padx=2, pady=4)

        tk.Button(toolbar, text="ASK ORACLE", bg="#7B68EE", command=self._btn_oracle, **btn_cfg).pack(side=tk.LEFT, padx=2, pady=4)

        tk.Button(toolbar, text="PASTE OUTPUT", bg="#2ECC40", command=self._cmd_paste, **btn_cfg).pack(side=tk.LEFT, padx=2, pady=4)

        tk.Frame(toolbar, width=2, bg="#3a3a5c").pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=4)

        tk.Button(toolbar, text="AUTO PILOT", bg="#e94560", command=self._btn_autopilot, **btn_cfg).pack(side=tk.LEFT, padx=2, pady=4)

        # Top-level container
        self._main = tk.Frame(self, bg="#1a1a2e")
        self._main.pack(fill=tk.BOTH, expand=True)

        # Horizontal split: canvas+log vs detail panel
        self._hpane = tk.PanedWindow(self._main, orient=tk.HORIZONTAL, bg="#1a1a2e",
                                     sashwidth=4, sashrelief=tk.FLAT)
        self._hpane.pack(fill=tk.BOTH, expand=True)

        # Left side: canvas + log (vertical split)
        self._left = tk.PanedWindow(self._hpane, orient=tk.VERTICAL, bg="#1a1a2e",
                                    sashwidth=4, sashrelief=tk.FLAT)
        self._hpane.add(self._left, stretch="always")

        # Canvas
        canvas_frame = tk.Frame(self._left, bg="#16213e")
        self.canvas = tk.Canvas(canvas_frame, bg="#16213e", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._left.add(canvas_frame, stretch="always")

        # Log panel
        self._log_frame = tk.Frame(self._left, bg="#0d0d1a", height=180)
        self._log_text = tk.Text(self._log_frame, bg="#0d0d1a", fg="#e0e0e0",
                                 font=("Consolas", 9), wrap=tk.WORD, state=tk.DISABLED,
                                 height=10, highlightthickness=0, borderwidth=0)
        log_scroll = tk.Scrollbar(self._log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(fill=tk.BOTH, expand=True)
        self._left.add(self._log_frame, stretch="never")

        # Configure log tag colors
        for cat, color in LOG_COLORS.items():
            self._log_text.tag_configure(cat, foreground=color)
        self._log_text.tag_configure("RESULT", foreground="#00BFFF")
        self._log_text.tag_configure("ERROR", foreground="#FF4444")
        self._log_text.tag_configure("INFO", foreground="#888888")

        # Right: detail panel
        self.panel = tk.Frame(self._hpane, bg="#0f3460", width=320)
        self._hpane.add(self.panel, stretch="never")

        # Bottom bar: suggestion + observer status + command bar
        self._bottom = tk.Frame(self, bg="#111122")
        self._bottom.pack(fill=tk.X, side=tk.BOTTOM)

        # Suggestion bar (top of bottom area)
        self._suggestion_var = tk.StringVar(value=">> Run: scan <target-ip> to begin")
        self._suggestion_label = tk.Label(self._bottom, textvariable=self._suggestion_var,
                                          bg="#1a0a2e", fg="#F5A623", font=("Consolas", 11, "bold"),
                                          anchor="w", padx=10, pady=4)
        self._suggestion_label.pack(fill=tk.X)

        # Status line (feedback)
        self._status_var = tk.StringVar(value="Ready")
        self._status_label = tk.Label(self._bottom, textvariable=self._status_var,
                                      bg="#111122", fg="#888888", font=("Consolas", 9),
                                      anchor="w")
        self._status_label.pack(fill=tk.X, padx=5)

        # Command + observer row
        cmd_row = tk.Frame(self._bottom, bg="#111122")
        cmd_row.pack(fill=tk.X)

        # Observer LEDs
        self._led_frame = tk.Frame(cmd_row, bg="#111122")
        self._led_frame.pack(side=tk.LEFT, padx=5, pady=2)
        self._leds = {}
        for name in OBSERVER_PORTS:
            f = tk.Frame(self._led_frame, bg="#111122")
            f.pack(side=tk.LEFT, padx=4)
            led = tk.Canvas(f, width=10, height=10, bg="#111122", highlightthickness=0)
            led.create_oval(1, 1, 9, 9, fill="#880000", outline="", tags="dot")
            led.pack(side=tk.LEFT)
            lbl = tk.Label(f, text=name[:4], bg="#111122", fg="#666666", font=("Consolas", 8))
            lbl.pack(side=tk.LEFT, padx=(2, 0))
            led.bind("<Button-1>", lambda e, n=name: self._show_observer_info(n))
            self._leds[name] = led

        # Zoom indicator
        self._zoom_label = tk.Label(cmd_row, text="100%", bg="#111122", fg="#666666",
                                    font=("Consolas", 8))
        self._zoom_label.pack(side=tk.LEFT, padx=8)

        # Command entry
        tk.Label(cmd_row, text="/", bg="#111122", fg="#e94560",
                 font=("Consolas", 11, "bold")).pack(side=tk.LEFT)
        self._cmd_var = tk.StringVar()
        self._cmd_entry = tk.Entry(cmd_row, textvariable=self._cmd_var, bg="#1a1a2e",
                                   fg="#e0e0e0", insertbackground="white",
                                   font=("Consolas", 11), relief=tk.FLAT)
        self._cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5), pady=2)
        self._cmd_entry.bind("<Return>", self._exec_cmd)
        self._cmd_entry.bind("<Escape>", lambda e: self.canvas.focus_set())
        self._cmd_entry.bind("<Up>", self._cmd_hist_prev)
        self._cmd_entry.bind("<Down>", self._cmd_hist_next)

        # Canvas events
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<MouseWheel>", self._on_scroll)
        self.canvas.bind("<Button-2>", self._start_pan)
        self.canvas.bind("<B2-Motion>", self._do_pan)
        self.canvas.bind("<ButtonRelease-2>", self._end_pan)
        # Shift+left for pan alternative
        self.canvas.bind("<Shift-Button-1>", self._start_pan)
        self.canvas.bind("<Shift-B1-Motion>", self._do_pan)
        self.canvas.bind("<Shift-ButtonRelease-1>", self._end_pan)
        # Right-click drag to pan (context menu only on node right-click release)
        self.canvas.bind("<Button-3>", self._on_right_press)
        self.canvas.bind("<B3-Motion>", self._do_pan)
        self.canvas.bind("<ButtonRelease-3>", self._on_right_release)

        self._build_panel()

    # ---- Panel ----

    def _build_panel(self):
        p = self.panel
        for w in p.winfo_children():
            w.destroy()

        style = {"bg": "#0f3460", "fg": "#e0e0e0", "font": ("Consolas", 11)}
        hstyle = {"bg": "#0f3460", "fg": "#e94560", "font": ("Consolas", 14, "bold")}

        tk.Label(p, text="NODE DETAILS", **hstyle).pack(pady=(15, 5), padx=10, anchor="w")

        if not self.selected_id or self.selected_id not in self.graph.nodes:
            tk.Label(p, text="Click a node to select it.", **style).pack(pady=20, padx=10)
            tk.Label(p, text="Double-click to add child.", **style).pack(padx=10)
            tk.Label(p, text="Right-click for actions.", **style).pack(padx=10)
            tk.Label(p, text="/ or Ctrl+K for command bar.", **style).pack(padx=10)

            tk.Label(p, text="\nLEGEND", **hstyle).pack(pady=(20, 5), padx=10, anchor="w")
            for phase, color in PHASE_COLORS.items():
                f = tk.Frame(p, bg="#0f3460")
                f.pack(fill=tk.X, padx=15, pady=1)
                tk.Canvas(f, width=12, height=12, bg=color, highlightthickness=0).pack(side=tk.LEFT, padx=(0, 8))
                tk.Label(f, text=phase.title(), **style).pack(side=tk.LEFT)

            tk.Label(p, text="\nKEYS", **hstyle).pack(pady=(15, 5), padx=10, anchor="w")
            keys = ["1-6  Set phase", "Tab  Cycle children", "Del  Delete node",
                    "Home Fit view", "C-S  Save", "C-L  Toggle log", "C-N  Add child"]
            for k in keys:
                tk.Label(p, text=k, bg="#0f3460", fg="#999999", font=("Consolas", 9),
                         anchor="w").pack(fill=tk.X, padx=15)
            return

        node = self.graph.nodes[self.selected_id]

        # Label
        tk.Label(p, text="Label:", **style).pack(anchor="w", padx=10, pady=(10, 0))
        self._name_var = tk.StringVar(value=node.label)
        name_entry = tk.Entry(p, textvariable=self._name_var, font=("Consolas", 12),
                              bg="#1a1a2e", fg="white", insertbackground="white")
        name_entry.pack(fill=tk.X, padx=10, pady=2)
        self._name_var.trace_add("write", lambda *a: self._update_field("label", self._name_var.get()))

        # Type
        tk.Label(p, text="Type:", **style).pack(anchor="w", padx=10, pady=(8, 0))
        self._type_var = tk.StringVar(value=node.node_type)
        type_combo = ttk.Combobox(p, textvariable=self._type_var, values=NODE_TYPES,
                                  font=("Consolas", 11), state="readonly")
        type_combo.pack(fill=tk.X, padx=10, pady=2)
        self._type_var.trace_add("write", lambda *a: self._update_field("node_type", self._type_var.get()))

        # Phase buttons
        tk.Label(p, text="Phase:", **style).pack(anchor="w", padx=10, pady=(8, 0))
        phase_frame = tk.Frame(p, bg="#0f3460")
        phase_frame.pack(fill=tk.X, padx=10, pady=2)
        for phase in PHASES:
            color = PHASE_COLORS[phase]
            is_current = node.phase == phase
            btn = tk.Button(
                phase_frame, text=phase[:3].upper(),
                bg=color if is_current else "#1a1a2e",
                fg="white", font=("Consolas", 9, "bold"),
                relief="sunken" if is_current else "flat",
                width=4, cursor="hand2",
                command=lambda ph=phase: self._set_phase(ph),
            )
            btn.pack(side=tk.LEFT, padx=1)

        # Notes
        tk.Label(p, text="Notes:", **style).pack(anchor="w", padx=10, pady=(10, 0))
        self._notes_text = tk.Text(p, height=6, font=("Consolas", 10),
                                   bg="#1a1a2e", fg="#e0e0e0", insertbackground="white",
                                   wrap=tk.WORD)
        self._notes_text.pack(fill=tk.X, padx=10, pady=2)
        self._notes_text.insert("1.0", node.notes)
        self._notes_text.bind("<KeyRelease>", self._on_notes_change)

        # Hypotheses
        tk.Label(p, text=f"Hypotheses ({len(node.hypotheses)}):", **style).pack(anchor="w", padx=10, pady=(10, 0))
        hyp_frame = tk.Frame(p, bg="#0f3460")
        hyp_frame.pack(fill=tk.X, padx=10, pady=2)

        for i, hyp in enumerate(node.hypotheses):
            hf = tk.Frame(hyp_frame, bg="#1a1a2e")
            hf.pack(fill=tk.X, pady=1)
            hyp_text = hyp if isinstance(hyp, str) else hyp.get("text", str(hyp))
            attempts = 0
            confidence = None
            if isinstance(hyp, dict):
                attempts = hyp.get("attempts", 0)
                confidence = hyp.get("confidence")
            suffix = f" [A:{attempts}]" if attempts else ""
            if confidence is not None:
                suffix += f" C:{confidence}"
            fg_color = "#FF4444" if attempts >= 3 else "#e0e0e0"
            prefix = "!! " if attempts >= 3 else "  "
            tk.Label(hf, text=f"{prefix}{hyp_text}{suffix}", bg="#1a1a2e", fg=fg_color,
                     font=("Consolas", 9), anchor="w", wraplength=220).pack(side=tk.LEFT, fill=tk.X, expand=True)
            # Search button — look up this hypothesis
            tk.Button(hf, text="?", bg="#4A90D9", fg="white", font=("Consolas", 8),
                      relief="flat", width=2,
                      command=lambda h=hyp_text: self._async_search(h)).pack(side=tk.RIGHT, padx=1)
            tk.Button(hf, text="x", bg="#D0021B", fg="white", font=("Consolas", 8),
                      relief="flat", width=2,
                      command=lambda idx=i: self._remove_hyp(idx)).pack(side=tk.RIGHT)

        if any(isinstance(h, dict) and h.get("attempts", 0) >= 3 for h in node.hypotheses):
            tk.Label(hyp_frame, text="CIRCUIT BREAKER: >=3 attempts", bg="#0f3460",
                     fg="#FF4444", font=("Consolas", 9, "bold")).pack(anchor="w")

        tk.Button(hyp_frame, text="+ Add Hypothesis", bg="#4A90D9", fg="white",
                  font=("Consolas", 9), relief="flat", cursor="hand2",
                  command=self._add_hyp).pack(fill=tk.X, pady=(3, 0))

        # Actions
        btn_style = {"fg": "white", "font": ("Consolas", 10, "bold"),
                     "relief": "flat", "cursor": "hand2"}
        btn_row = tk.Frame(p, bg="#0f3460")
        btn_row.pack(fill=tk.X, padx=10, pady=(10, 2))
        tk.Button(btn_row, text="+ Child", bg="#2ECC40", command=self._cmd_add_child,
                  **btn_style).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        tk.Button(btn_row, text="Delete", bg="#D0021B", command=self._delete_selected,
                  **btn_style).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        tk.Button(btn_row, text="Save", bg="#e94560", command=self._save,
                  **btn_style).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)

    # ---- World/Screen transforms ----

    def _world_to_screen(self, wx, wy):
        cx = self.canvas.winfo_width() / 2
        cy = self.canvas.winfo_height() / 2
        return cx + (wx + self.pan_x) * self.zoom_level, cy + (wy + self.pan_y) * self.zoom_level

    def _screen_to_world(self, sx, sy):
        cx = self.canvas.winfo_width() / 2
        cy = self.canvas.winfo_height() / 2
        return (sx - cx) / self.zoom_level - self.pan_x, (sy - cy) / self.zoom_level - self.pan_y

    # ---- Drawing ----

    def _draw(self):
        self.canvas.delete("all")
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 10:
            return

        # Edges (bezier curves)
        for edge in self.graph.edges:
            if edge.src in self.graph.nodes and edge.dst in self.graph.nodes:
                n1 = self.graph.nodes[edge.src]
                n2 = self.graph.nodes[edge.dst]
                x1, y1 = self._world_to_screen(n1.x, n1.y)
                x2, y2 = self._world_to_screen(n2.x, n2.y)
                mx = (x1 + x2) / 2
                my = (y1 + y2) / 2
                # Perpendicular offset for curve
                dx, dy = x2 - x1, y2 - y1
                dist = math.sqrt(dx * dx + dy * dy) or 1
                off = min(dist * 0.15, 30)
                cx_b = mx + (dy / dist) * off
                cy_b = my - (dx / dist) * off
                self.canvas.create_line(
                    x1, y1, cx_b, cy_b, x2, y2,
                    smooth=True, fill="#3a3a5c", width=2, stipple="gray50",
                )

        # Nodes
        for node in self.graph.nodes.values():
            self._draw_node(node)

        # Minimap
        self._draw_minimap()

    def _draw_node(self, node: Node):
        sx, sy = self._world_to_screen(node.x, node.y)
        color = PHASE_COLORS.get(node.phase, "#888")
        is_selected = node.id == self.selected_id
        base_r = 30 if node.node_type == "target" else 20
        r = base_r * self.zoom_level

        # Glow for selected
        if is_selected:
            gr = r * 1.5
            self.canvas.create_oval(sx - gr, sy - gr, sx + gr, sy + gr,
                                    fill="", outline="#e94560", width=2, stipple="gray25")

        outline = "#e94560" if is_selected else "#3a3a5c"
        outline_w = 3 if is_selected else 1

        if node.node_type in ("target", "service", "finding"):
            self.canvas.create_oval(sx - r, sy - r, sx + r, sy + r,
                                    fill=color, outline=outline, width=outline_w,
                                    tags=("node", node.id))
        elif node.node_type == "machine":
            pts = [sx, sy - r, sx + r, sy, sx, sy + r, sx - r, sy]
            self.canvas.create_polygon(pts, fill=color, outline=outline, width=outline_w,
                                       tags=("node", node.id))
        elif node.node_type == "credential":
            self.canvas.create_rectangle(sx - r * 0.8, sy - r * 0.8, sx + r * 0.8, sy + r * 0.8,
                                         fill=color, outline=outline, width=outline_w,
                                         tags=("node", node.id))
        elif node.node_type == "pivot":
            # Hexagon
            pts = []
            for i in range(6):
                a = math.pi / 3 * i - math.pi / 6
                pts.extend([sx + r * math.cos(a), sy + r * math.sin(a)])
            self.canvas.create_polygon(pts, fill=color, outline=outline, width=outline_w,
                                       tags=("node", node.id))
        else:
            self.canvas.create_oval(sx - r, sy - r, sx + r, sy + r,
                                    fill=color, outline=outline, width=outline_w,
                                    tags=("node", node.id))

        # Phase text (bold)
        fs = max(7, int(8 * self.zoom_level))
        self.canvas.create_text(sx, sy, text=node.phase[:3].upper(), fill="white",
                                font=("Consolas", fs, "bold"), tags=("phase", node.id))

        # Label below
        fs2 = max(7, int(9 * self.zoom_level))
        self.canvas.create_text(sx, sy + r + 10 * self.zoom_level,
                                text=node.label[:20], fill="#e0e0e0",
                                font=("Consolas", fs2), tags=("label", node.id))

        # Hypothesis count badge
        nhyp = len(node.hypotheses)
        if nhyp > 0:
            bx, by = sx + r * 0.7, sy - r * 0.7
            br = max(6, 8 * self.zoom_level)
            self.canvas.create_oval(bx - br, by - br, bx + br, by + br,
                                    fill="#e94560", outline="")
            self.canvas.create_text(bx, by, text=f"H:{nhyp}", fill="white",
                                    font=("Consolas", max(6, int(7 * self.zoom_level))))

    def _draw_minimap(self):
        if not self.graph.nodes:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        mm_w, mm_h = 120, 90
        mm_x, mm_y = cw - mm_w - 10, ch - mm_h - 10

        self.canvas.create_rectangle(mm_x, mm_y, mm_x + mm_w, mm_y + mm_h,
                                     fill="#0d0d1a", outline="#3a3a5c", stipple="gray50")

        xs = [n.x for n in self.graph.nodes.values()]
        ys = [n.y for n in self.graph.nodes.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 1)
        span_y = max(max_y - min_y, 1)
        scale = min((mm_w - 20) / span_x, (mm_h - 20) / span_y, 1.0)

        for node in self.graph.nodes.values():
            dx = (node.x - min_x) * scale + 10
            dy = (node.y - min_y) * scale + 10
            c = PHASE_COLORS.get(node.phase, "#888")
            dot_r = 3 if node.id == self.selected_id else 2
            self.canvas.create_oval(mm_x + dx - dot_r, mm_y + dy - dot_r,
                                    mm_x + dx + dot_r, mm_y + dy + dot_r,
                                    fill=c, outline="", tags="minimap")

        # Viewport rect
        s_tl_x, s_tl_y = self._screen_to_world(0, 0)
        s_br_x, s_br_y = self._screen_to_world(cw, ch)
        vx1 = (s_tl_x - min_x) * scale + 10
        vy1 = (s_tl_y - min_y) * scale + 10
        vx2 = (s_br_x - min_x) * scale + 10
        vy2 = (s_br_y - min_y) * scale + 10
        vx1 = max(0, min(mm_w, vx1))
        vy1 = max(0, min(mm_h, vy1))
        vx2 = max(0, min(mm_w, vx2))
        vy2 = max(0, min(mm_h, vy2))
        self.canvas.create_rectangle(mm_x + vx1, mm_y + vy1, mm_x + vx2, mm_y + vy2,
                                     outline="#e94560", width=1, tags="minimap")

        # Bind minimap click
        self.canvas.tag_bind("minimap", "<Button-1>", lambda e: self._minimap_click(e, mm_x, mm_y, mm_w, mm_h, min_x, min_y, scale))

    def _minimap_click(self, event, mm_x, mm_y, mm_w, mm_h, min_x, min_y, scale):
        if scale <= 0:
            return
        rx = event.x - mm_x - 10
        ry = event.y - mm_y - 10
        wx = rx / scale + min_x
        wy = ry / scale + min_y
        self.pan_x = -wx
        self.pan_y = -wy
        self._draw()

    # ---- Events ----

    def _hit_node(self, sx, sy):
        for node in self.graph.nodes.values():
            nx, ny = self._world_to_screen(node.x, node.y)
            r = (30 if node.node_type == "target" else 20) * self.zoom_level
            if (sx - nx) ** 2 + (sy - ny) ** 2 <= r ** 2:
                return node
        return None

    def _on_click(self, event):
        node = self._hit_node(event.x, event.y)
        if node:
            self.selected_id = node.id
            self.drag_data = {"id": node.id, "ox": event.x, "oy": event.y,
                              "start_x": node.x, "start_y": node.y, "moved": False}
        else:
            self.selected_id = None
            self.drag_data = None
        self._build_panel()
        self._draw()

    def _on_drag(self, event):
        if not self.drag_data:
            return
        node = self.graph.nodes.get(self.drag_data["id"])
        if not node:
            return
        dx = (event.x - self.drag_data["ox"]) / self.zoom_level
        dy = (event.y - self.drag_data["oy"]) / self.zoom_level
        if abs(dx) > 3 or abs(dy) > 3:
            self.drag_data["moved"] = True
        node.x = self.drag_data["start_x"] + dx
        node.y = self.drag_data["start_y"] + dy
        self._draw()

    def _on_release(self, event):
        self.drag_data = None

    def _on_double_click(self, event):
        node = self._hit_node(event.x, event.y)
        if node:
            self.selected_id = node.id
            self._cmd_add_child()
        else:
            wx, wy = self._screen_to_world(event.x, event.y)
            label = simpledialog.askstring("New Node", "Label:", parent=self)
            if label:
                n = Node(label=label, node_type="service", x=wx, y=wy)
                self.graph.add_node(n)
                self.selected_id = n.id
                self._build_panel()
                self._draw()

    def _on_right_press(self, event):
        """Right-click press: start pan, remember position for context menu."""
        self._right_click_start = (event.x, event.y)
        self._start_pan(event)

    def _on_right_release(self, event):
        """Right-click release: show context menu if didn't drag."""
        self._end_pan(event)
        if self._right_click_start:
            sx, sy = self._right_click_start
            if abs(event.x - sx) < 5 and abs(event.y - sy) < 5:
                self._show_context_menu(event)
        self._right_click_start = None

    def _show_context_menu(self, event):
        node = self._hit_node(event.x, event.y)
        if not node:
            return
        self.selected_id = node.id
        self._build_panel()
        self._draw()

        menu = tk.Menu(self, tearoff=0, bg="#1a1a2e", fg="white",
                       activebackground="#e94560", font=("Consolas", 10))
        for phase in PHASES:
            menu.add_command(label=f"Phase: {phase.title()}",
                             command=lambda ph=phase: self._set_phase(ph))
        menu.add_separator()
        for ntype in NODE_TYPES:
            menu.add_command(label=f"Type: {ntype.title()}",
                             command=lambda nt=ntype: self._set_type(nt))
        menu.add_separator()
        menu.add_command(label="+ Add Child", command=self._cmd_add_child)
        menu.add_command(label="Delete", command=self._delete_selected)
        menu.tk_popup(event.x_root, event.y_root)

    def _on_scroll(self, event):
        # Zoom centered on cursor
        old_wx, old_wy = self._screen_to_world(event.x, event.y)
        factor = 1.1 if event.delta > 0 else 0.9
        self.zoom_level = max(0.1, min(5.0, self.zoom_level * factor))
        # Adjust pan so cursor stays over same world point
        new_wx, new_wy = self._screen_to_world(event.x, event.y)
        self.pan_x += new_wx - old_wx
        self.pan_y += new_wy - old_wy
        self._zoom_label.configure(text=f"{int(self.zoom_level * 100)}%")
        self._draw()

    def _start_pan(self, event):
        self._pan_drag = (event.x, event.y, self.pan_x, self.pan_y)
        return "break"

    def _do_pan(self, event):
        if not self._pan_drag:
            return
        ox, oy, px, py = self._pan_drag
        self.pan_x = px + (event.x - ox) / self.zoom_level
        self.pan_y = py + (event.y - oy) / self.zoom_level
        self._draw()
        return "break"

    def _end_pan(self, event):
        self._pan_drag = None
        return "break"

    def _on_key(self, event):
        # Ignore if focus is in an entry/text widget
        w = self.focus_get()
        if isinstance(w, (tk.Entry, tk.Text)):
            return

        if event.char == "/":
            self._focus_cmd()
            return "break"

        # Number keys for phase
        phase_map = {"1": "recon", "2": "research", "3": "run", "4": "review", "5": "done", "6": "failed"}
        if event.char in phase_map:
            self._set_phase(phase_map[event.char])
            return

        if event.keysym == "Tab":
            self._cycle_children()
            return "break"

        if event.keysym == "Escape":
            self.selected_id = None
            self._build_panel()
            self._draw()

    def _cycle_children(self):
        if not self.selected_id or self.selected_id not in self.graph.nodes:
            return
        node = self.graph.nodes[self.selected_id]
        children = self.graph.children(node.parent_id or node.id)
        if not children:
            children = self.graph.children(node.id)
        if not children:
            return
        ids = [c.id for c in children]
        if self.selected_id in ids:
            idx = (ids.index(self.selected_id) + 1) % len(ids)
        else:
            idx = 0
        self.selected_id = ids[idx]
        self._build_panel()
        self._draw()

    # ---- Fit view ----

    def _fit_view(self):
        if not self.graph.nodes:
            return
        xs = [n.x for n in self.graph.nodes.values()]
        ys = [n.y for n in self.graph.nodes.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 100)
        span_y = max(max_y - min_y, 100)
        cw = max(self.canvas.winfo_width(), 100)
        ch = max(self.canvas.winfo_height(), 100)
        self.zoom_level = min(cw / (span_x + 100), ch / (span_y + 100), 3.0)
        self.pan_x = -(min_x + max_x) / 2
        self.pan_y = -(min_y + max_y) / 2
        self._zoom_label.configure(text=f"{int(self.zoom_level * 100)}%")
        self._draw()

    # ---- Actions ----

    def _update_field(self, field, value):
        if self.selected_id and self.selected_id in self.graph.nodes:
            setattr(self.graph.nodes[self.selected_id], field, value)
            self._draw()

    def _set_phase(self, phase):
        if self.selected_id and self.selected_id in self.graph.nodes:
            self.graph.nodes[self.selected_id].phase = phase
            self._build_panel()
            self._draw()
            self._set_status(f"Phase: {phase}")

    def _set_type(self, ntype):
        if self.selected_id and self.selected_id in self.graph.nodes:
            self.graph.nodes[self.selected_id].node_type = ntype
            self._build_panel()
            self._draw()

    def _on_notes_change(self, event=None):
        if self.selected_id and self.selected_id in self.graph.nodes:
            self.graph.nodes[self.selected_id].notes = self._notes_text.get("1.0", "end-1c")

    def _cmd_add_child(self, label=None):
        parent = self.graph.nodes.get(self.selected_id) if self.selected_id else None
        if not label:
            label = simpledialog.askstring("New Node", "Label:", parent=self)
        if not label:
            return

        if parent:
            children = self.graph.children(parent.id)
            count = len(children)
            angle = (count * (2 * math.pi / max(6, count + 1))) - math.pi / 2
            dist = 120
            x = parent.x + dist * math.cos(angle)
            y = parent.y + dist * math.sin(angle)
            pid = parent.id
        else:
            x, y = 0, 0
            pid = None

        n = Node(label=label, node_type="service", x=x, y=y, parent_id=pid)
        self.graph.add_node(n)
        self.selected_id = n.id
        self._build_panel()
        self._draw()
        self._set_status(f"Added: {label}")

    def _add_hyp(self):
        if not self.selected_id:
            return
        hyp = simpledialog.askstring("Hypothesis", "What might work here?", parent=self)
        if hyp:
            self.graph.nodes[self.selected_id].hypotheses.append(hyp)
            self._build_panel()

    def _remove_hyp(self, idx):
        if self.selected_id and self.selected_id in self.graph.nodes:
            hyps = self.graph.nodes[self.selected_id].hypotheses
            if 0 <= idx < len(hyps):
                hyps.pop(idx)
                self._build_panel()

    def _delete_selected(self):
        if not self.selected_id:
            return
        node = self.graph.nodes.get(self.selected_id)
        if not node:
            return
        if node.node_type == "target":
            self._set_status("Cannot delete the target node")
            return
        label = node.label
        self.graph.remove_node(self.selected_id)
        self.selected_id = None
        self._build_panel()
        self._draw()
        self._set_status(f"Deleted: {label}")

    def _save(self):
        self.graph.save(self.save_path)
        self._toast("Saved")
        self._set_status("Saved")

    def _auto_save(self):
        try:
            self.graph.save(self.save_path)
        except Exception:
            pass
        self.after(30000, self._auto_save)

    # ---- Command Bar ----

    def _focus_cmd(self):
        self._cmd_entry.focus_set()
        self._cmd_entry.select_range(0, tk.END)

    def _exec_cmd(self, event=None):
        raw = self._cmd_var.get().strip()
        if not raw:
            return
        self.cmd_history.append(raw)
        self.cmd_history_idx = -1
        self._cmd_var.set("")

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "add":
            if arg:
                self._cmd_add_child(label=arg)
            else:
                self._set_status("Usage: add <label>")
        elif cmd == "phase":
            if arg in PHASES:
                self._set_phase(arg)
            else:
                self._set_status(f"Unknown phase. Use: {', '.join(PHASES)}")
        elif cmd == "hyp":
            if self.selected_id and arg:
                self.graph.nodes[self.selected_id].hypotheses.append(arg)
                self._build_panel()
                self._draw()
                self._set_status(f"Hypothesis added")
            else:
                self._set_status("Select a node first, then: hyp <text>")
        elif cmd == "note":
            if self.selected_id and arg:
                node = self.graph.nodes[self.selected_id]
                node.notes = (node.notes + "\n" + arg).strip()
                self._build_panel()
                self._set_status("Note appended")
            else:
                self._set_status("Select a node first, then: note <text>")
        elif cmd == "type":
            if arg in NODE_TYPES:
                self._set_type(arg)
            else:
                self._set_status(f"Unknown type. Use: {', '.join(NODE_TYPES)}")
        elif cmd == "search":
            if arg:
                self._async_search(arg)
            else:
                self._set_status("Usage: search <query>")
        elif cmd == "recall":
            if arg:
                self._async_recall(arg)
            else:
                self._set_status("Usage: recall <query>")
        elif cmd == "remember":
            if arg:
                self._async_remember(arg)
            else:
                self._set_status("Usage: remember <text>")
        elif cmd == "oracle":
            if arg:
                self._async_oracle(arg)
            else:
                self._set_status("Usage: oracle <query>")
        elif cmd == "scan":
            if arg:
                self._cmd_scan(arg.strip())
            else:
                self._set_status("Usage: scan <ip-address>")
        elif cmd == "paste":
            self._cmd_paste()
        elif cmd == "save":
            self._save()
        elif cmd == "fit":
            self._fit_view()
        elif cmd == "delete":
            self._delete_selected()
        elif cmd == "quickscan":
            # Fast scan: top 1000 ports only
            if arg:
                self._cmd_quickscan(arg.strip())
            else:
                self._set_status("Usage: quickscan <ip-address>")
        elif cmd == "webscan":
            # Run gobuster on an http service
            if arg:
                self._cmd_webscan(arg.strip())
            else:
                self._set_status("Usage: webscan <url>")
        else:
            self._set_status(f"Unknown command: {cmd}. Try: scan, add, phase, hyp, note, search, recall, oracle, paste")

        self.canvas.focus_set()

    def _cmd_hist_prev(self, event):
        if not self.cmd_history:
            return
        if self.cmd_history_idx == -1:
            self.cmd_history_idx = len(self.cmd_history) - 1
        elif self.cmd_history_idx > 0:
            self.cmd_history_idx -= 1
        self._cmd_var.set(self.cmd_history[self.cmd_history_idx])

    def _cmd_hist_next(self, event):
        if self.cmd_history_idx == -1:
            return
        if self.cmd_history_idx < len(self.cmd_history) - 1:
            self.cmd_history_idx += 1
            self._cmd_var.set(self.cmd_history[self.cmd_history_idx])
        else:
            self.cmd_history_idx = -1
            self._cmd_var.set("")

    # ---- Async HTTP commands ----

    def _async_search(self, query):
        self._set_status("Searching Smart Router...")
        def do():
            try:
                r = http_post(SMART_ROUTER_URL, {"query": query, "top_k": 5}, timeout=30)
                results = r.get("results", r.get("data", [r]))
                lines = [f"--- Smart Router: {query} ---"]
                if isinstance(results, list):
                    for item in results[:5]:
                        if isinstance(item, dict):
                            lines.append(item.get("text", item.get("content", str(item)))[:200])
                        else:
                            lines.append(str(item)[:200])
                else:
                    lines.append(str(results)[:500])
                self.after(0, lambda: self._log_lines(lines, "RESULT"))
            except Exception as e:
                self.after(0, lambda: self._log_lines([f"Search error: {e}"], "ERROR"))
        threading.Thread(target=do, daemon=True).start()

    def _async_recall(self, query):
        self._set_status("Querying Hyphae...")
        def do():
            try:
                r = http_post(HYPHAE_RECALL_URL, {"query": query, "top_k": 10}, timeout=15)
                results = r.get("results", r.get("memories", [r]))
                lines = [f"--- Hyphae Recall: {query} ---"]
                if isinstance(results, list):
                    for item in results[:10]:
                        if isinstance(item, dict):
                            lines.append(item.get("text", item.get("content", str(item)))[:200])
                        else:
                            lines.append(str(item)[:200])
                else:
                    lines.append(str(results)[:500])
                self.after(0, lambda: self._log_lines(lines, "RESULT"))
            except Exception as e:
                self.after(0, lambda: self._log_lines([f"Recall error: {e}"], "ERROR"))
        threading.Thread(target=do, daemon=True).start()

    def _async_remember(self, text):
        self._set_status("Saving to Hyphae...")
        def do():
            try:
                http_post(HYPHAE_REMEMBER_URL, {"text": text, "source": "treadstone-ui"}, timeout=10)
                self.after(0, lambda: self._set_status("Remembered"))
                self.after(0, lambda: self._toast("Saved to Hyphae"))
            except Exception as e:
                self.after(0, lambda: self._log_lines([f"Remember error: {e}"], "ERROR"))
        threading.Thread(target=do, daemon=True).start()

    def _async_oracle(self, query):
        self._set_status("Asking Oracle (may take a while)...")
        def do():
            try:
                r = http_post(ORACLE_URL, {
                    "model": ORACLE_MODEL, "prompt": query, "stream": False
                }, timeout=120)
                resp = r.get("response", str(r))
                lines = [f"--- Oracle: {query} ---"]
                for line in resp.split("\n"):
                    lines.append(line)
                self.after(0, lambda: self._log_lines(lines, "RESULT"))
            except Exception as e:
                self.after(0, lambda: self._log_lines([f"Oracle error: {e}"], "ERROR"))
        threading.Thread(target=do, daemon=True).start()

    # ---- Toolbar button handlers ----

    def _guess_target_ip(self):
        """Try to get target IP from HTB API or mission state."""
        try:
            result = subprocess.run(
                ["python3", str(Path.home() / "openkeel/scripts/htb_api.py"), "status"],
                capture_output=True, text=True, timeout=10
            )
            m = re.search(r'IP:\s*(\d+\.\d+\.\d+\.\d+)', result.stdout)
            if m:
                return m.group(1)
        except Exception:
            pass
        # Check if target node already has an IP-like label
        for n in self.graph.nodes.values():
            if n.node_type == "target" and re.match(r'\d+\.\d+\.\d+\.\d+', n.label):
                return n.label
        return ""

    def _btn_scan(self):
        ip = self._target_var.get().strip()
        if not ip:
            self._set_status("Enter target IP first")
            return
        self._scan_btn.configure(bg="#666666", text="SCANNING...", state=tk.DISABLED)
        self._cmd_scan(ip)

    def _btn_quickscan(self):
        ip = self._target_var.get().strip()
        if not ip:
            self._set_status("Enter target IP first")
            return
        self._cmd_quickscan(ip)

    def _btn_webscan(self):
        """Auto-webscan all HTTP services in the graph."""
        ip = self._target_var.get().strip()
        http_nodes = [n for n in self.graph.nodes.values()
                      if n.node_type == "service" and any(s in n.label.lower() for s in ("http", "web", "443", "8080", "8443"))]
        if not http_nodes:
            if ip:
                self._cmd_webscan(f"http://{ip}")
            else:
                self._set_status("No HTTP services found. Run scan first.")
            return
        for node in http_nodes:
            port = re.search(r':(\d+)', node.label)
            port_num = port.group(1) if port else "80"
            proto = "https" if "443" in port_num or "https" in node.label.lower() else "http"
            url = f"{proto}://{ip}:{port_num}"
            self._log_lines([f"Web scanning {url}..."], "INFO")
            self._cmd_webscan(url)
            # Attach results to this node
            self.selected_id = node.id

    def _btn_research_all(self):
        """Research all service nodes that haven't been researched yet."""
        count = 0
        for node in self.graph.nodes.values():
            if node.node_type == "service" and not node.hypotheses:
                svc = node.label.split(":")[0].lower()
                version = ""
                for line in node.notes.split("\n"):
                    if line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()
                port = re.search(r':(\d+)', node.label)
                port_num = port.group(1) if port else ""
                self._auto_research_service(node.id, svc, version, port_num)
                count += 1
        if count:
            self._set_status(f"Researching {count} services...")
            self._toast(f"Auto-researching {count} services")
        else:
            self._set_status("All services already have hypotheses")

    def _btn_oracle(self):
        """Ask Oracle about the current state of the attack."""
        # Build context from graph
        services = [n.label for n in self.graph.nodes.values() if n.node_type == "service"]
        findings = [n.label for n in self.graph.nodes.values() if n.node_type in ("finding", "credential")]
        target = self._target_var.get().strip()

        context = f"Target: {target}. Services: {', '.join(services)}."
        if findings:
            context += f" Findings: {', '.join(findings)}."

        selected = self.graph.nodes.get(self.selected_id)
        if selected:
            context += f" Currently looking at: {selected.label} ({selected.node_type}, phase: {selected.phase})."
            if selected.hypotheses:
                hyps = [h if isinstance(h, str) else h.get("text", "") for h in selected.hypotheses[:3]]
                context += f" Hypotheses: {'; '.join(hyps)}."

        query = f"I'm attacking an HTB machine. {context} What should I try next? Be specific and actionable."
        self._async_oracle(query)
        self._set_status("Asking Oracle for strategic advice...")

    def _btn_autopilot(self):
        """Full auto: scan → research → webscan HTTP → oracle advice."""
        ip = self._target_var.get().strip()
        if not ip:
            self._set_status("Enter target IP first")
            return

        self._log_lines(["=== AUTO PILOT ENGAGED ===", f"Target: {ip}"], "RESULT")
        self._toast("Auto Pilot: Starting full scan chain")

        # Chain: quickscan → (on complete) auto-research → webscan → oracle
        self._autopilot_ip = ip
        self._autopilot_stage = 0
        self._cmd_quickscan(ip)
        # Check every 5s if scan is done and advance
        self.after(5000, self._autopilot_check)

    def _autopilot_check(self):
        """Check autopilot progress and chain next action."""
        if not hasattr(self, '_autopilot_stage'):
            return

        services = [n for n in self.graph.nodes.values() if n.node_type == "service"]

        if self._autopilot_stage == 0 and services:
            # Scan done, research all
            self._autopilot_stage = 1
            self._log_lines(["[AUTOPILOT] Scan complete, researching services..."], "RESULT")
            self._btn_research_all()
            # Also start full scan in background
            self._cmd_scan(self._autopilot_ip)
            self.after(8000, self._autopilot_check)
        elif self._autopilot_stage == 1:
            # Research done (or at least started), webscan HTTP
            self._autopilot_stage = 2
            http_nodes = [n for n in services if any(s in n.label.lower() for s in ("http", "web", "443", "8080"))]
            if http_nodes:
                self._log_lines(["[AUTOPILOT] Starting web scans..."], "RESULT")
                self._btn_webscan()
            self.after(10000, self._autopilot_check)
        elif self._autopilot_stage == 2:
            # Webscan done, ask oracle
            self._autopilot_stage = 3
            self._log_lines(["[AUTOPILOT] Asking Oracle for strategy..."], "RESULT")
            self._btn_oracle()
            self._log_lines(["=== AUTO PILOT COMPLETE ===", "Review the tree and pick your attack vector."], "RESULT")
            self._scan_btn.configure(bg="#D0021B", text="SCAN", state=tk.NORMAL)
            del self._autopilot_stage
        else:
            # Still waiting
            self.after(5000, self._autopilot_check)

    # ---- Automation: scan, auto-research, suggested action ----

    def _cmd_scan(self, target_ip):
        """Run nmap against target, parse results, auto-create service nodes."""
        self._set_status(f"Scanning {target_ip}...")
        self._log_lines([f"Starting nmap scan on {target_ip}..."], "INFO")

        def do_scan():
            try:
                result = subprocess.run(
                    ["nmap", "-sV", "-sC", "--min-rate", "3000", "-p-", target_ip],
                    capture_output=True, text=True, timeout=600
                )
                output = result.stdout
                self.after(0, lambda: self._parse_nmap(target_ip, output))
            except subprocess.TimeoutExpired:
                self.after(0, lambda: self._log_lines(["Nmap scan timed out (10min)"], "ERROR"))
            except FileNotFoundError:
                self.after(0, lambda: self._log_lines(["nmap not found, install it first"], "ERROR"))
            except Exception as e:
                self.after(0, lambda: self._log_lines([f"Scan error: {e}"], "ERROR"))
        threading.Thread(target=do_scan, daemon=True).start()

    def _parse_nmap(self, target_ip, output):
        """Parse nmap output, create service nodes, auto-research each."""
        self._log_lines(["--- Nmap Results ---"] + output.split("\n")[-30:], "RESULT")

        # Store full output in target node notes
        root = self._get_target_node()
        if root:
            root.notes = (root.notes + "\n\n--- NMAP ---\n" + output).strip()
            root.label = target_ip
            root.phase = "research"  # advance target past recon

        # Parse open ports: "22/tcp   open  ssh     OpenSSH 8.9p1"
        services_found = []
        for line in output.split("\n"):
            m = re.match(r'(\d+)/tcp\s+open\s+(\S+)\s*(.*)', line)
            if not m:
                m = re.match(r'(\d+)/udp\s+open\s+(\S+)\s*(.*)', line)
            if m:
                port, service, version = m.group(1), m.group(2), m.group(3).strip()
                services_found.append((port, service, version))

        if not services_found:
            self._log_lines(["No open ports found or couldn't parse nmap output."], "ERROR")
            self._toast("No open ports found")
            return

        # Create service nodes radially around target
        root_id = root.id if root else None
        # Dedup: skip services that already exist
        existing_labels = {n.label for n in self.graph.nodes.values()}
        new_services = []
        for port, service, version in services_found:
            label = f"{service}:{port}"
            if label in existing_labels:
                # Update notes on existing node instead
                for n in self.graph.nodes.values():
                    if n.label == label and version and "Version:" not in n.notes:
                        n.notes = (n.notes + f"\nVersion: {version}").strip()
                continue
            new_services.append((port, service, version))

        existing_children = self.graph.children(root_id) if root_id else []
        offset = len(existing_children)
        total = max(len(new_services) + offset, 1)

        services_found = []
        for i, (port, service, version) in enumerate(new_services):
            label = f"{service}:{port}"
            angle = ((offset + i) * 2 * math.pi / total) - math.pi / 2
            dist = 150
            parent_x = root.x if root else 0
            parent_y = root.y if root else 0
            x = parent_x + dist * math.cos(angle)
            y = parent_y + dist * math.sin(angle)

            n = Node(
                label=label, node_type="service", phase="recon",
                x=x, y=y, notes=f"Version: {version}\nPort: {port}\nService: {service}",
                parent_id=root_id
            )
            self.graph.add_node(n)
            existing_labels.add(label)
            services_found.append((port, service, version, n.id))

        self._build_panel()
        self._draw()
        self._toast(f"Found {len(services_found)} services")
        self._set_status(f"Scan complete: {len(services_found)} open ports")

        # Auto-research each service
        for port, service, version, node_id in services_found:
            self._auto_research_service(node_id, service, version, port)

        # Re-enable scan button
        self._scan_btn.configure(bg="#D0021B", text="SCAN", state=tk.NORMAL)

        # Remember in Hyphae
        svc_summary = ", ".join(f"{s}:{p}" for p, s, v, _ in services_found)
        self._async_remember(f"Target {target_ip} has open ports: {svc_summary}")

        # Auto-chain: if not in autopilot, auto-research and webscan HTTP
        if not hasattr(self, '_autopilot_stage'):
            self.after(2000, self._btn_research_all)
            http_svcs = [s for p, s, v, nid in services_found if s.lower() in ("http", "https", "http-proxy")]
            if http_svcs:
                self.after(5000, self._btn_webscan)

        # Update suggested action
        self.after(500, self._update_suggestion)

    def _auto_research_service(self, node_id, service, version, port):
        """Query Smart Router for a service and populate hypotheses."""
        svc_key = service.lower().split("/")[0]  # normalize
        queries = SERVICE_RESEARCH.get(svc_key, [f"{service} exploit", f"{service} enumeration"])

        def do_research():
            all_hyps = []
            for query in queries[:3]:
                full_query = f"{query} {version}" if version else query
                try:
                    r = http_post(SMART_ROUTER_URL, {"query": full_query, "top_k": 3}, timeout=15)
                    results = r.get("results", r.get("data", []))
                    if isinstance(results, list):
                        for item in results[:2]:
                            if isinstance(item, dict):
                                text = item.get("text", item.get("content", ""))
                            else:
                                text = str(item)
                            # Extract actionable hypothesis from result
                            if text and len(text) > 10:
                                short = text[:120].strip().replace("\n", " ")
                                all_hyps.append(short)
                except Exception:
                    pass

            # Also add generic hypotheses based on service type
            generic = {
                "ssh": ["Try password brute force with hydra", "Check for key-based auth bypass"],
                "http": ["Run gobuster/feroxbuster for hidden dirs", "Check for default credentials", "Look for CMS/framework version vulns"],
                "https": ["Check TLS cert for hostnames/subdomains", "Run gobuster", "Check for web app vulns"],
                "ftp": ["Try anonymous login", "Check FTP version for known exploits"],
                "smb": ["Enumerate shares with smbclient -L", "Check for null session", "Run enum4linux"],
                "mysql": ["Try root with no password", "Check version for UDF exploit"],
                "redis": ["Try unauthenticated access", "Check for module load RCE"],
            }
            for g in generic.get(svc_key, [f"Enumerate {service} further", f"Check {service} version for CVEs"]):
                all_hyps.append(g)

            # Deduplicate
            seen = set()
            unique = []
            for h in all_hyps:
                key = h[:50].lower()
                if key not in seen:
                    seen.add(key)
                    unique.append(h)

            def apply():
                if node_id in self.graph.nodes:
                    node = self.graph.nodes[node_id]
                    node.hypotheses = unique[:8]  # cap at 8
                    node.phase = "research"
                    self._build_panel()
                    self._draw()
            self.after(0, apply)

        threading.Thread(target=do_research, daemon=True).start()

    def _cmd_quickscan(self, target_ip):
        """Fast scan: top 1000 ports with version detection."""
        self._set_status(f"Quick scanning {target_ip}...")
        self._log_lines([f"Quick nmap scan (top 1000) on {target_ip}..."], "INFO")

        def do_scan():
            try:
                result = subprocess.run(
                    ["nmap", "-sV", "--min-rate", "3000", target_ip],
                    capture_output=True, text=True, timeout=120
                )
                self.after(0, lambda: self._parse_nmap(target_ip, result.stdout))
            except Exception as e:
                self.after(0, lambda: self._log_lines([f"Quick scan error: {e}"], "ERROR"))
        threading.Thread(target=do_scan, daemon=True).start()

    def _cmd_webscan(self, url):
        """Run gobuster/feroxbuster against a URL."""
        self._set_status(f"Web scanning {url}...")
        self._log_lines([f"Running gobuster on {url}..."], "INFO")

        def do_scan():
            try:
                # Try feroxbuster first, fallback to gobuster
                wordlist = "/usr/share/wordlists/dirb/common.txt"
                if not os.path.exists(wordlist):
                    wordlist = "/usr/share/seclists/Discovery/Web-Content/common.txt"

                for tool in ["feroxbuster", "gobuster"]:
                    if tool == "feroxbuster":
                        cmd = ["feroxbuster", "-u", url, "-w", wordlist, "-q", "--no-state", "-t", "50"]
                    else:
                        cmd = ["gobuster", "dir", "-u", url, "-w", wordlist, "-q", "-t", "50"]
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                        if result.returncode == 0 or result.stdout.strip():
                            self.after(0, lambda o=result.stdout: self._parse_tool_output(o))
                            return
                    except FileNotFoundError:
                        continue
                self.after(0, lambda: self._log_lines(["Neither feroxbuster nor gobuster found"], "ERROR"))
            except Exception as e:
                self.after(0, lambda: self._log_lines([f"Web scan error: {e}"], "ERROR"))
        threading.Thread(target=do_scan, daemon=True).start()

    def _get_target_node(self):
        """Find the root target node."""
        for n in self.graph.nodes.values():
            if n.node_type == "target":
                return n
        return None

    def _cmd_paste(self):
        """Open a paste dialog for tool output, auto-parse it."""
        win = tk.Toplevel(self)
        win.title("Paste Tool Output")
        win.geometry("700x500")
        win.configure(bg="#1a1a2e")

        tk.Label(win, text="Paste command output below:", bg="#1a1a2e", fg="#e0e0e0",
                 font=("Consolas", 11)).pack(anchor="w", padx=10, pady=5)
        text = tk.Text(win, bg="#0d0d1a", fg="#e0e0e0", font=("Consolas", 10),
                       insertbackground="white", wrap=tk.WORD)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        def process():
            content = text.get("1.0", "end-1c")
            win.destroy()
            self._parse_tool_output(content)

        tk.Button(win, text="Parse & Add to Tree", bg="#e94560", fg="white",
                  font=("Consolas", 11, "bold"), relief="flat", command=process).pack(pady=10)

    def _parse_tool_output(self, output):
        """Parse arbitrary tool output and add findings as nodes."""
        findings = []

        # Detect nmap-style port lines
        for line in output.split("\n"):
            m = re.match(r'(\d+)/tcp\s+open\s+(\S+)\s*(.*)', line)
            if m:
                findings.append(("service", f"{m.group(2)}:{m.group(1)}", m.group(3).strip()))
                continue

            # Detect URLs/directories from gobuster/feroxbuster
            m = re.match(r'.*(/[\w\-./]+)\s+.*Status:\s*(\d+)', line)
            if m and m.group(2) in ("200", "301", "302", "403"):
                findings.append(("finding", f"DIR: {m.group(1)}", f"Status {m.group(2)}"))
                continue

            # Detect credentials
            if re.search(r'(password|credential|passwd|cred)\s*[:=]\s*\S+', line, re.IGNORECASE):
                findings.append(("credential", f"CRED: {line.strip()[:60]}", line.strip()))
                continue

            # Detect usernames
            m = re.search(r'(?:user(?:name)?|login)\s*[:=]\s*(\S+)', line, re.IGNORECASE)
            if m:
                findings.append(("credential", f"USER: {m.group(1)}", line.strip()))
                continue

        if not findings:
            # Store as notes on selected node
            if self.selected_id and self.selected_id in self.graph.nodes:
                node = self.graph.nodes[self.selected_id]
                node.notes = (node.notes + "\n\n--- Tool Output ---\n" + output[:2000]).strip()
                self._build_panel()
                self._set_status("Output added to selected node's notes")
            else:
                self._log_lines(["Couldn't auto-parse output. Select a node and it'll go to notes."], "INFO")
            return

        # Add findings as child nodes of selected (or target)
        parent = self.graph.nodes.get(self.selected_id) or self._get_target_node()
        if not parent:
            return

        existing_labels = {n.label for n in self.graph.nodes.values()}
        existing_children = self.graph.children(parent.id)
        count = len(existing_children)
        added = 0

        for i, (ntype, label, notes) in enumerate(findings):
            if label in existing_labels:
                continue
            angle = ((count + i) * 2 * math.pi / max(8, count + len(findings))) - math.pi / 2
            dist = 150
            x = parent.x + dist * math.cos(angle)
            y = parent.y + dist * math.sin(angle)
            n = Node(label=label, node_type=ntype, phase="recon", x=x, y=y,
                     notes=notes, parent_id=parent.id)
            self.graph.add_node(n)
            existing_labels.add(label)
            added += 1

        self._build_panel()
        self._draw()
        self._toast(f"Parsed {added} new findings")
        self._set_status(f"Added {added} nodes from output ({len(findings) - added} duplicates skipped)")
        self.after(500, self._update_suggestion)

    # ---- Suggested Next Action ----

    def _update_suggestion(self):
        """Analyze graph state and suggest what to do next."""
        nodes = list(self.graph.nodes.values())
        if not nodes:
            self._set_suggestion("Start: enter target IP with 'scan <ip>'")
            return

        target = self._get_target_node()

        # Count nodes by phase
        by_phase = {}
        for n in nodes:
            by_phase.setdefault(n.phase, []).append(n)

        services = [n for n in nodes if n.node_type == "service"]
        findings = [n for n in nodes if n.node_type in ("finding", "credential")]

        # Only target node, no services? Need to scan.
        if len(nodes) == 1 and target:
            self._set_suggestion("Run: scan <target-ip>  (e.g. scan 10.129.244.184)")
            return

        # Have services but all in recon? Research them.
        recon_services = [s for s in services if s.phase == "recon"]
        if recon_services:
            svc = recon_services[0]
            self._set_suggestion(f"Research: click '{svc.label}', review hypotheses, press 2 for research phase")
            return

        # Have services in research? Pick one to run.
        research_services = [s for s in services if s.phase == "research"]
        if research_services:
            # Pick the one with most hypotheses
            best = max(research_services, key=lambda s: len(s.hypotheses))
            hyp_text = best.hypotheses[0] if best.hypotheses else "check version for CVEs"
            if isinstance(hyp_text, dict):
                hyp_text = hyp_text.get("text", str(hyp_text))
            self._set_suggestion(f"Try: {hyp_text[:80]} (on {best.label})")
            return

        # Have services in run? Check for results.
        run_services = [s for s in services if s.phase == "run"]
        if run_services:
            svc = run_services[0]
            self._set_suggestion(f"Review: did '{svc.label}' work? Press 4 to review, add findings, or press 6 if failed")
            return

        # Have findings? Research them deeper.
        recon_findings = [f for f in findings if f.phase == "recon"]
        if recon_findings:
            f = recon_findings[0]
            self._set_suggestion(f"Investigate: '{f.label}' — search for exploits or enumerate further")
            return

        # Everything reviewed? Look for privesc.
        done_count = len(by_phase.get("done", []))
        if done_count > 0 and not by_phase.get("run", []) and not by_phase.get("research", []):
            self._set_suggestion("Pivot: got a shell? Add a 'machine' node, scan internal network, look for privesc")
            return

        # Default
        self._set_suggestion("Explore: look at failed nodes, try alternative approaches, or scan deeper")

    def _set_suggestion(self, text):
        """Update the suggestion bar."""
        if hasattr(self, '_suggestion_var'):
            self._suggestion_var.set(f">> {text}")

    def _suggestion_loop(self):
        """Periodically refresh the suggestion."""
        self._update_suggestion()
        self.after(10000, self._suggestion_loop)

    # ---- Observer Health ----

    def _health_check(self):
        def check_one(name, port):
            url = f"http://127.0.0.1:{port}/api/tags"
            t0 = time.time()
            try:
                http_get(url, timeout=5)
                ms = int((time.time() - t0) * 1000)
                self.after(0, lambda: self._update_led(name, True, ms))
            except Exception:
                self.after(0, lambda: self._update_led(name, False, 0))

        for name, port in OBSERVER_PORTS.items():
            threading.Thread(target=check_one, args=(name, port), daemon=True).start()
        self.after(30000, self._health_check)

    def _update_led(self, name, up, ms):
        self.observer_status[name] = {"up": up, "ms": ms}
        led = self._leds.get(name)
        if led:
            led.delete("dot")
            color = "#00CC00" if up else "#CC0000"
            led.create_oval(1, 1, 9, 9, fill=color, outline="", tags="dot")

    def _show_observer_info(self, name):
        info = self.observer_status.get(name, {})
        if info.get("up"):
            self._set_status(f"{name}: UP ({info.get('ms', '?')}ms)")
        else:
            self._set_status(f"{name}: DOWN")

    # ---- Log Panel ----

    def _toggle_log(self):
        if self.log_visible:
            self._left.forget(self._log_frame)
            self.log_visible = False
        else:
            self._left.add(self._log_frame, stretch="never")
            self.log_visible = True

    def _log_lines(self, lines, tag="INFO"):
        self._log_text.configure(state=tk.NORMAL)
        for line in lines:
            self._log_text.insert(tk.END, line + "\n", tag)
        self._log_text.see(tk.END)
        self._log_text.configure(state=tk.DISABLED)
        self._set_status(lines[0] if lines else "")

    def _tail_logs(self):
        self._tail_file(self.mission_dir / "distilled_log.jsonl", "_log_offset")
        self._tail_file(self.mission_dir / "observer_nudges.jsonl", "_nudge_offset", is_nudge=True)
        self.after(5000, self._tail_logs)

    def _tail_file(self, path, offset_attr, is_nudge=False):
        if not path.exists():
            return
        try:
            current = getattr(self, offset_attr)
            with open(path, "r", encoding="utf-8") as f:
                f.seek(current)
                new_data = f.read()
                setattr(self, offset_attr, f.tell())
            if not new_data.strip():
                return
            self._log_text.configure(state=tk.NORMAL)
            for line in new_data.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    entry = {"message": line}
                cat = entry.get("category", "OBSERVER" if is_nudge else "INFO")
                msg = entry.get("message", entry.get("text", str(entry)))
                tag = cat if cat in LOG_COLORS else "INFO"
                ts = entry.get("timestamp", "")
                prefix = f"[{ts}] " if ts else ""
                self._log_text.insert(tk.END, f"{prefix}{msg}\n", tag)
                if is_nudge:
                    self._toast(msg[:60])
            self._log_text.see(tk.END)
            self._log_text.configure(state=tk.DISABLED)
        except Exception:
            pass

    # ---- Status & Toast ----

    def _set_status(self, text):
        self._status_var.set(text)

    def _toast(self, msg, duration=5000):
        cw = self.canvas.winfo_width()
        y_offset = 20 + len(self._toasts) * 30
        tid = self.canvas.create_text(cw - 20, y_offset, text=msg, anchor="ne",
                                      fill="#e0e0e0", font=("Consolas", 10),
                                      tags="toast")
        bg = self.canvas.create_rectangle(
            self.canvas.bbox(tid), fill="#333355", outline="#e94560", tags="toast_bg")
        self.canvas.tag_lower(bg, tid)
        self._toasts.append((tid, bg))

        def remove():
            try:
                self.canvas.delete(tid)
                self.canvas.delete(bg)
                self._toasts = [(t, b) for t, b in self._toasts if t != tid]
            except Exception:
                pass
        self.after(duration, remove)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Treadstone Visual Attack Tree")
    parser.add_argument("--mission", "-m", default="", help="Mission name")
    args = parser.parse_args()

    mission = args.mission
    if not mission:
        active_file = OPENKEEL_DIR / "active_mission.txt"
        if active_file.exists():
            mission = active_file.read_text(encoding="utf-8").strip()

    app = TreadstoneUI(mission=mission)
    app.mainloop()


if __name__ == "__main__":
    main()
