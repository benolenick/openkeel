"""LLM Categorizer for LLMOS UX Research.

Reads scraped posts from the SQLite database and tags each one using
a local LLM (Qwen on kagg via Ollama).

Tags per post:
  - user_intent: what the user was trying to do
  - what_went_wrong: the failure mode
  - emotion: frustrated | confused | angry | helpless | resigned | neutral
  - os: Windows | Mac | Linux | ChromeOS | unknown
  - skill_level: novice | intermediate | power
  - category: update | driver | settings | file_management | networking |
              performance | security | privacy | accessibility | ui | other

The original text is NEVER summarized — we preserve the user's voice.

Usage:
    python categorizer.py run              # categorize uncategorized posts
    python categorizer.py run --limit 100  # batch of 100
    python categorizer.py stats            # show categorization stats
    python categorizer.py export           # export tagged data as JSON
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
from pathlib import Path

log = logging.getLogger("ux_researcher.categorizer")

DB_PATH = Path.home() / ".openkeel" / "ux_research.db"

# Ollama endpoint — default to localhost (kaloth)
OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434
OLLAMA_MODEL = "gemma4:e2b"


# ---------------------------------------------------------------------------
# Database schema extension
# ---------------------------------------------------------------------------

def ensure_tags_table(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS post_tags (
            post_id         TEXT PRIMARY KEY REFERENCES posts(id),
            user_intent     TEXT DEFAULT '',
            what_went_wrong TEXT DEFAULT '',
            emotion         TEXT DEFAULT '',
            os              TEXT DEFAULT '',
            skill_level     TEXT DEFAULT '',
            category        TEXT DEFAULT '',
            raw_llm_output  TEXT DEFAULT '',
            tagged_at       REAL NOT NULL,
            model_used      TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_tags_emotion ON post_tags(emotion);
        CREATE INDEX IF NOT EXISTS idx_tags_os ON post_tags(os);
        CREATE INDEX IF NOT EXISTS idx_tags_intent ON post_tags(user_intent);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# LLM tagging
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a UX researcher analyzing user complaints about operating systems.

For each post, extract these tags as JSON:

{
  "user_intent": "what the user was trying to do (1 sentence, their words)",
  "what_went_wrong": "the failure mode (1 sentence, specific)",
  "emotion": "one of: frustrated | confused | angry | helpless | resigned | neutral",
  "os": "one of: Windows | Mac | Linux | ChromeOS | iOS | unknown",
  "skill_level": "one of: novice | intermediate | power",
  "category": "one of: update | driver | settings | file_management | networking | performance | security | privacy | accessibility | ui_design | ui_consistency | bloatware | forced_change | install_setup | notifications | storage | consent_control | other"
}

Rules:
- Use the user's own words when possible
- Be specific about the failure mode (not "it didn't work")
- INFER the OS from the subreddit name when the post doesn't state it explicitly:
  r/windows, r/Windows11, r/Windows10, r/WindowsHelp → Windows
  r/MacOS, r/apple, r/MacApps → Mac
  r/iphone, r/ios, r/ipados → iOS
  r/linux4noobs, r/Ubuntu, r/linuxquestions, r/pop_os, r/fedora, r/archlinux, r/linuxmint, r/EndeavourOS → Linux
  r/ChromeOS → ChromeOS
- If the post isn't about an OS problem, set user_intent to "not_applicable"
- Respond ONLY with the JSON object, no other text"""


