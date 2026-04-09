"""Reddit scraper for LLMOS UX Research.

Scrapes posts and comments from OS-related subreddits to understand
how real users struggle with their operating systems.

No auth required — uses Reddit's public JSON API (.json suffix).
Rate limited to 1 request per 2 seconds.

Usage:
    python reddit_scraper.py scrape          # scrape all subreddits
    python reddit_scraper.py scrape --sub windows --limit 500
    python reddit_scraper.py comments        # fetch comments for top posts
    python reddit_scraper.py stats           # show collection stats
    python reddit_scraper.py push            # push to Hyphae
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ux_researcher.reddit")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SUBREDDITS = [
    "windows",
    "Windows11",
    "linux4noobs",
    "Ubuntu",
    "techsupport",
    "apple",
    "ChromeOS",
    "elderly",       # often has tech frustration posts
    "accessibility",
]

# Additional subreddits — expanded to replace MS Answers + Apple Support
BONUS_SUBREDDITS = [
    # Windows ecosystem (replaces MS Answers)
    "WindowsHelp",
    "Windows10",
    "sysadmin",
    "HomeNetworking",
    # Linux ecosystem
    "linuxquestions",
    "pop_os",
    "fedora",
    "kde",
    "gnome",
    "EndeavourOS",
    "archlinux",
    "linuxmint",
    # Apple ecosystem (replaces Apple Support Communities)
    "MacOS",
    "iphone",
    "ios",
    "ipados",
    "MacApps",
    # General
    "buildapc",
    "AskTechnology",
    "computing",
]

DB_PATH = Path.home() / ".openkeel" / "ux_research.db"
HYPHAE_URL = "http://127.0.0.1:8102"  # dedicated UX research Hyphae instance
RATE_LIMIT = 2.0  # seconds between requests

USER_AGENT = "LLMOS-UX-Research/1.0 (research bot; contact: ux@llmos.dev)"

# Listings to scrape per subreddit (Reddit returns max 100 per request)
SORT_MODES = ["hot", "top", "new"]  # top gives best signal, hot for trends, new for freshness
TOP_TIMEFRAMES = ["month", "year", "all"]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id              TEXT PRIMARY KEY,
            subreddit       TEXT NOT NULL,
            title           TEXT NOT NULL,
            selftext        TEXT DEFAULT '',
            score           INTEGER DEFAULT 0,
            num_comments    INTEGER DEFAULT 0,
            created_utc     REAL NOT NULL,
            author          TEXT DEFAULT '',
            url             TEXT DEFAULT '',
            flair           TEXT DEFAULT '',
            permalink       TEXT DEFAULT '',
            is_self         INTEGER DEFAULT 1,
            scraped_at      REAL NOT NULL,
            comments_scraped INTEGER DEFAULT 0,
            pushed_to_hyphae INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS comments (
            id              TEXT PRIMARY KEY,
            post_id         TEXT NOT NULL REFERENCES posts(id),
            parent_id       TEXT DEFAULT '',
            author          TEXT DEFAULT '',
            body            TEXT NOT NULL,
            score           INTEGER DEFAULT 0,
            created_utc     REAL NOT NULL,
            depth           INTEGER DEFAULT 0,
            scraped_at      REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_posts_sub ON posts(subreddit);
        CREATE INDEX IF NOT EXISTS idx_posts_score ON posts(score DESC);
        CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Reddit API
# ---------------------------------------------------------------------------

def _reddit_get(url: str, params: dict | None = None) -> dict:
    """Make a rate-limited request to Reddit's JSON API."""
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{query}"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            time.sleep(RATE_LIMIT)  # rate limit
            return data
    except urllib.error.HTTPError as e:
        if e.code == 429:
            log.warning("Rate limited, waiting 60s...")
            time.sleep(60)
            return _reddit_get(url)  # retry
        log.error("HTTP %d for %s", e.code, url)
        return {}
    except Exception as e:
        log.error("Request failed: %s — %s", url, e)
        return {}


