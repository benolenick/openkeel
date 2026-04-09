"""Automatic knowledge retrieval for session context injection.

Searches journal, wiki, and local memory for content relevant to the
current profile + goal and returns formatted snippets suitable for
injection into CLAUDE.md.

Usage::

    from openkeel.integrations.context_retriever import retrieve_context

    knowledge = retrieve_context("cybersecurity", "enumerate web services on target", project="pentest")
    # knowledge["journal_entries"]  -> list of str
    # knowledge["wiki_pages"]       -> list of str
    # knowledge["memory_facts"]     -> list of str
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Caps to keep injected context under ~2000 chars
_MAX_JOURNAL = 3
_MAX_WIKI = 3
_MAX_FACTS = 5
_WIKI_BODY_LIMIT = 200


def retrieve_context(
    profile_name: str,
    goal_objective: str,
    project: str = "",
    top_k: int = 5,
) -> dict[str, list[str]]:
    """Retrieve relevant knowledge for a session.

    Builds a search query from *profile_name* and *goal_objective*, then
    queries the journal, wiki, and local memory backends.  Each backend
    is queried independently; failures are logged and silently skipped.

    Returns a dict with keys:

    - ``journal_entries``: formatted journal snippets (up to 3)
    - ``wiki_pages``: formatted wiki page snippets (up to 3)
    - ``memory_facts``: one-liner memory facts (up to 5)
    """
    result: dict[str, list[str]] = {
        "journal_entries": [],
        "wiki_pages": [],
        "memory_facts": [],
    }

    # Build a combined query from profile + goal
    query_parts = []
    if profile_name:
        query_parts.append(profile_name)
    if goal_objective:
        query_parts.append(goal_objective)
    query = " ".join(query_parts).strip()
    if not query:
        return result

    result["journal_entries"] = _search_journal(query, project, top_k)
    result["wiki_pages"] = _search_wiki(query, top_k)
    result["memory_facts"] = _search_memory(query, project, top_k)

    return result


def _search_journal(query: str, project: str, top_k: int) -> list[str]:
    """Search journal entries, returning formatted snippets."""
    try:
        from openkeel.integrations.journal import Journal

        journal = Journal()
        try:
            hits = journal.search_semantic(query, top_k=top_k, project=project)
            entries: list[str] = []
            seen: set[int] = set()
            for entry in hits[:_MAX_JOURNAL]:
                eid = entry.get("id")
                if eid in seen:
                    continue
                seen.add(eid)
                ts = entry.get("timestamp", 0.0)
                date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                    "%Y-%m-%d"
                )
                title = entry.get("title") or f"Entry #{eid}"
                body = (entry.get("body") or "").strip()
                # Truncate body to keep context tight
                if len(body) > 200:
                    body = body[:200] + "..."
                entries.append(f"**{title}** ({date_str}): {body}")
            return entries
        finally:
            journal.close()
    except Exception as exc:
        logger.debug("context_retriever: journal search failed: %s", exc)
        return []


def _search_wiki(query: str, top_k: int) -> list[str]:
    """Search wiki pages, returning title + truncated body."""
    try:
        from openkeel.integrations.wiki import Wiki

        wiki = Wiki()
        try:
            hits = wiki.search_semantic(query, top_k=top_k)
            pages: list[str] = []
            seen: set[int] = set()
            for page in hits[:_MAX_WIKI]:
                pid = page.get("id")
                if pid in seen:
                    continue
                seen.add(pid)
                title = page.get("title", "Untitled")
                category = page.get("category", "")
                body = (page.get("body") or "").strip()
                if len(body) > _WIKI_BODY_LIMIT:
                    body = body[:_WIKI_BODY_LIMIT] + "..."
                label = f"**{title}**"
                if category:
                    label += f" ({category})"
                pages.append(f"{label}: {body}")
            return pages
        finally:
            wiki.close()
    except Exception as exc:
        logger.debug("context_retriever: wiki search failed: %s", exc)
        return []


def _search_memory(query: str, project: str, top_k: int) -> list[str]:
    """Search local memory facts, returning one-liners."""
    try:
        from openkeel.integrations.local_memory import LocalMemory

        mem = LocalMemory()
        try:
            hits = mem.recall(query, top_k=top_k, project=project)
            facts: list[str] = []
            seen: set[str] = set()
            for hit in hits[:_MAX_FACTS]:
                text = (hit.get("text") or "").strip()
                if not text or text in seen:
                    continue
                seen.add(text)
                # Cap individual facts at 150 chars
                if len(text) > 150:
                    text = text[:150] + "..."
                facts.append(text)
            return facts
        finally:
            mem.close()
    except Exception as exc:
        logger.debug("context_retriever: memory search failed: %s", exc)
        return []


def format_knowledge_block(knowledge: dict[str, list[str]]) -> str:
    """Format retrieved knowledge into a markdown section for CLAUDE.md.

    Returns an empty string if no knowledge was retrieved, so the caller
    can skip the section entirely.
    """
    journal = knowledge.get("journal_entries", [])
    wiki = knowledge.get("wiki_pages", [])
    facts = knowledge.get("memory_facts", [])

    if not journal and not wiki and not facts:
        return ""

    sections: list[str] = ["## Relevant Knowledge"]

    if journal:
        sections.append("")
        sections.append("### From Journal")
        for entry in journal:
            sections.append(f"- {entry}")

    if wiki:
        sections.append("")
        sections.append("### Reference Pages")
        for page in wiki:
            sections.append(f"- {page}")

    if facts:
        sections.append("")
        sections.append("### Key Facts")
        for fact in facts:
            sections.append(f"- {fact}")

    return "\n".join(sections)
