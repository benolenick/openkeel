"""Shared database schema for the OpenKeel knowledge system.

Creates and initialises a single SQLite database at
``~/.openkeel/knowledge.db`` (separate from the existing ``memory.db``).

The database holds four first-class stores:

* **journal** — time-stamped session/milestone entries with FTS5 search.
* **wiki_pages** — persistent wiki-style knowledge pages with FTS5 search
  and an adjacency table for page-to-page links.
* **tasks** — kanban board / todo tracker with FTS5 search, subtasks,
  board grouping, and cross-links to wiki pages.
* **embeddings** — vector blobs (optional; requires an external embedding
  service to populate) associated with journal entries, wiki pages, or tasks.

Usage::

    from openkeel.integrations.knowledge_db import init_db

    conn = init_db()                         # default path
    conn = init_db("~/my-project/kb.db")    # custom path
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB: Path = Path.home() / ".openkeel" / "knowledge.db"

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA = """
-- -----------------------------------------------------------------------
-- Journal: time-ordered session entries, decisions, and milestones
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS journal (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    DEFAULT '',
    project      TEXT    DEFAULT '',
    timestamp    REAL    NOT NULL,
    entry_type   TEXT    DEFAULT 'manual',   -- manual | session_end | milestone
    title        TEXT    DEFAULT '',
    body         TEXT    NOT NULL,
    tags         TEXT    DEFAULT '',
    mission_name TEXT    DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
    title,
    body,
    project,
    tags,
    content=journal,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- Keep journal_fts in sync with journal
CREATE TRIGGER IF NOT EXISTS journal_ai AFTER INSERT ON journal BEGIN
    INSERT INTO journal_fts(rowid, title, body, project, tags)
    VALUES (new.id, new.title, new.body, new.project, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS journal_ad AFTER DELETE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, title, body, project, tags)
    VALUES ('delete', old.id, old.title, old.body, old.project, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS journal_au AFTER UPDATE ON journal BEGIN
    INSERT INTO journal_fts(journal_fts, rowid, title, body, project, tags)
    VALUES ('delete', old.id, old.title, old.body, old.project, old.tags);
    INSERT INTO journal_fts(rowid, title, body, project, tags)
    VALUES (new.id, new.title, new.body, new.project, new.tags);
END;

-- -----------------------------------------------------------------------
-- Wiki: persistent knowledge pages (concepts, how-tos, references)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wiki_pages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT    UNIQUE NOT NULL,
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    category    TEXT    DEFAULT '',
    project     TEXT    DEFAULT '',
    tags        TEXT    DEFAULT '',
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    source_type TEXT    DEFAULT 'manual'     -- manual | journal | mission
);

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    title,
    body,
    category,
    tags,
    content=wiki_pages,
    content_rowid=id,
    tokenize='porter unicode61'
);

