"""Microsoft Answers forum scraper for LLMOS UX Research.

Scrapes support threads from answers.microsoft.com — Microsoft's official
community support forum. These threads are gold for UX research because
users describe exact workflows that failed.

Categories: Windows 10, Windows 11, Surface, OneDrive, Microsoft 365, Edge

Uses Bing site search to find threads (MS Answers has no public API),
then scrapes each thread page for question + answers.

Usage:
    python ms_answers_scraper.py scrape                # scrape all categories
    python ms_answers_scraper.py scrape --category windows-11
    python ms_answers_scraper.py stats
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("ux_researcher.ms_answers")

DB_PATH = Path.home() / ".openkeel" / "ux_research.db"
RATE_LIMIT = 3.0  # be gentle with MS servers
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

# MS Answers categories mapped to search terms
CATEGORIES = {
    "windows-11": [
        "windows 11 problem", "windows 11 not working", "windows 11 update broke",
        "windows 11 slow", "windows 11 crash", "windows 11 settings missing",
        "windows 11 driver issue", "windows 11 taskbar", "windows 11 start menu",
        "windows 11 file explorer", "windows 11 blue screen",
    ],
    "windows-10": [
        "windows 10 problem", "windows 10 update issue", "windows 10 slow",
        "windows 10 not responding", "windows 10 printer not working",
        "windows 10 wifi keeps disconnecting",
    ],
    "surface": [
        "surface pro problem", "surface laptop issue", "surface not charging",
        "surface screen flickering",
    ],
    "onedrive": [
        "onedrive sync problem", "onedrive not syncing", "onedrive taking space",
        "onedrive keeps asking to sign in",
    ],
    "edge": [
        "microsoft edge problem", "edge keeps crashing", "edge too much memory",
        "edge default browser keeps changing",
    ],
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ms_threads (
            id              TEXT PRIMARY KEY,
            url             TEXT NOT NULL,
            category        TEXT DEFAULT '',
            title           TEXT NOT NULL,
            question_body   TEXT DEFAULT '',
            question_author TEXT DEFAULT '',
            answer_body     TEXT DEFAULT '',
            answer_author   TEXT DEFAULT '',
            is_answered     INTEGER DEFAULT 0,
            view_count      INTEGER DEFAULT 0,
            vote_count      INTEGER DEFAULT 0,
            created_date    TEXT DEFAULT '',
            scraped_at      REAL NOT NULL,
            pushed_to_hyphae INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_ms_cat ON ms_threads(category);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# HTML parsing (lightweight, no BeautifulSoup dependency)
# ---------------------------------------------------------------------------

class MSAnswersParser(HTMLParser):
    """Extract question and answer from an MS Answers thread page."""

    def __init__(self):
        super().__init__()
        self._capture = False
        self._current_tag = ""
        self._current_class = ""
        self._depth = 0
        self.title = ""
        self.question = ""
        self.answer = ""
        self.author = ""
        self.views = 0
        self.votes = 0
        self._in_question = False
        self._in_answer = False
        self._text_buffer = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")

        if tag == "h1" and "thread-title" in cls:
            self._capture = True
            self._current_tag = "title"
            self._text_buffer = []

        elif "question-body" in cls or "thread-message-content-body-text" in cls:
            if not self._in_answer:
                self._in_question = True
                self._capture = True
                self._current_tag = "question"
                self._text_buffer = []

        elif "answer-body" in cls or "accepted-answer" in cls:
            self._in_answer = True
            self._capture = True
            self._current_tag = "answer"
            self._text_buffer = []

        elif "vote-count" in cls:
            self._capture = True
            self._current_tag = "votes"
            self._text_buffer = []

    def handle_endtag(self, tag):
        if self._capture and tag in ("h1", "div", "span"):
            text = " ".join(self._text_buffer).strip()
            if self._current_tag == "title" and not self.title:
                self.title = text
            elif self._current_tag == "question" and not self.question:
                self.question = text[:5000]
                self._in_question = False
            elif self._current_tag == "answer" and not self.answer:
                self.answer = text[:5000]
            elif self._current_tag == "votes":
                try:
                    self.votes = int(text.replace(",", ""))
                except ValueError:
                    pass
            self._capture = False
            self._text_buffer = []

    def handle_data(self, data):
        if self._capture:
            self._text_buffer.append(data.strip())


class SearchResultParser(HTMLParser):
    """Extract URLs from Bing search results."""

    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if "answers.microsoft.com" in href and "/thread/" in href:
                # Clean URL
                if href.startswith("http"):
                    url = href.split("&")[0]  # strip tracking params
                    if url not in self.urls:
                        self.urls.append(url)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _fetch_page(url: str) -> str:
    """Fetch a page with rate limiting."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            time.sleep(RATE_LIMIT)
            return data
    except Exception as e:
        log.error("Fetch failed: %s — %s", url, e)
        time.sleep(RATE_LIMIT)
        return ""


