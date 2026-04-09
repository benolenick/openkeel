#!/usr/bin/env python3
"""Evidence store: keeps raw worker output out of Opus's context."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


class EvidenceStore:
    """File-backed store for raw tool output and evidence blobs."""

    def __init__(self, base_dir: str = ".calcifer/evidence"):
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)
        self._refs: dict[str, str] = {}  # in-memory index for this session

    def put(self, task_id: str, step_id: str, kind: str, blob: Any) -> str:
        """Store a blob, return a reference string."""
        task_dir = self.base / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Serialize the blob
        if isinstance(blob, (dict, list)):
            data = json.dumps(blob).encode()
        elif isinstance(blob, bytes):
            data = blob
        elif isinstance(blob, str):
            data = blob.encode()
        else:
            data = repr(blob).encode()

        # Write to file
        filename = f"{step_id}_{kind}.blob"
        path = task_dir / filename
        path.write_bytes(data)

        # Return reference
        ref = f"evd://{task_id}/{step_id}/{kind}"
        self._refs[ref] = str(path)
        return ref

    def get(self, ref: str) -> Optional[bytes]:
        """Retrieve a blob by reference."""
        if ref not in self._refs:
            return None
        return Path(self._refs[ref]).read_bytes()

    def get_text(self, ref: str) -> Optional[str]:
        """Retrieve a blob as text."""
        blob = self.get(ref)
        if blob is None:
            return None
        try:
            return json.loads(blob.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return blob.decode(errors="replace")

    def compress(self, ref: str, max_chars: int = 2000) -> str:
        """Get a compressed text view of evidence for feeding back to models."""
        text = self.get_text(ref)
        if text is None:
            return "[evidence not found]"

        if isinstance(text, str):
            lines = text.splitlines()
        else:
            text = json.dumps(text, indent=2)
            lines = text.splitlines()

        # Truncate
        if len(text) > max_chars:
            remaining = len(lines) - (max_chars // 40)  # rough estimate
            return "\n".join(lines[: max_chars // 40]) + f"\n…[{remaining} more lines]"

        return text
