"""Kanban board and task tracker for OpenKeel.

Stores tasks in SQLite with FTS5 search, subtask hierarchy, board grouping,
and cross-links to wiki pages. Follows the same patterns as Journal and Wiki.

Usage::

    from openkeel.integrations.kanban import Kanban

    kb = Kanban()
    tid = kb.add_task("Fix login flow", priority="high", project="myproject", type="bug")
    kb.move(tid, "in_progress")
    kb.assign(tid, "autopwn-agent")
    view = kb.board_view(project="myproject")
    print(kb.get_task_summary(project="myproject"))
    kb.close()
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from openkeel.integrations.knowledge_db import init_db

logger = logging.getLogger(__name__)

_EMBEDDINGS_BASE = "http://localhost:7437"

_VALID_STATUSES = {"todo", "in_progress", "done", "blocked"}
_VALID_PRIORITIES = {"low", "medium", "high", "critical"}
_VALID_TYPES = {"task", "bug", "feature", "idea"}


class Kanban:
    """SQLite + FTS5 kanban board with optional semantic search."""

    def __init__(self, db_path: str | None = None) -> None:
        self._conn: sqlite3.Connection = init_db(db_path)
        logger.debug("Kanban: opened knowledge DB")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        try:
            self._conn.close()
            logger.debug("Kanban: closed connection")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_task(
        self,
        title: str,
        description: str = "",
        status: str = "todo",
        priority: str = "medium",
        type: str = "task",
        project: str = "",
        tags: str = "",
        assigned_to: str = "",
        board: str = "default",
        due_date: float | None = None,
        parent_id: int | None = None,
    ) -> int:
        """Create a task and return its row ID."""
        now = time.time()
        # Get next sort_order for this board+status
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order "
            "FROM tasks WHERE board = ? AND status = ?",
            (board, status),
        ).fetchone()
        sort_order = row["next_order"] if row else 0

        cur = self._conn.execute(
            """
            INSERT INTO tasks
                (title, description, status, priority, type, project, tags,
                 assigned_to, board, created_at, updated_at, due_date,
                 parent_id, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, description, status, priority, type, project, tags,
             assigned_to, board, now, now, due_date, parent_id, sort_order),
        )
        self._conn.commit()
        task_id: int = cur.lastrowid  # type: ignore[assignment]
        logger.info("Kanban: created task #%d %r (status=%s, project=%r)", task_id, title, status, project)
        self._index_async(task_id, f"{title} {description}")
        return task_id

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        """Return full task details including subtasks and linked wiki pages."""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None

        task: dict[str, Any] = dict(row)

        # Subtasks
        subtasks = self._conn.execute(
            "SELECT id, title, status, priority, type, assigned_to "
            "FROM tasks WHERE parent_id = ? ORDER BY sort_order, id",
            (task_id,),
        ).fetchall()
        task["subtasks"] = [dict(s) for s in subtasks]

        # Linked wiki pages
        links = self._conn.execute(
            """
            SELECT wp.id, wp.slug, wp.title, wp.category
            FROM task_links tl
            JOIN wiki_pages wp ON wp.id = tl.page_id
            WHERE tl.task_id = ?
            """,
            (task_id,),
        ).fetchall()
        task["wiki_links"] = [dict(l) for l in links]

        return task

    def update_task(self, task_id: int, **fields: Any) -> bool:
        """Update any combination of task fields. Returns True if updated."""
        allowed = {
            "title", "description", "status", "priority", "type", "project",
            "tags", "assigned_to", "board", "due_date", "parent_id", "sort_order",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False

        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [task_id]

        cur = self._conn.execute(
            f"UPDATE tasks SET {set_clause} WHERE id = ?", params
        )
        self._conn.commit()
        if cur.rowcount == 0:
            return False
        logger.info("Kanban: updated task #%d fields=%s", task_id, list(updates.keys()))
        return True

    def delete_task(self, task_id: int) -> bool:
        """Delete a task and its cross-links. Returns True if deleted."""
        self._conn.execute("DELETE FROM task_links WHERE task_id = ?", (task_id,))
        # Unparent any subtasks
        self._conn.execute("UPDATE tasks SET parent_id = NULL WHERE parent_id = ?", (task_id,))
        cur = self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()
        if cur.rowcount == 0:
            return False
        logger.info("Kanban: deleted task #%d", task_id)
        return True

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def move(self, task_id: int, new_status: str) -> bool:
        """Change task status. Returns True if updated."""
        if new_status not in _VALID_STATUSES:
            logger.warning("Kanban: invalid status %r", new_status)
            return False
        return self.update_task(task_id, status=new_status)

    def assign(self, task_id: int, agent_name: str) -> bool:
        """Assign task to an agent. Returns True if updated."""
        return self.update_task(task_id, assigned_to=agent_name)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_tasks(
        self,
        status: str = "",
        project: str = "",
        board: str = "",
        type: str = "",
        assigned_to: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return filtered task list ordered by priority then sort_order."""
        clauses: list[str] = []
        params: list[Any] = []

        if status:
            clauses.append("status = ?")
            params.append(status)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if board:
            clauses.append("board = ?")
            params.append(board)
        if type:
            clauses.append("type = ?")
            params.append(type)
        if assigned_to:
            clauses.append("assigned_to = ?")
            params.append(assigned_to)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        sql = f"""
            SELECT id, title, status, priority, type, project, tags,
                   assigned_to, board, created_at, updated_at, due_date,
                   parent_id, sort_order
            FROM tasks
            {where}
            ORDER BY
                CASE priority
                    WHEN 'critical' THEN 0
                    WHEN 'high'     THEN 1
                    WHEN 'medium'   THEN 2
                    WHEN 'low'      THEN 3
                END,
                sort_order, id
            LIMIT ?
        """
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def list_boards(self, project: str = "") -> list[dict[str, Any]]:
        """Return board summaries: [{board, count, statuses}]."""
        if project:
            sql = """
                SELECT board, COUNT(*) AS count,
                       GROUP_CONCAT(DISTINCT status) AS statuses
                FROM tasks WHERE project = ?
                GROUP BY board ORDER BY board
            """
            rows = self._conn.execute(sql, (project,)).fetchall()
        else:
            sql = """
                SELECT board, COUNT(*) AS count,
                       GROUP_CONCAT(DISTINCT status) AS statuses
                FROM tasks
                GROUP BY board ORDER BY board
            """
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def board_view(self, project: str = "", board: str = "") -> dict[str, list[dict[str, Any]]]:
        """Return tasks grouped by status for board display."""
        result: dict[str, list[dict[str, Any]]] = {
            "todo": [], "in_progress": [], "done": [], "blocked": [],
        }
        tasks = self.list_tasks(project=project, board=board, limit=200)
        for t in tasks:
            status = t.get("status", "todo")
            if status in result:
                result[status].append(t)
        return result

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search_keyword(
        self,
        query: str,
        top_k: int = 10,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """FTS5 BM25 keyword search over tasks."""
        fts_query = " OR ".join(
            f'"{w}"' for w in query.split() if w.strip()
        )
        if not fts_query:
            return []

        if project:
            sql = """
                SELECT t.*, rank AS score
                FROM tasks_fts fts
                JOIN tasks t ON t.id = fts.rowid
                WHERE tasks_fts MATCH ?
                  AND t.project = ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self._conn.execute(sql, (fts_query, project, top_k)).fetchall()
        else:
            sql = """
                SELECT t.*, rank AS score
                FROM tasks_fts fts
                JOIN tasks t ON t.id = fts.rowid
                WHERE tasks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            rows = self._conn.execute(sql, (fts_query, top_k)).fetchall()

        return [{**dict(r), "score": abs(r["score"])} for r in rows]

    def search_semantic(
        self,
        query: str,
        top_k: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Semantic search via embeddings, falling back to keyword search."""
        try:
            from openkeel.integrations.embeddings_client import EmbeddingsClient

            client = EmbeddingsClient(base_url=_EMBEDDINGS_BASE)
            hits = client.search(query, top_k=top_k, source_types=["task"])
            if hits:
                tasks: list[dict[str, Any]] = []
                seen: set[int] = set()
                for hit in hits:
                    src_id = hit.get("source_id")
                    if src_id is None or src_id in seen:
                        continue
                    seen.add(src_id)
                    row = self._conn.execute(
                        "SELECT * FROM tasks WHERE id = ?", (src_id,)
                    ).fetchone()
                    if row:
                        task = dict(row)
                        task["score"] = hit.get("score", 0)
                        tasks.append(task)
                if tasks:
                    return tasks
        except Exception as exc:
            logger.debug("Kanban: semantic search unavailable (%s), falling back", exc)

        return self.search_keyword(query, top_k=top_k, project=project)

    # ------------------------------------------------------------------
    # Cross-links
    # ------------------------------------------------------------------

    def link_wiki(self, task_id: int, wiki_slug: str) -> bool:
        """Link a task to a wiki page by slug. Returns True on success."""
        page = self._conn.execute(
            "SELECT id FROM wiki_pages WHERE slug = ?", (wiki_slug,)
        ).fetchone()
        if not page:
            logger.warning("Kanban: wiki page %r not found", wiki_slug)
            return False

        task = self._conn.execute(
            "SELECT id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not task:
            logger.warning("Kanban: task #%d not found", task_id)
            return False

        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO task_links (task_id, page_id) VALUES (?, ?)",
                (task_id, page["id"]),
            )
            self._conn.commit()
            logger.info("Kanban: linked task #%d → wiki %r", task_id, wiki_slug)
            return True
        except sqlite3.Error as exc:
            logger.warning("Kanban: link_wiki error: %s", exc)
            return False

    def from_journal(
        self,
        journal_id: int,
        title: str = "",
        priority: str = "medium",
        project: str = "",
    ) -> int:
        """Promote a journal entry to a task. Returns the new task ID."""
        row = self._conn.execute(
            "SELECT * FROM journal WHERE id = ?", (journal_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Journal entry {journal_id!r} not found")

        task_title = title or row["title"] or f"From journal #{journal_id}"
        task_project = project or row["project"] or ""
        tags = row["tags"] or ""

        return self.add_task(
            title=task_title,
            description=row["body"],
            priority=priority,
            project=task_project,
            tags=tags,
        )

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def get_task_summary(self, project: str = "", board: str = "") -> str:
        """Return a formatted summary string for context injection."""
        view = self.board_view(project=project, board=board)
        total = sum(len(v) for v in view.values())
        if total == 0:
            return ""

        lines = ["## Active Tasks"]

        for status_label, status_key in [
            ("TODO", "todo"),
            ("IN PROGRESS", "in_progress"),
            ("BLOCKED", "blocked"),
        ]:
            tasks = view[status_key]
            if not tasks:
                continue
            lines.append(f"\n### {status_label}")
            for t in tasks[:5]:  # cap at 5 per column for context size
                pri = _priority_badge(t.get("priority", "medium"))
                type_badge = _type_badge(t.get("type", "task"))
                assignee = f" @{t['assigned_to']}" if t.get("assigned_to") else ""
                lines.append(f"- #{t['id']} {t['title']}{pri}{type_badge}{assignee}")

        counts = {k: len(v) for k, v in view.items()}
        lines.append(
            f"\n{total} tasks ({counts['todo']} todo, {counts['in_progress']} active, "
            f"{counts['done']} done, {counts['blocked']} blocked)"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self, project: str = "") -> dict[str, Any]:
        """Return task statistics breakdown."""
        where = "WHERE project = ?" if project else ""
        params: tuple = (project,) if project else ()

        total = self._conn.execute(
            f"SELECT COUNT(*) AS c FROM tasks {where}", params
        ).fetchone()["c"]

        by_status = {}
        for row in self._conn.execute(
            f"SELECT status, COUNT(*) AS c FROM tasks {where} GROUP BY status", params
        ).fetchall():
            by_status[row["status"]] = row["c"]

        by_type = {}
        for row in self._conn.execute(
            f"SELECT type, COUNT(*) AS c FROM tasks {where} GROUP BY type", params
        ).fetchall():
            by_type[row["type"]] = row["c"]

        by_priority = {}
        for row in self._conn.execute(
            f"SELECT priority, COUNT(*) AS c FROM tasks {where} GROUP BY priority", params
        ).fetchall():
            by_priority[row["priority"]] = row["c"]

        by_assignee = {}
        for row in self._conn.execute(
            f"SELECT assigned_to, COUNT(*) AS c FROM tasks {where} "
            "AND assigned_to != '' GROUP BY assigned_to" if project else
            "SELECT assigned_to, COUNT(*) AS c FROM tasks "
            "WHERE assigned_to != '' GROUP BY assigned_to",
            params,
        ).fetchall():
            by_assignee[row["assigned_to"]] = row["c"]

        return {
            "total": total,
            "by_status": by_status,
            "by_type": by_type,
            "by_priority": by_priority,
            "by_assignee": by_assignee,
        }

    # ------------------------------------------------------------------
    # Roadmaps
    # ------------------------------------------------------------------

    def create_roadmap(self, project: str, title: str, description: str = "") -> int:
        """Create a new project roadmap."""
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO roadmaps (project, title, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            (project, title, description, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_roadmap(self, roadmap_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM roadmaps WHERE id = ?", (roadmap_id,)).fetchone()
        return dict(row) if row else None

    def list_roadmaps(self, project: str = "") -> list[dict[str, Any]]:
        if project:
            rows = self._conn.execute(
                "SELECT * FROM roadmaps WHERE project = ? ORDER BY created_at DESC", (project,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM roadmaps ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_roadmap(self, roadmap_id: int, **kwargs) -> bool:
        allowed = {"title", "description", "status"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self._conn.execute(
            f"UPDATE roadmaps SET {set_clause} WHERE id = ?",
            list(updates.values()) + [roadmap_id],
        )
        self._conn.commit()
        return True

    def delete_roadmap(self, roadmap_id: int) -> bool:
        # Delete linked milestone_tasks first, then milestones, then roadmap
        milestone_ids = [r["id"] for r in self._conn.execute(
            "SELECT id FROM milestones WHERE roadmap_id = ?", (roadmap_id,)
        ).fetchall()]
        for mid in milestone_ids:
            self._conn.execute("DELETE FROM milestone_tasks WHERE milestone_id = ?", (mid,))
        self._conn.execute("DELETE FROM milestones WHERE roadmap_id = ?", (roadmap_id,))
        self._conn.execute("DELETE FROM roadmaps WHERE id = ?", (roadmap_id,))
        self._conn.commit()
        return True

    def add_milestone(self, roadmap_id: int, title: str, description: str = "",
                      target_date: float | None = None, sort_order: int = 0) -> int:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO milestones (roadmap_id, title, description, status, target_date, "
            "sort_order, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
            (roadmap_id, title, description, target_date, sort_order, now, now),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_milestone(self, milestone_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
        return dict(row) if row else None

    def update_milestone(self, milestone_id: int, **kwargs) -> bool:
        allowed = {"title", "description", "status", "target_date", "sort_order"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        updates["updated_at"] = time.time()
        if updates.get("status") == "completed":
            updates["completed_at"] = time.time()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        self._conn.execute(
            f"UPDATE milestones SET {set_clause} WHERE id = ?",
            list(updates.values()) + [milestone_id],
        )
        self._conn.commit()
        return True

    def delete_milestone(self, milestone_id: int) -> bool:
        self._conn.execute("DELETE FROM milestone_tasks WHERE milestone_id = ?", (milestone_id,))
        self._conn.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
        self._conn.commit()
        return True

    def link_task_to_milestone(self, milestone_id: int, task_id: int) -> bool:
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO milestone_tasks (milestone_id, task_id) VALUES (?, ?)",
                (milestone_id, task_id),
            )
            self._conn.commit()
            return True
        except Exception:
            return False

    def unlink_task_from_milestone(self, milestone_id: int, task_id: int) -> bool:
        self._conn.execute(
            "DELETE FROM milestone_tasks WHERE milestone_id = ? AND task_id = ?",
            (milestone_id, task_id),
        )
        self._conn.commit()
        return True

    def roadmap_view(self, roadmap_id: int) -> dict[str, Any]:
        """Full roadmap view: roadmap + milestones + linked tasks with progress."""
        roadmap = self.get_roadmap(roadmap_id)
        if not roadmap:
            return {"error": "Roadmap not found"}

        milestones = self._conn.execute(
            "SELECT * FROM milestones WHERE roadmap_id = ? ORDER BY sort_order, target_date",
            (roadmap_id,),
        ).fetchall()

        result_milestones = []
        total_tasks = 0
        total_done = 0

        for ms in milestones:
            ms_dict = dict(ms)
            # Get linked tasks
            task_rows = self._conn.execute(
                "SELECT t.* FROM tasks t "
                "JOIN milestone_tasks mt ON mt.task_id = t.id "
                "WHERE mt.milestone_id = ? ORDER BY t.sort_order",
                (ms["id"],),
            ).fetchall()
            tasks = [dict(t) for t in task_rows]
            done = sum(1 for t in tasks if t["status"] == "done")
            total_tasks += len(tasks)
            total_done += done

            ms_dict["tasks"] = tasks
            ms_dict["task_count"] = len(tasks)
            ms_dict["done_count"] = done
            ms_dict["progress"] = round(done / len(tasks) * 100) if tasks else 0
            result_milestones.append(ms_dict)

        roadmap["milestones"] = result_milestones
        roadmap["total_tasks"] = total_tasks
        roadmap["total_done"] = total_done
        roadmap["progress"] = round(total_done / total_tasks * 100) if total_tasks else 0
        return roadmap

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _index_async(self, task_id: int, text: str) -> None:
        """Fire-and-forget POST to the embeddings service."""
        try:
            payload = json.dumps(
                {"source_type": "task", "source_id": task_id, "text": text}
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{_EMBEDDINGS_BASE}/index",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                logger.debug("Kanban: indexed task #%d (status %s)", task_id, resp.status)
        except Exception:
            logger.debug("Kanban: embeddings indexing skipped for task #%d", task_id)


# ---------------------------------------------------------------------------
# Module-level helpers (used by CLI board rendering)
# ---------------------------------------------------------------------------


def _priority_badge(priority: str) -> str:
    """Return short priority indicator for display."""
    return {"critical": " [C]", "high": " [H]", "medium": " [M]", "low": ""}.get(priority, "")


def _type_badge(task_type: str) -> str:
    """Return type indicator for non-task types."""
    if task_type == "task":
        return ""
    return f" ({task_type})"
