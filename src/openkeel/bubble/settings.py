"""Persistent settings for bubble v4."""

import json
import subprocess
import sys
from pathlib import Path

SETTINGS_DIR = Path.home() / ".openkeel2"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

DEFAULTS = {
    "local_model": "gemma4:e2b",
    "local_for": "gather",  # "gather", "reason", "both", "off"
    "keep_warm": True,
    "keep_warm_duration": "24h",
}


def load():
    """Load settings, merging with defaults."""
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            settings.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return settings


def save(settings):
    """Save settings to disk."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")


def open_gui():
    """Open zenity-based settings GUI. Returns True if settings were changed."""
    from . import ollama

    if not ollama.is_available():
        print("[bubble] Ollama not running — can't configure local models", file=sys.stderr)
        return False

    models = ollama.list_models()
    if not models:
        print("[bubble] No Ollama models found", file=sys.stderr)
        return False

    current = load()

    # Step 1: Pick model
    model_list = "|".join(models)
    try:
        result = subprocess.run(
            ["zenity", "--list",
             "--title=Bubble v4 — Local Model",
             "--text=Select the local LLM to use.\nCurrently: " + current["local_model"],
             "--column=Model", "--column=Status",
             "--width=400", "--height=350"] +
            _model_rows(models, current["local_model"]),
            capture_output=True, text=True, timeout=60
        )
    except Exception:
        print("[bubble] zenity not available", file=sys.stderr)
        return False

    if result.returncode != 0:
        return False

    selected_model = result.stdout.strip().split("|")[0]
    if not selected_model:
        return False

    # Step 2: Pick what to use it for
    try:
        result = subprocess.run(
            ["zenity", "--list",
             "--title=Bubble v4 — Local Model Role",
             "--text=What should " + selected_model + " do?",
             "--radiolist",
             "--column=Pick", "--column=Mode", "--column=Description",
             "--width=500", "--height=300",
             "TRUE" if current["local_for"] == "gather" else "FALSE",
             "gather", "Gather data (replaces Haiku API — free)",
             "TRUE" if current["local_for"] == "reason" else "FALSE",
             "reason", "Reason over data (replaces Sonnet CLI — free)",
             "TRUE" if current["local_for"] == "both" else "FALSE",
             "both", "Both gather + reason (fully local — zero cost)",
             "TRUE" if current["local_for"] == "ultra" else "FALSE",
             "ultra", "Local does everything, Haiku judges + escalates to Sonnet (~$0.003)",
             "TRUE" if current["local_for"] == "cascade" else "FALSE",
             "cascade", "Haiku auto-classifies difficulty → routes to local or Sonnet (smartest)",
             "TRUE" if current["local_for"] == "off" else "FALSE",
             "off", "Disabled (API only, v3 behavior)"],
            capture_output=True, text=True, timeout=60
        )
    except Exception:
        return False

    if result.returncode != 0:
        return False

    selected_mode = result.stdout.strip()
    if not selected_mode:
        return False

    # Step 3: Keep warm?
    try:
        result = subprocess.run(
            ["zenity", "--question",
             "--title=Bubble v4 — Keep Warm",
             "--text=Pin " + selected_model + " in VRAM?\n\n"
             "This keeps the model loaded so bubble runs faster.\n"
             "Uses GPU memory until manually unloaded.",
             "--ok-label=Yes, keep warm",
             "--cancel-label=No, load on demand",
             "--width=400"],
            capture_output=True, timeout=30
        )
        do_warm = result.returncode == 0
    except Exception:
        do_warm = True

    # Save
    new_settings = {
        "local_model": selected_model,
        "local_for": selected_mode,
        "keep_warm": do_warm,
        "keep_warm_duration": "24h",
    }
    save(new_settings)

    # Warm if requested
    if do_warm and selected_mode != "off":
        ollama.keep_warm(selected_model, verbose=True)

    print(f"[bubble] Settings saved: model={selected_model}, mode={selected_mode}, warm={do_warm}",
          file=sys.stderr)
    return True


def _model_rows(models, current):
    """Build zenity list rows with status indicators."""
    from . import ollama
    loaded = {m.get("name", "") for m in ollama.get_loaded()}
    rows = []
    for m in models:
        status = ""
        if m == current:
            status += "● selected"
        if m in loaded:
            status += (" + " if status else "") + "warm"
        if not status:
            status = "available"
        rows.extend([m, status])
    return rows
