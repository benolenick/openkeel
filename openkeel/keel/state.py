"""Mission state CRUD operations."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PlanStep:
    """A single step in a mission plan."""
    id: int
    step: str
    status: str = "pending"  # "pending", "in_progress", "done", "skipped"
    time_box_minutes: int = 0  # 0 = no time box


@dataclass
class Mission:
    """Mission state."""
    name: str
    objective: str = ""
    tags: list[str] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    credentials: list[str] = field(default_factory=list)
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict for YAML serialization."""
        d = {
            "name": self.name,
            "objective": self.objective,
            "tags": self.tags,
            "plan": [
                {"id": s.id, "step": s.step, "status": s.status, "time_box_minutes": s.time_box_minutes}
                for s in self.plan
            ],
            "findings": self.findings,
            "credentials": self.credentials,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mission:
        """Create a Mission from a plain dict (loaded from YAML)."""
        plan_steps = []
        for raw_step in data.get("plan", []):
            if isinstance(raw_step, dict):
                plan_steps.append(PlanStep(
                    id=raw_step.get("id", 0),
                    step=raw_step.get("step", ""),
                    status=raw_step.get("status", "pending"),
                    time_box_minutes=raw_step.get("time_box_minutes", 0),
                ))

        return cls(
            name=data.get("name", "unnamed"),
            objective=data.get("objective", ""),
            tags=data.get("tags", []),
            plan=plan_steps,
            findings=data.get("findings", []),
            credentials=data.get("credentials", []),
            notes=data.get("notes", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    def format_injection(self) -> str:
        """Format mission state for context injection into the agent."""
        lines = [
            "=" * 60,
            "OPENKEEL MISSION STATE (auto-injected, do not ignore)",
            "=" * 60,
            f"OBJECTIVE: {self.objective}",
        ]

        if self.plan:
            lines.append("PLAN:")
            for step in self.plan:
                if step.status == "done":
                    marker = "[x]"
                elif step.status == "in_progress":
                    marker = "[>]"
                elif step.status == "skipped":
                    marker = "[-]"
                else:
                    marker = "[ ]"
                tb = f" (time-box: {step.time_box_minutes}min)" if step.time_box_minutes else ""
                lines.append(f"  {marker} {step.id}. {step.step}{tb}")

        if self.findings:
            lines.append("KEY FINDINGS:")
            for finding in self.findings:
                lines.append(f"  - {finding}")

        if self.credentials:
            lines.append("CREDENTIALS:")
            for cred in self.credentials:
                lines.append(f"  - {cred}")

        if self.notes:
            lines.append(f"NOTES: {self.notes}")

        if self.tags:
            lines.append(f"TAGS: {', '.join(self.tags)}")

        lines.append("=" * 60)
        return "\n".join(lines)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_missions_dir(config: dict[str, Any]) -> Path:
    """Get the missions directory from config, creating it if needed."""
    missions_dir = Path(config.get("keel", {}).get("missions_dir", "~/.openkeel/missions")).expanduser()
    missions_dir.mkdir(parents=True, exist_ok=True)
    return missions_dir


def get_active_mission_name(config: dict[str, Any]) -> str:
    """Get the active mission name from config."""
    return config.get("keel", {}).get("active_mission", "")


def create_mission(
    config: dict[str, Any],
    name: str,
    objective: str = "",
    tags: list[str] | None = None,
) -> Mission:
    """Create a new mission and save it to disk."""
    missions_dir = get_missions_dir(config)

    now = _utc_now()
    mission = Mission(
        name=name,
        objective=objective,
        tags=tags or [],
        created_at=now,
        updated_at=now,
    )

    save_mission(missions_dir, mission)
    return mission


def save_mission(missions_dir: Path, mission: Mission) -> None:
    """Save mission state to disk atomically."""
    mission.updated_at = _utc_now()
    path = missions_dir / f"{mission.name}.yaml"
    tmp_path = path.with_suffix(".yaml.tmp")

    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(mission.to_dict(), f, default_flow_style=False, allow_unicode=True)
        try:
            os.replace(tmp_path, path)
        except OSError:
            tmp_path.rename(path)
    except OSError:
        raise
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def load_mission(missions_dir: Path, name: str) -> Mission | None:
    """Load a mission from disk. Returns None if not found."""
    path = missions_dir / f"{name}.yaml"
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError):
        return None

    if not isinstance(data, dict):
        return None

    return Mission.from_dict(data)


def list_missions(missions_dir: Path) -> list[str]:
    """List all mission names in the missions directory."""
    if not missions_dir.exists():
        return []
    return sorted(p.stem for p in missions_dir.glob("*.yaml") if not p.name.endswith(".tmp"))


def archive_mission(config: dict[str, Any], name: str) -> bool:
    """Archive a mission by renaming it with .archived suffix."""
    missions_dir = get_missions_dir(config)
    path = missions_dir / f"{name}.yaml"
    if not path.exists():
        return False
    archived_path = missions_dir / f"{name}.archived.yaml"
    path.rename(archived_path)
    return True