def scrape_subreddit(conn: sqlite3.Connection, subreddit: str,
                     sort: str = "top", timeframe: str = "year",
                     limit: int = 200, after: str = "") -> int:
    """Scrape posts from a subreddit. Returns number of new posts saved."""
    log.info("Scraping r/%s (%s/%s, limit=%d)", subreddit, sort, timeframe, limit)

    saved = 0
    after_token = after
    fetched = 0

    while fetched < limit:
        batch_size = min(100, limit - fetched)
        url = f"https://www.reddit.com/r/{subreddit}/{sort}.json"
        params = {"limit": str(batch_size), "raw_json": "1"}
        if sort == "top":
            params["t"] = timeframe
        if after_token:
            params["after"] = after_token

        data = _reddit_get(url, params)
        listing = data.get("data", {})
        children = listing.get("children", [])

        if not children:
            break

        for child in children:
            post = child.get("data", {})
            if not post.get("id"):
                continue

            post_id = f"t3_{post['id']}"
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO posts
                       (id, subreddit, title, selftext, score, num_comments,
                        created_utc, author, url, flair, permalink, is_self, scraped_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        post_id,
                        subreddit,
                        post.get("title", ""),
                        post.get("selftext", "")[:10000],  # cap at 10K chars
                        post.get("score", 0),
                        post.get("num_comments", 0),
                        post.get("created_utc", 0),
                        post.get("author", ""),
                        post.get("url", ""),
                        post.get("link_flair_text", ""),
                        post.get("permalink", ""),
                        1 if post.get("is_self") else 0,
                        time.time(),
                    ),
                )
                saved += 1
            except sqlite3.IntegrityError:
                pass  # duplicate

        conn.commit()
        fetched += len(children)
        after_token = listing.get("after", "")
        if not after_token:
            break

        log.info("  r/%s: fetched %d/%d, saved %d new", subreddit, fetched, limit, saved)

    log.info("  r/%s done: %d new posts saved", subreddit, saved)
    return saved


def scrape_comments(conn: sqlite3.Connection, min_score: int = 10,
                    min_comments: int = 5, limit: int = 500) -> int:
    """Fetch comments for top posts that haven't been scraped yet."""
    rows = conn.execute(
        """SELECT id, permalink FROM posts
           WHERE comments_scraped = 0
             AND (score >= ? OR num_comments >= ?)
           ORDER BY score DESC
           LIMIT ?""",
        (min_score, min_comments, limit),
    ).fetchall()

    log.info("Fetching comments for %d posts", len(rows))
    total_comments = 0

    for row in rows:
        post_id = row["id"]
        permalink = row["permalink"]
        if not permalink:
            continue

        url = f"https://www.reddit.com{permalink}.json"
        params = {"limit": "50", "depth": "2", "raw_json": "1"}
        data = _reddit_get(url, params)

        if not isinstance(data, list) or len(data) < 2:
            continue

        comments_data = data[1].get("data", {}).get("children", [])
        saved = _save_comments(conn, post_id, comments_data, depth=0)
        total_comments += saved

        conn.execute("UPDATE posts SET comments_scraped = 1 WHERE id = ?", (post_id,))
        conn.commit()

    log.info("Total comments saved: %d", total_comments)
    return total_comments