-- Keep wiki_fts in sync with wiki_pages
CREATE TRIGGER IF NOT EXISTS wiki_ai AFTER INSERT ON wiki_pages BEGIN
    INSERT INTO wiki_fts(rowid, title, body, category, tags)
    VALUES (new.id, new.title, new.body, new.category, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS wiki_ad AFTER DELETE ON wiki_pages BEGIN
    INSERT INTO wiki_fts(wiki_fts, rowid, title, body, category, tags)
    VALUES ('delete', old.id, old.title, old.body, old.category, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS wiki_au AFTER UPDATE ON wiki_pages BEGIN
    INSERT INTO wiki_fts(wiki_fts, rowid, title, body, category, tags)
    VALUES ('delete', old.id, old.title, old.body, old.category, old.tags);
    INSERT INTO wiki_fts(rowid, title, body, category, tags)
    VALUES (new.id, new.title, new.body, new.category, new.tags);
END;

-- -----------------------------------------------------------------------
-- Wiki links: page-to-page adjacency (directed)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wiki_links (
    from_page_id INTEGER REFERENCES wiki_pages(id),
    to_page_id   INTEGER REFERENCES wiki_pages(id),
    PRIMARY KEY (from_page_id, to_page_id)
);

-- -----------------------------------------------------------------------
-- Embeddings: optional vector store for semantic search
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS embeddings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type  TEXT    NOT NULL,           -- journal | wiki | fact | task
    source_id    INTEGER NOT NULL,
    chunk_index  INTEGER DEFAULT 0,
    text_preview TEXT    DEFAULT '',
    vector       BLOB,
    created_at   REAL    NOT NULL
);

-- -----------------------------------------------------------------------
-- Tasks: kanban board / todo tracker
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL,
    description TEXT    DEFAULT '',
    status      TEXT    DEFAULT 'todo',        -- todo | in_progress | done | blocked
    priority    TEXT    DEFAULT 'medium',       -- low | medium | high | critical
    type        TEXT    DEFAULT 'task',         -- task | bug | feature | idea
    project     TEXT    DEFAULT '',
    tags        TEXT    DEFAULT '',
    assigned_to TEXT    DEFAULT '',             -- agent name or empty
    board       TEXT    DEFAULT 'default',      -- grouping (e.g. "sprint-1", "backlog")
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL,
    due_date    REAL    DEFAULT NULL,
    parent_id   INTEGER DEFAULT NULL REFERENCES tasks(id),
    sort_order  INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
    title, description, project, tags,
    content=tasks, content_rowid=id,
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS tasks_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO tasks_fts(rowid, title, description, project, tags)
    VALUES (new.id, new.title, new.description, new.project, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS tasks_ad AFTER DELETE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, project, tags)
    VALUES ('delete', old.id, old.title, old.description, old.project, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS tasks_au AFTER UPDATE ON tasks BEGIN
    INSERT INTO tasks_fts(tasks_fts, rowid, title, description, project, tags)
    VALUES ('delete', old.id, old.title, old.description, old.project, old.tags);
    INSERT INTO tasks_fts(rowid, title, description, project, tags)
    VALUES (new.id, new.title, new.description, new.project, new.tags);
END;

-- Cross-link tasks to wiki pages
CREATE TABLE IF NOT EXISTS task_links (
    task_id  INTEGER REFERENCES tasks(id),
    page_id  INTEGER REFERENCES wiki_pages(id),
    PRIMARY KEY (task_id, page_id)
);

-- -----------------------------------------------------------------------
-- Project War Rooms: persistent project state pages
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS war_rooms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT    UNIQUE NOT NULL,
    status      TEXT    DEFAULT 'active',           -- active | paused | archived
    summary     TEXT    DEFAULT '',                  -- current state summary
    blockers    TEXT    DEFAULT '',                  -- current blockers (newline separated)
    key_files   TEXT    DEFAULT '',                  -- important files (newline separated)
    decisions   TEXT    DEFAULT '',                  -- key decisions log
    notes       TEXT    DEFAULT '',                  -- freeform notes
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

-- -----------------------------------------------------------------------
-- Activity Feed: cross-session timeline of all agent work
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS activity_feed (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL    NOT NULL,
    agent       TEXT    DEFAULT '',                  -- agent name/session
    project     TEXT    DEFAULT '',
    action_type TEXT    DEFAULT 'update',            -- update | fix | deploy | research | decision | handoff
    summary     TEXT    NOT NULL,
    details     TEXT    DEFAULT '',
    task_id     INTEGER DEFAULT NULL REFERENCES tasks(id)
);

-- -----------------------------------------------------------------------
-- Handoff Packets: shift change briefings between agents
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS handoffs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT    NOT NULL,
    from_agent      TEXT    DEFAULT '',
    to_agent        TEXT    DEFAULT '',
    timestamp       REAL    NOT NULL,
    status_summary  TEXT    NOT NULL,                -- what was done
    in_progress     TEXT    DEFAULT '',              -- what's mid-flight
    blocked_on      TEXT    DEFAULT '',              -- what's blocked
    next_steps      TEXT    DEFAULT '',              -- what to do next
    files_touched   TEXT    DEFAULT '',              -- files modified
    key_decisions   TEXT    DEFAULT '',              -- decisions made
    warnings        TEXT    DEFAULT '',              -- gotchas for next agent
    picked_up       INTEGER DEFAULT 0               -- 1 when next agent reads it
);

-- -----------------------------------------------------------------------
-- Directives: queued instructions for agents
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS directives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_agent TEXT   DEFAULT '',                  -- specific agent or empty=any
    project     TEXT    DEFAULT '',
    priority    TEXT    DEFAULT 'normal',            -- normal | urgent
    message     TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    picked_up   INTEGER DEFAULT 0,
    picked_up_by TEXT   DEFAULT '',
    picked_up_at REAL   DEFAULT NULL
);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_db(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (or create) the knowledge database and return a connection.

    Parameters
    ----------
    path:
        Filesystem path to the SQLite file.  Defaults to
        ``~/.openkeel/knowledge.db``.  Parent directories are created
        automatically.

    Returns
    -------
    sqlite3.Connection
        An open connection with ``row_factory=sqlite3.Row`` set and WAL
        journal mode enabled.  The caller is responsible for closing the
        connection when finished.
    """
    db_path = Path(path).expanduser() if path else _DEFAULT_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    conn.commit()

    logger.debug("knowledge_db: opened %s", db_path)
    return conn
