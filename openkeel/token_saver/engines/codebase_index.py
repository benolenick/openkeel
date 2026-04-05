"""Codebase Index — lightweight AST-level index of the project.

Parses Python/JS/TS/Go/Rust files to extract:
  - Classes and their methods
  - Top-level functions with signatures
  - Import/dependency graph
  - File purpose summaries

Maintains a JSON index at ~/.openkeel/token_saver_codebase/{project_hash}.json
Updated incrementally on file mtime changes.

When Claude searches or reads, the index answers first — saving full file reads.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

INDEX_DIR = Path.home() / ".openkeel" / "token_saver_codebase"

# File extensions we index
INDEXABLE = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".sh"}

# Directories to skip
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs",
    "unsloth_compiled_cache", ".tmp", "SadTalker", "TalkingHead",
    "gfpgan", "mirror_assets",
}

# Max file size to index (100KB)
MAX_FILE_SIZE = 100_000


def _project_hash(project_root: str) -> str:
    return hashlib.sha256(project_root.encode()).hexdigest()[:12]


def _should_index(path: Path) -> bool:
    if path.suffix not in INDEXABLE:
        return False
    if path.stat().st_size > MAX_FILE_SIZE:
        return False
    for part in path.parts:
        if part in SKIP_DIRS:
            return False
    return True


# ---------------------------------------------------------------------------
# Python parser (AST-based, most accurate)
# ---------------------------------------------------------------------------

def _parse_python(file_path: Path, content: str) -> dict[str, Any]:
    """Extract structure from a Python file using AST."""
    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError:
        return _parse_generic(file_path, content)

    classes = []
    functions = []
    imports = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            methods = []
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in item.args.args if a.arg != "self"]
                    methods.append({
                        "name": item.name,
                        "args": args[:6],
                        "line": item.lineno,
                        "async": isinstance(item, ast.AsyncFunctionDef),
                    })
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "bases": [_name_of(b) for b in node.bases],
                "methods": methods,
                "docstring": ast.get_docstring(node) or "",
            })

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            functions.append({
                "name": node.name,
                "args": args[:8],
                "line": node.lineno,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "docstring": (ast.get_docstring(node) or "")[:120],
            })

        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.append(module)

    docstring = ast.get_docstring(tree) or ""
    return {
        "classes": classes,
        "functions": functions,
        "imports": imports[:20],
        "docstring": docstring[:200],
    }


def _name_of(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name_of(node.value)}.{node.attr}"
    return "?"


# ---------------------------------------------------------------------------
# Generic parser (regex-based, for JS/TS/Go/Rust/etc.)
# ---------------------------------------------------------------------------

_CLASS_RE = re.compile(r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)
_FUNC_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function\s+|def\s+|fn\s+|func\s+)(\w+)\s*\(([^)]{0,200})\)",
    re.MULTILINE,
)
_IMPORT_RE = re.compile(
    r"^(?:import|from|require|use)\s+[\"']?([^\s\"';]+)",
    re.MULTILINE,
)


def _parse_generic(file_path: Path, content: str) -> dict[str, Any]:
    """Regex-based extraction for non-Python files."""
    classes = []
    for m in _CLASS_RE.finditer(content):
        line = content[:m.start()].count("\n") + 1
        classes.append({"name": m.group(1), "line": line, "methods": [], "bases": []})

    functions = []
    for m in _FUNC_RE.finditer(content):
        line = content[:m.start()].count("\n") + 1
        args = [a.strip().split(":")[0].split(" ")[-1] for a in m.group(2).split(",") if a.strip()]
        functions.append({
            "name": m.group(1),
            "args": args[:8],
            "line": line,
            "async": "async" in content[max(0, m.start() - 10):m.start()],
        })

    imports = [m.group(1) for m in _IMPORT_RE.finditer(content)]

    # First comment block as docstring
    docstring = ""
    lines = content.split("\n")[:5]
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("#", "//", "/*", "/**", "*")):
            docstring += stripped.lstrip("#/ *") + " "
        elif stripped.startswith('"""') or stripped.startswith("'''"):
            docstring += stripped.strip("\"'") + " "

    return {
        "classes": classes[:20],
        "functions": functions[:40],
        "imports": imports[:20],
        "docstring": docstring[:200].strip(),
    }


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def build_index(project_root: str, force: bool = False) -> dict[str, Any]:
    """Build or update the codebase index for a project."""
    root = Path(project_root).resolve()
    phash = _project_hash(str(root))
    index_path = INDEX_DIR / f"{phash}.json"
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing index
    existing: dict[str, Any] = {}
    if not force and index_path.exists():
        try:
            existing = json.loads(index_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    files_index = existing.get("files", {})
    updated = 0
    removed = 0

    # Scan all indexable files
    current_files = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if not _should_index(path):
            continue

        rel = str(path.relative_to(root))
        current_files.add(rel)

        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        # Skip if unchanged
        if rel in files_index and files_index[rel].get("mtime") == mtime:
            continue

        # Parse
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        line_count = content.count("\n") + 1

        if path.suffix == ".py":
            structure = _parse_python(path, content)
        else:
            structure = _parse_generic(path, content)

        files_index[rel] = {
            "mtime": mtime,
            "lines": line_count,
            "size": len(content),
            **structure,
        }
        updated += 1

    # Remove deleted files
    for rel in list(files_index.keys()):
        if rel not in current_files:
            del files_index[rel]
            removed += 1

    index = {
        "project_root": str(root),
        "project_hash": phash,
        "updated_at": time.time(),
        "file_count": len(files_index),
        "files": files_index,
    }

    index_path.write_text(json.dumps(index, indent=2))

    return {
        "file_count": len(files_index),
        "updated": updated,
        "removed": removed,
        "index_path": str(index_path),
    }


def load_index(project_root: str) -> dict[str, Any] | None:
    """Load existing index for a project."""
    root = Path(project_root).resolve()
    phash = _project_hash(str(root))
    index_path = INDEX_DIR / f"{phash}.json"
    if not index_path.exists():
        return None
    try:
        return json.loads(index_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def query_index(project_root: str, query: str, limit: int = 10) -> list[dict]:
    """Search the index for functions, classes, or files matching a query."""
    index = load_index(project_root)
    if not index:
        return []

    query_lower = query.lower()
    results = []

    for rel, info in index.get("files", {}).items():
        score = 0

        # Match file name
        if query_lower in rel.lower():
            score += 10

        # Match class names
        for cls in info.get("classes", []):
            if query_lower in cls.get("name", "").lower():
                score += 8
                results.append({
                    "type": "class",
                    "name": cls["name"],
                    "file": rel,
                    "line": cls.get("line", 0),
                    "methods": [m["name"] for m in cls.get("methods", [])[:5]],
                    "score": score,
                })

        # Match function names
        for func in info.get("functions", []):
            if query_lower in func.get("name", "").lower():
                score += 6
                results.append({
                    "type": "function",
                    "name": func["name"],
                    "file": rel,
                    "line": func.get("line", 0),
                    "args": func.get("args", []),
                    "score": score,
                })

        # Match imports
        for imp in info.get("imports", []):
            if query_lower in imp.lower():
                score += 2

        # Match docstring
        if query_lower in info.get("docstring", "").lower():
            score += 4

        if score > 0 and not any(r.get("file") == rel for r in results):
            results.append({
                "type": "file",
                "name": rel,
                "file": rel,
                "line": 0,
                "score": score,
            })

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


def format_index_summary(project_root: str) -> str:
    """Format a compact project map for Claude's context."""
    index = load_index(project_root)
    if not index:
        return ""

    lines = []
    files = index.get("files", {})

    # Group by directory
    dirs: dict[str, list] = {}
    for rel, info in sorted(files.items()):
        dir_name = str(Path(rel).parent) if "/" in rel else "."
        dirs.setdefault(dir_name, []).append((rel, info))

    for dir_name, file_list in sorted(dirs.items()):
        lines.append(f"\n{dir_name}/")
        for rel, info in file_list:
            fname = Path(rel).name
            classes = [c["name"] for c in info.get("classes", [])]
            funcs = [f["name"] for f in info.get("functions", [])[:5]]
            parts = []
            if classes:
                parts.append(f"classes: {', '.join(classes[:3])}")
            if funcs:
                parts.append(f"fn: {', '.join(funcs[:4])}")
            detail = f" — {'; '.join(parts)}" if parts else ""
            lines.append(f"  {fname} ({info.get('lines', 0)}L){detail}")

    return "\n".join(lines)