def find_thread_urls(query: str, num_results: int = 20) -> list[str]:
    """Search DuckDuckGo for MS Answers threads matching a query."""
    encoded = urllib.parse.quote(f"site:answers.microsoft.com {query}")
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    html = _fetch_page(url)
    if not html:
        return []

    urls = []
    # Parse DDG results
    parser = SearchResultParser()
    parser.feed(html)
    urls.extend(parser.urls)

    # Also regex extract MS Answers URLs from the full HTML
    extra = re.findall(r'https?://answers\.microsoft\.com/[^\s"<>]+/thread/[a-f0-9-]+', html)
    for u in extra:
        clean = u.split("&")[0].split("?")[0]
        if clean not in urls:
            urls.append(clean)

    return urls[:num_results]


def scrape_thread(url: str) -> dict:
    """Scrape a single MS Answers thread."""
    html = _fetch_page(url)
    if not html:
        return {}

    parser = MSAnswersParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    if not parser.title and not parser.question:
        return {}

    # Extract thread ID from URL
    thread_match = re.search(r'/thread/([a-f0-9-]+)', url)
    thread_id = thread_match.group(1) if thread_match else url

    return {
        "id": f"ms_{thread_id[:36]}",
        "url": url,
        "title": parser.title,
        "question_body": parser.question,
        "answer_body": parser.answer,
        "is_answered": 1 if parser.answer else 0,
        "vote_count": parser.votes,
    }


def scrape_category(conn: sqlite3.Connection, category: str,
                    queries: list[str], threads_per_query: int = 10) -> int:
    """Scrape threads for a category."""
    ensure_tables(conn)
    saved = 0

    for query in queries:
        log.info("Searching: %s", query)
        urls = find_thread_urls(query, num_results=threads_per_query)
        log.info("  Found %d thread URLs", len(urls))

        for url in urls:
            # Skip if already scraped
            existing = conn.execute("SELECT id FROM ms_threads WHERE url = ?", (url,)).fetchone()
            if existing:
                continue

            thread = scrape_thread(url)
            if not thread or not thread.get("title"):
                continue

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO ms_threads
                       (id, url, category, title, question_body, answer_body,
                        is_answered, vote_count, scraped_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        thread["id"], thread["url"], category,
                        thread["title"], thread.get("question_body", ""),
                        thread.get("answer_body", ""),
                        thread.get("is_answered", 0),
                        thread.get("vote_count", 0),
                        time.time(),
                    ),
                )
                saved += 1
            except sqlite3.IntegrityError:
                pass

        conn.commit()
        log.info("  Saved %d new threads for '%s'", saved, query)

    return saved


def scrape_all(conn: sqlite3.Connection, threads_per_query: int = 10) -> int:
    """Scrape all categories."""
    total = 0
    for category, queries in CATEGORIES.items():
        log.info("=== Category: %s (%d queries) ===", category, len(queries))
        saved = scrape_category(conn, category, queries, threads_per_query)
        total += saved
        log.info("Category %s: %d new threads", category, saved)
    return total


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(conn: sqlite3.Connection):
    ensure_tables(conn)
    total = conn.execute("SELECT COUNT(*) FROM ms_threads").fetchone()[0]
    answered = conn.execute("SELECT COUNT(*) FROM ms_threads WHERE is_answered = 1").fetchone()[0]
    print(f"MS Answers threads: {total:,} total, {answered:,} answered")
    print()

    print("By category:")
    for row in conn.execute(
        "SELECT category, COUNT(*) as cnt FROM ms_threads GROUP BY category ORDER BY cnt DESC"
    ).fetchall():
        print(f"  {row['category']:<20} {row['cnt']:>5}")

    print("\nSample threads:")
    for row in conn.execute(
        "SELECT category, title, vote_count FROM ms_threads ORDER BY vote_count DESC LIMIT 10"
    ).fetchall():
        print(f"  [{row['category']}] {row['title'][:70]}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "scrape":
        if "--category" in sys.argv:
            idx = sys.argv.index("--category")
            cat = sys.argv[idx + 1]
            if cat in CATEGORIES:
                scrape_category(conn, cat, CATEGORIES[cat])
            else:
                print(f"Unknown category: {cat}. Available: {list(CATEGORIES.keys())}")
        else:
            scrape_all(conn)
        print_stats(conn)

    elif cmd == "stats":
        print_stats(conn)

    else:
        print(__doc__)

    conn.close()


if __name__ == "__main__":
    main()
