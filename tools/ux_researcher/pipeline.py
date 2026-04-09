"""UX Research Pipeline — orchestrates the full scrape → categorize → analyze flow.

This is the main entry point deployed on kagg. Runs all phases in sequence
with progress reporting to the fractal engine and kanban board.

Usage:
    python pipeline.py run                 # full pipeline run
    python pipeline.py run --phase collect # only collection phase
    python pipeline.py run --phase tag     # only categorization
    python pipeline.py status              # show pipeline status
    python pipeline.py deploy              # copy to kagg and set up timers
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("ux_researcher.pipeline")

DB_PATH = Path.home() / ".openkeel" / "ux_research.db"
HYPHAE_URL = "http://127.0.0.1:8100"
KANBAN_URL = "http://127.0.0.1:8200"

# Import our modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def run_collection(conn: sqlite3.Connection, full: bool = True) -> dict:
    """Phase 1: Collect posts from all sources."""
    from tools.ux_researcher.reddit_scraper import (
        SUBREDDITS, BONUS_SUBREDDITS, SORT_MODES, TOP_TIMEFRAMES,
        scrape_subreddit, scrape_comments,
    )

    subs = SUBREDDITS + BONUS_SUBREDDITS if full else SUBREDDITS
    total_posts = 0
    limit_per = 100 if full else 25

    log.info("=== COLLECTION PHASE: %d subreddits ===", len(subs))

    for sub in subs:
        for sort in SORT_MODES:
            if sort == "top":
                for tf in TOP_TIMEFRAMES:
                    try:
                        saved = scrape_subreddit(conn, sub, sort=sort,
                                                 timeframe=tf, limit=limit_per)
                        total_posts += saved
                    except Exception as e:
                        log.error("Failed r/%s/%s/%s: %s", sub, sort, tf, e)
            else:
                try:
                    saved = scrape_subreddit(conn, sub, sort=sort, limit=limit_per)
                    total_posts += saved
                except Exception as e:
                    log.error("Failed r/%s/%s: %s", sub, sort, e)

    # Fetch comments for top posts
    total_comments = 0
    try:
        total_comments = scrape_comments(conn, min_score=10, min_comments=5, limit=200)
    except Exception as e:
        log.error("Comment scraping failed: %s", e)

    return {"new_posts": total_posts, "new_comments": total_comments}


def run_categorization(conn: sqlite3.Connection, limit: int = 200) -> dict:
    """Phase 2: Tag posts with LLM."""
    from tools.ux_researcher.categorizer import categorize_batch, ensure_tags_table

    ensure_tags_table(conn)

    # Check if Ollama is reachable
    import urllib.request
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        urllib.request.urlopen(req, timeout=5)
        host = "127.0.0.1"
    except Exception:
        # Try kagg
        try:
            req = urllib.request.Request("http://192.168.0.224:11434/api/tags")
            urllib.request.urlopen(req, timeout=5)
            host = "192.168.0.224"
        except Exception:
            log.error("No Ollama endpoint available — skipping categorization")
            return {"tagged": 0, "error": "no_ollama"}

    log.info("=== CATEGORIZATION PHASE: using Ollama on %s ===", host)
    tagged = categorize_batch(conn, limit=limit, host=host)
    return {"tagged": tagged}


def run_push_hyphae(conn: sqlite3.Connection, hyphae_url: str = HYPHAE_URL) -> dict:
    """Push collected data to Hyphae for semantic retrieval."""
    from tools.ux_researcher.reddit_scraper import push_to_hyphae

    pushed = push_to_hyphae(conn, hyphae_url=hyphae_url, batch_size=100)
    return {"pushed": pushed}


def get_status(conn: sqlite3.Connection) -> dict:
    """Get full pipeline status."""
    stats = {}

    stats["posts"] = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    stats["comments"] = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    stats["pushed"] = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE pushed_to_hyphae = 1"
    ).fetchone()[0]

    # Tags
    try:
        stats["tagged"] = conn.execute("SELECT COUNT(*) FROM post_tags").fetchone()[0]
    except Exception:
        stats["tagged"] = 0

    # By subreddit
    stats["by_subreddit"] = {}
    for row in conn.execute(
        "SELECT subreddit, COUNT(*) as cnt FROM posts GROUP BY subreddit ORDER BY cnt DESC"
    ).fetchall():
        stats["by_subreddit"][row["subreddit"]] = row["cnt"]

    # MS threads
    try:
        stats["ms_threads"] = conn.execute("SELECT COUNT(*) FROM ms_threads").fetchone()[0]
    except Exception:
        stats["ms_threads"] = 0

    # Apple threads
    try:
        stats["apple_threads"] = conn.execute("SELECT COUNT(*) FROM apple_threads").fetchone()[0]
    except Exception:
        stats["apple_threads"] = 0

    # Phase assessment
    total_text = stats["posts"] + stats.get("ms_threads", 0) + stats.get("apple_threads", 0)
    stats["phase"] = "collect"
    if total_text >= 500:
        stats["phase"] = "categorize"
    if stats["tagged"] >= 1000:
        stats["phase"] = "pattern_extraction"
    if stats["tagged"] >= 5000:
        stats["phase"] = "ready_for_analysis"

    return stats


def report_to_kanban(stats: dict, task_id: int = 247):
    """Report pipeline status to kanban board."""
    try:
        import urllib.request
        report = (
            f"Pipeline: {stats['posts']} posts, {stats['comments']} comments, "
            f"{stats.get('tagged', 0)} tagged. Phase: {stats.get('phase', 'collect')}"
        )
        data = json.dumps({
            "agent_name": "ux-researcher",
            "status": "done",
            "report": report,
        }).encode()
        req = urllib.request.Request(
            f"{KANBAN_URL}/api/task/{task_id}/report",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


def report_to_hyphae(stats: dict):
    """Save pipeline status to Hyphae."""
    try:
        import urllib.request
        text = (
            f"UX Research pipeline status ({time.strftime('%Y-%m-%d %H:%M')}): "
            f"{stats['posts']} Reddit posts, {stats['comments']} comments, "
            f"{stats.get('tagged', 0)} LLM-tagged, "
            f"{len(stats.get('by_subreddit', {}))} subreddits covered. "
            f"Current phase: {stats.get('phase', 'collect')}"
        )
        data = json.dumps({"text": text, "source": "ux-researcher"}).encode()
        req = urllib.request.Request(
            f"{HYPHAE_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "run":
        phase = None
        full = "--full" in sys.argv
        if "--phase" in sys.argv:
            idx = sys.argv.index("--phase")
            phase = sys.argv[idx + 1]

        results = {}
        start = time.time()

        if phase is None or phase == "collect":
            log.info("Running collection phase...")
            results["collect"] = run_collection(conn, full=full)

        if phase is None or phase == "tag":
            log.info("Running categorization phase...")
            results["categorize"] = run_categorization(conn)

        if phase is None or phase == "push":
            log.info("Pushing to Hyphae...")
            results["push"] = run_push_hyphae(conn)

        elapsed = time.time() - start
        stats = get_status(conn)

        print(f"\n{'='*50}")
        print(f"Pipeline run complete in {elapsed:.0f}s")
        print(f"  Posts: {stats['posts']}")
        print(f"  Comments: {stats['comments']}")
        print(f"  Tagged: {stats.get('tagged', 0)}")
        print(f"  Pushed: {stats['pushed']}")
        print(f"  Phase: {stats.get('phase', 'collect')}")

        report_to_kanban(stats)
        report_to_hyphae(stats)

    elif cmd == "status":
        stats = get_status(conn)
        print(json.dumps(stats, indent=2))

    elif cmd == "deploy":
        _deploy_to_kagg()

    else:
        print(__doc__)

    conn.close()


def _deploy_to_kagg():
    """Deploy pipeline to kagg via SSH."""
    script_dir = Path(__file__).parent
    deploy_script = script_dir / "deploy_kagg.sh"
    if deploy_script.exists():
        os.execvp("bash", ["bash", str(deploy_script)])
    else:
        print(f"Deploy script not found: {deploy_script}")


if __name__ == "__main__":
    main()
