"""Apple Support Communities scraper for LLMOS UX Research.

Scrapes discussion threads from discussions.apple.com — Apple's
community support forum where users describe Mac/iOS/iPadOS problems.

Focus: usability complaints, workflow failures, confusion — NOT hardware.

Uses DuckDuckGo site search (Apple has no public API for community forums),
then scrapes each thread page.

Usage:
    python apple_scraper.py scrape
    python apple_scraper.py scrape --category macos
    python apple_scraper.py stats
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

log = logging.getLogger("ux_researcher.apple")

DB_PATH = Path.home() / ".openkeel" / "ux_research.db"
RATE_LIMIT = 3.0
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

CATEGORIES = {
    "macos": [
        "macos problem", "mac not working", "finder keeps crashing",
        "mac slow after update", "macos settings confusing",
        "mac file management frustrating", "spotlight not finding files",
        "mac permissions annoying", "mac dock confusing",
        "macos update broke", "time machine not working",
    ],
    "ios": [
        "iphone confusing settings", "ios update problem",
        "iphone battery after update", "iphone notifications overwhelming",
        "ios privacy settings confusing", "iphone storage full cant fix",
    ],
    "ipad": [
        "ipad multitasking confusing", "ipados file management bad",
        "ipad stage manager problem", "ipad not like laptop",
    ],
    "accessibility": [
        "apple accessibility problem", "voiceover confusing",
        "mac accessibility settings", "iphone hard to use elderly",
    ],
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS apple_threads (
            id              TEXT PRIMARY KEY,
            url             TEXT NOT NULL,
            category        TEXT DEFAULT '',
            title           TEXT NOT NULL,
            question_body   TEXT DEFAULT '',
            question_author TEXT DEFAULT '',
            reply_body      TEXT DEFAULT '',
            reply_count     INTEGER DEFAULT 0,
            view_count      INTEGER DEFAULT 0,
            helpful_count   INTEGER DEFAULT 0,
            created_date    TEXT DEFAULT '',
            scraped_at      REAL NOT NULL,
            pushed_to_hyphae INTEGER DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_apple_cat ON apple_threads(category);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

class AppleThreadParser(HTMLParser):
    """Extract content from Apple Support Community thread pages."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.question = ""
        self.reply = ""
        self.author = ""
        self._capture = False
        self._current = ""
        self._text_buffer = []
        self._in_question = False
        self._in_reply = False

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")
        itemprop = attr_dict.get("itemprop", "")

        if tag == "h1":
            self._capture = True
            self._current = "title"
            self._text_buffer = []
        elif itemprop == "text" and not self._in_reply:
            self._in_question = True
            self._capture = True
            self._current = "question"
            self._text_buffer = []
        elif "reply-content" in cls or "accepted-solution" in cls:
            self._in_reply = True
            self._capture = True
            self._current = "reply"
            self._text_buffer = []
        elif "user-profile-link" in cls and not self.author:
            self._capture = True
            self._current = "author"
            self._text_buffer = []

    def handle_endtag(self, tag):
        if self._capture and tag in ("h1", "div", "span", "p", "a"):
            text = " ".join(self._text_buffer).strip()
            if self._current == "title" and not self.title and text:
                self.title = text
            elif self._current == "question" and not self.question and len(text) > 20:
                self.question = text[:5000]
                self._in_question = False
            elif self._current == "reply" and not self.reply and len(text) > 20:
                self.reply = text[:5000]
            elif self._current == "author" and not self.author:
                self.author = text
            self._capture = False
            self._text_buffer = []

    def handle_data(self, data):
        if self._capture:
            self._text_buffer.append(data.strip())


class DDGSearchParser(HTMLParser):
    """Extract URLs from DuckDuckGo search results."""

    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if "discussions.apple.com" in href and "/thread/" in href:
                url = href.split("&")[0]
                if url.startswith("//"):
                    url = "https:" + url
                if url not in self.urls:
                    self.urls.append(url)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _fetch_page(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
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


def find_thread_urls(query: str, num_results: int = 15) -> list[str]:
    """Search DuckDuckGo for Apple Support Community threads."""
    encoded = urllib.parse.quote(f"site:discussions.apple.com {query}")
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    html = _fetch_page(url)
    if not html:
        return []

    parser = DDGSearchParser()
    parser.feed(html)

    # Also try extracting from href patterns
    extra = re.findall(r'https?://discussions\.apple\.com/thread/\d+', html)
    for u in extra:
        if u not in parser.urls:
            parser.urls.append(u)

    return parser.urls[:num_results]


def scrape_thread(url: str) -> dict:
    """Scrape a single Apple Support Community thread."""
    html = _fetch_page(url)
    if not html:
        return {}

    parser = AppleThreadParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    if not parser.title and not parser.question:
        # Fallback: extract from meta tags
        title_match = re.search(r'<title>(.*?)</title>', html)
        if title_match:
            parser.title = title_match.group(1).split(" - Apple")[0].strip()

        # Try og:description for question body
        desc_match = re.search(r'<meta property="og:description" content="(.*?)"', html)
        if desc_match:
            parser.question = desc_match.group(1)

    if not parser.title:
        return {}

    thread_match = re.search(r'/thread/(\d+)', url)
    thread_id = thread_match.group(1) if thread_match else str(hash(url))

    return {
        "id": f"apple_{thread_id}",
        "url": url,
        "title": parser.title,
        "question_body": parser.question,
        "question_author": parser.author,
        "reply_body": parser.reply,
    }


def scrape_category(conn: sqlite3.Connection, category: str,
                    queries: list[str], threads_per_query: int = 10) -> int:
    ensure_tables(conn)
    saved = 0

    for query in queries:
        log.info("Searching: %s", query)
        urls = find_thread_urls(query, num_results=threads_per_query)
        log.info("  Found %d thread URLs", len(urls))

        for url in urls:
            existing = conn.execute("SELECT id FROM apple_threads WHERE url = ?", (url,)).fetchone()
            if existing:
                continue

            thread = scrape_thread(url)
            if not thread or not thread.get("title"):
                continue

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO apple_threads
                       (id, url, category, title, question_body, question_author,
                        reply_body, scraped_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        thread["id"], thread["url"], category,
                        thread["title"], thread.get("question_body", ""),
                        thread.get("question_author", ""),
                        thread.get("reply_body", ""),
                        time.time(),
                    ),
                )
                saved += 1
            except sqlite3.IntegrityError:
                pass

        conn.commit()

    log.info("Category %s: %d new threads", category, saved)
    return saved


def scrape_all(conn: sqlite3.Connection, threads_per_query: int = 10) -> int:
    total = 0
    for category, queries in CATEGORIES.items():
        log.info("=== Category: %s (%d queries) ===", category, len(queries))
        saved = scrape_category(conn, category, queries, threads_per_query)
        total += saved
    return total


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(conn: sqlite3.Connection):
    ensure_tables(conn)
    total = conn.execute("SELECT COUNT(*) FROM apple_threads").fetchone()[0]
    print(f"Apple Support threads: {total:,}")
    print()

    print("By category:")
    for row in conn.execute(
        "SELECT category, COUNT(*) as cnt FROM apple_threads GROUP BY category ORDER BY cnt DESC"
    ).fetchall():
        print(f"  {row['category']:<20} {row['cnt']:>5}")

    print("\nSample threads:")
    for row in conn.execute(
        "SELECT category, title FROM apple_threads LIMIT 10"
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