def tag_post(title: str, body: str, subreddit: str,
             host: str = OLLAMA_HOST, port: int = OLLAMA_PORT,
             model: str = OLLAMA_MODEL) -> dict:
    """Tag a single post using the LLM."""
    prompt = f"Subreddit: r/{subreddit}\nTitle: {title}\n\n{body[:3000]}"

    try:
        data = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.1, "num_predict": 512},
        }).encode()

        req = urllib.request.Request(
            f"http://{host}:{port}/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        raw = json.loads(resp.read()).get("message", {}).get("content", "")

        # Parse JSON from response
        return _parse_tags(raw)
    except Exception as e:
        log.error("LLM tagging failed: %s", e)
        return {"error": str(e), "raw": ""}


def _parse_tags(raw: str) -> dict:
    """Extract tag JSON from LLM response."""
    raw = raw.strip()

    # Direct JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # JSON in code fence
    if "```" in raw:
        import re
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

    # Find first { to last }
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    return {"error": "parse_failed", "raw": raw[:500]}


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def categorize_batch(conn: sqlite3.Connection, limit: int = 50,
                     host: str = OLLAMA_HOST, port: int = OLLAMA_PORT,
                     model: str = OLLAMA_MODEL) -> int:
    """Tag a batch of uncategorized posts."""
    ensure_tags_table(conn)

    rows = conn.execute(
        """SELECT p.id, p.title, p.selftext, p.subreddit
           FROM posts p
           LEFT JOIN post_tags t ON p.id = t.post_id
           WHERE t.post_id IS NULL
             AND p.selftext != ''
           ORDER BY p.score DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    if not rows:
        log.info("No uncategorized posts remaining")
        return 0

    log.info("Categorizing %d posts using %s on %s:%d", len(rows), model, host, port)
    tagged = 0

    for row in rows:
        tags = tag_post(row["title"], row["selftext"], row["subreddit"],
                        host=host, port=port, model=model)

        if "error" in tags:
            log.warning("Tagging failed for %s: %s", row["id"], tags.get("error"))
            continue

        try:
            conn.execute(
                """INSERT OR REPLACE INTO post_tags
                   (post_id, user_intent, what_went_wrong, emotion, os,
                    skill_level, category, raw_llm_output, tagged_at, model_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    tags.get("user_intent", ""),
                    tags.get("what_went_wrong", ""),
                    tags.get("emotion", ""),
                    tags.get("os", ""),
                    tags.get("skill_level", ""),
                    tags.get("category", ""),
                    json.dumps(tags),
                    time.time(),
                    model,
                ),
            )
            tagged += 1
            if tagged % 10 == 0:
                conn.commit()
                log.info("  Tagged %d/%d", tagged, len(rows))
        except Exception as e:
            log.error("DB error for %s: %s", row["id"], e)

    conn.commit()
    log.info("Tagged %d/%d posts", tagged, len(rows))
    return tagged


# ---------------------------------------------------------------------------
# Stats + export
# ---------------------------------------------------------------------------

def print_stats(conn: sqlite3.Connection):
    ensure_tags_table(conn)

    total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    tagged = conn.execute("SELECT COUNT(*) FROM post_tags").fetchone()[0]
    print(f"Posts: {total_posts:,} total, {tagged:,} tagged ({tagged/max(total_posts,1)*100:.0f}%)")
    print()

    print("By emotion:")
    for row in conn.execute(
        "SELECT emotion, COUNT(*) as cnt FROM post_tags GROUP BY emotion ORDER BY cnt DESC"
    ).fetchall():
        bar = "#" * min(int(row["cnt"] / max(tagged, 1) * 40), 40)
        print(f"  {row['emotion']:<14} {row['cnt']:>5}  {bar}")

    print("\nBy OS:")
    for row in conn.execute(
        "SELECT os, COUNT(*) as cnt FROM post_tags GROUP BY os ORDER BY cnt DESC"
    ).fetchall():
        print(f"  {row['os']:<14} {row['cnt']:>5}")

    print("\nBy category:")
    for row in conn.execute(
        "SELECT category, COUNT(*) as cnt FROM post_tags GROUP BY category ORDER BY cnt DESC"
    ).fetchall():
        print(f"  {row['category']:<20} {row['cnt']:>5}")

    print("\nTop 20 user intents:")
    for row in conn.execute(
        "SELECT user_intent, COUNT(*) as cnt FROM post_tags "
        "WHERE user_intent != '' AND user_intent != 'not_applicable' "
        "GROUP BY user_intent ORDER BY cnt DESC LIMIT 20"
    ).fetchall():
        print(f"  [{row['cnt']:>3}] {row['user_intent'][:80]}")


def export_tagged(conn: sqlite3.Connection, output_path: str = "ux_research_tagged.json"):
    ensure_tags_table(conn)

    rows = conn.execute(
        """SELECT p.*, t.user_intent, t.what_went_wrong, t.emotion,
                  t.os, t.skill_level, t.category
           FROM posts p
           JOIN post_tags t ON p.id = t.post_id
           ORDER BY p.score DESC"""
    ).fetchall()

    data = [dict(r) for r in rows]
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"Exported {len(data)} tagged posts to {output_path}")


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

    if cmd == "run":
        limit = 50
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            limit = int(sys.argv[idx + 1])
        categorize_batch(conn, limit=limit)

    elif cmd == "stats":
        print_stats(conn)

    elif cmd == "export":
        out = "ux_research_tagged.json"
        if "--output" in sys.argv:
            idx = sys.argv.index("--output")
            out = sys.argv[idx + 1]
        export_tagged(conn, out)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

    conn.close()


if __name__ == "__main__":
    main()