def _save_comments(conn: sqlite3.Connection, post_id: str,
                   children: list, depth: int) -> int:
    """Recursively save comments up to depth 2."""
    saved = 0
    for child in children:
        if child.get("kind") != "t1":
            continue
        comment = child.get("data", {})
        if not comment.get("id"):
            continue

        comment_id = f"t1_{comment['id']}"
        body = comment.get("body", "")
        if not body or body == "[deleted]" or body == "[removed]":
            continue

        try:
            conn.execute(
                """INSERT OR IGNORE INTO comments
                   (id, post_id, parent_id, author, body, score, created_utc, depth, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    comment_id,
                    post_id,
                    comment.get("parent_id", ""),
                    comment.get("author", ""),
                    body[:5000],
                    comment.get("score", 0),
                    comment.get("created_utc", 0),
                    depth,
                    time.time(),
                ),
            )
            saved += 1
        except sqlite3.IntegrityError:
            pass

        # Recurse into replies (depth 1 only)
        if depth < 1:
            replies = comment.get("replies", "")
            if isinstance(replies, dict):
                reply_children = replies.get("data", {}).get("children", [])
                saved += _save_comments(conn, post_id, reply_children, depth + 1)

    return saved


# ---------------------------------------------------------------------------
# Hyphae push
# ---------------------------------------------------------------------------

def push_to_hyphae(conn: sqlite3.Connection, hyphae_url: str = HYPHAE_URL,
                   batch_size: int = 50) -> int:
    """Push unpushed posts to Hyphae for embedding + retrieval."""
    rows = conn.execute(
        "SELECT * FROM posts WHERE pushed_to_hyphae = 0 ORDER BY score DESC LIMIT ?",
        (batch_size,),
    ).fetchall()

    pushed = 0
    for row in rows:
        text = f"[r/{row['subreddit']}] {row['title']}\n\n{row['selftext'][:2000]}"
        # Add top comments if available
        comments = conn.execute(
            "SELECT body, score FROM comments WHERE post_id = ? ORDER BY score DESC LIMIT 3",
            (row["id"],),
        ).fetchall()
        if comments:
            text += "\n\nTop comments:\n"
            for c in comments:
                text += f"- {c['body'][:300]}\n"

        try:
            data = json.dumps({
                "text": text,
                "source": f"reddit:r/{row['subreddit']}",
                "metadata": {
                    "post_id": row["id"],
                    "subreddit": row["subreddit"],
                    "score": row["score"],
                    "num_comments": row["num_comments"],
                    "created_utc": row["created_utc"],
                },
            }).encode()
            req = urllib.request.Request(
                f"{hyphae_url}/remember",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
            conn.execute("UPDATE posts SET pushed_to_hyphae = 1 WHERE id = ?", (row["id"],))
            pushed += 1
        except Exception as e:
            log.debug("Hyphae push failed for %s: %s", row["id"], e)

    conn.commit()
    log.info("Pushed %d/%d posts to Hyphae", pushed, len(rows))
    return pushed


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(conn: sqlite3.Connection):
    total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    pushed = conn.execute("SELECT COUNT(*) FROM posts WHERE pushed_to_hyphae = 1").fetchone()[0]
    comments_scraped = conn.execute("SELECT COUNT(*) FROM posts WHERE comments_scraped = 1").fetchone()[0]

    print(f"UX Research Database: {DB_PATH}")
    print(f"  Total posts:      {total_posts:,}")
    print(f"  Total comments:   {total_comments:,}")
    print(f"  Comments scraped: {comments_scraped:,} posts")
    print(f"  Pushed to Hyphae: {pushed:,}")
    print()

    print("By subreddit:")
    for row in conn.execute(
        "SELECT subreddit, COUNT(*) as cnt, AVG(score) as avg_score "
        "FROM posts GROUP BY subreddit ORDER BY cnt DESC"
    ).fetchall():
        print(f"  r/{row['subreddit']:<20} {row['cnt']:>6} posts  (avg score: {row['avg_score']:.0f})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )

    conn = init_db()

    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "scrape":
        # Parse optional args
        subs = SUBREDDITS
        limit = 200
        if "--sub" in sys.argv:
            idx = sys.argv.index("--sub")
            subs = [sys.argv[idx + 1]]
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            limit = int(sys.argv[idx + 1])
        if "--all" in sys.argv:
            subs = SUBREDDITS + BONUS_SUBREDDITS

        total = 0
        for sub in subs:
            for sort in SORT_MODES:
                if sort == "top":
                    for tf in TOP_TIMEFRAMES:
                        total += scrape_subreddit(conn, sub, sort=sort,
                                                  timeframe=tf, limit=limit)
                else:
                    total += scrape_subreddit(conn, sub, sort=sort, limit=limit)
        print(f"\nTotal new posts: {total}")
        print_stats(conn)

    elif cmd == "comments":
        min_score = 10
        if "--min-score" in sys.argv:
            idx = sys.argv.index("--min-score")
            min_score = int(sys.argv[idx + 1])
        total = scrape_comments(conn, min_score=min_score)
        print(f"\nTotal new comments: {total}")

    elif cmd == "push":
        batch = 100
        if "--batch" in sys.argv:
            idx = sys.argv.index("--batch")
            batch = int(sys.argv[idx + 1])
        total = push_to_hyphae(conn, batch_size=batch)
        print(f"\nPushed: {total}")

    elif cmd == "stats":
        print_stats(conn)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

    conn.close()


if __name__ == "__main__":
    main()
