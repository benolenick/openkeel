#!/usr/bin/env python3
"""Callout — GitHub & community intelligence tool for AI development.

Modes:
  pulse      — scan GitHub, Reddit, HN, ArXiv, DevTo for AI developments
  research   — deep-dive a topic or repo across all sources
  compete    — competitive analysis: find repos similar to yours, compare
  sentiment  — gauge community sentiment on a topic from Reddit + HN comments
  track      — track specific repos/topics over time, alert on changes
  history    — browse past scans and research from the local database
  config     — print example config

Usage:
  callout pulse [--hours 24] [--limit 30] [--watch] [--json]
  callout research "browser-use agent framework" [--deep]
  callout research "https://github.com/owner/repo" [--deep]
  callout compete "hybrid local cloud LLM coding agent"
  callout sentiment "Claude Code vs Cursor"
  callout track add owner/repo
  callout track list
  callout track check
  callout history [--days 7]
  callout config
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
REDDIT_BASE = "https://www.reddit.com"
HN_ALGOLIA = "https://hn.algolia.com/api/v1"
ARXIV_API = "http://export.arxiv.org/api/query"
DEVTO_API = "https://dev.to/api"

DEFAULT_CONFIG_PATH = Path.home() / ".openkeel" / "callout.yaml"
HYPHAE_URL = os.getenv("HYPHAE_URL", "http://127.0.0.1:8100")
DB_PATH = Path.home() / ".openkeel" / "callout.db"

AI_SUBREDDITS = [
    "MachineLearning", "LocalLLaMA", "artificial", "singularity",
    "StableDiffusion", "ChatGPT", "ClaudeAI", "opensource",
    "selfhosted", "coding", "ExperiencedDevs",
]

AI_KEYWORDS = [
    "llm", "gpt", "claude", "gemini", "llama", "mistral", "agent", "rag",
    "fine-tune", "finetune", "inference", "transformer", "diffusion",
    "multimodal", "embedding", "vector", "mcp", "tool-use", "function-calling",
    "open-source", "self-hosted", "local model", "quantization", "gguf",
    "lora", "rlhf", "dpo", "moe", "mixture of experts", "benchmark",
    "context window", "reasoning", "chain-of-thought", "code generation",
    "browser automation", "computer use", "agentic", "openai", "anthropic",
    "huggingface", "vllm", "ollama", "groq", "together.ai", "cursor",
    "copilot", "codex", "devin", "bolt", "replit", "cline", "aider",
    "continue", "tabby", "deepseek", "qwen", "phi", "yi",
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class CalloutConfig:
    github_token_env: str = "GITHUB_TOKEN"
    hyphae_url: str = HYPHAE_URL
    subreddits: list[str] = field(default_factory=lambda: list(AI_SUBREDDITS))
    keywords: list[str] = field(default_factory=lambda: list(AI_KEYWORDS))
    github_languages: list[str] = field(default_factory=lambda: ["Python", "TypeScript", "Rust", "Go", "C++", "Java"])
    github_topics: list[str] = field(default_factory=lambda: [
        "ai", "llm", "agents", "machine-learning", "deep-learning",
        "generative-ai", "rag", "langchain", "transformers", "nlp",
    ])
    exclude_keywords: list[str] = field(default_factory=lambda: [
        "awesome-list", "tutorial", "course", "interview", "dotfiles",
    ])
    watch_interval: int = 1800
    default_hours: int = 24
    default_limit: int = 30

    @classmethod
    def from_file(cls, path: str | None = None) -> CalloutConfig:
        config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
        if not config_path.exists():
            return cls()
        try:
            import yaml
        except ImportError:
            return cls()
        raw = yaml.safe_load(config_path.read_text()) or {}
        gh = raw.get("github", {})
        reddit = raw.get("reddit", {})
        general = raw.get("general", {})
        return cls(
            github_token_env=gh.get("token_env", "GITHUB_TOKEN"),
            hyphae_url=general.get("hyphae_url", HYPHAE_URL),
            subreddits=reddit.get("subreddits", list(AI_SUBREDDITS)),
            keywords=general.get("keywords", list(AI_KEYWORDS)),
            github_languages=gh.get("languages", ["Python", "TypeScript", "Rust", "Go", "C++", "Java"]),
            github_topics=gh.get("topics", cls.github_topics),
            exclude_keywords=general.get("exclude_keywords", cls.exclude_keywords),
            watch_interval=int(general.get("watch_interval", 1800)),
            default_hours=int(general.get("default_hours", 24)),
            default_limit=int(general.get("default_limit", 30)),
        )


# ---------------------------------------------------------------------------
# Database — all scans, research, and tracked repos stored locally
# ---------------------------------------------------------------------------

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    scan_type TEXT NOT NULL,
    query TEXT,
    timestamp REAL NOT NULL,
    result_count INTEGER DEFAULT 0,
    data TEXT
);
CREATE TABLE IF NOT EXISTS tracked_repos (
    repo TEXT PRIMARY KEY,
    added_at REAL NOT NULL,
    last_checked REAL,
    last_stars INTEGER DEFAULT 0,
    last_data TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT,
    alert_type TEXT,
    message TEXT,
    timestamp REAL NOT NULL,
    seen INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_scans_time ON scans(timestamp);
CREATE INDEX IF NOT EXISTS idx_scans_type ON scans(scan_type);
"""


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.executescript(_DB_SCHEMA)
    return conn


def _save_scan(scan_type: str, query: str, results: Any, count: int) -> None:
    try:
        conn = _get_db()
        scan_id = hashlib.sha256(f"{scan_type}:{query}:{time.time()}".encode()).hexdigest()[:16]
        conn.execute(
            "INSERT INTO scans (id, scan_type, query, timestamp, result_count, data) VALUES (?,?,?,?,?,?)",
            (scan_id, scan_type, query, time.time(), count, json.dumps(results)[:50000]),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> Any:
    hdrs = headers or {}
    hdrs.setdefault("User-Agent", "openkeel-callout/2.0")
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        logger.warning("GET %s failed: %s", url, exc)
        return None


def _get_text(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> str:
    hdrs = headers or {}
    hdrs.setdefault("User-Agent", "openkeel-callout/2.0")
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _github_headers(config: CalloutConfig) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "openkeel-callout",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv(config.github_token_env, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _hyphae_save(config: CalloutConfig, text: str) -> None:
    try:
        data = json.dumps({"text": text, "source": "callout"}).encode()
        req = urllib.request.Request(
            f"{config.hyphae_url}/remember", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"[\n\r]+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Source: GitHub
# ---------------------------------------------------------------------------

def github_scan(config: CalloutConfig, hours: int, limit: int) -> list[dict]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    topics_query = " ".join(f"topic:{t}" for t in config.github_topics[:3])
    query = f"is:public archived:false created:>={since.date().isoformat()} {topics_query}"
    params = urllib.parse.urlencode({
        "q": query, "sort": "stars", "order": "desc",
        "per_page": str(min(limit * 2, 100)),
    })
    data = _get_json(f"{GITHUB_API}/search/repositories?{params}", headers=_github_headers(config))
    if not data or not isinstance(data.get("items"), list):
        return []

    results = []
    for repo in data["items"]:
        haystack = f"{repo.get('name', '')} {repo.get('description', '')} {' '.join(repo.get('topics', []))}".lower()
        if any(ex.lower() in haystack for ex in config.exclude_keywords):
            continue
        results.append(_repo_to_item(repo, "github", config))

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


def github_trending(config: CalloutConfig, limit: int = 15) -> list[dict]:
    since = datetime.now(UTC) - timedelta(days=7)
    topics_query = " ".join(f"topic:{t}" for t in config.github_topics[:4])
    query = f"is:public archived:false pushed:>={since.date().isoformat()} stars:>50 {topics_query}"
    params = urllib.parse.urlencode({
        "q": query, "sort": "stars", "order": "desc",
        "per_page": str(min(limit * 2, 100)),
    })
    data = _get_json(f"{GITHUB_API}/search/repositories?{params}", headers=_github_headers(config))
    if not data or not isinstance(data.get("items"), list):
        return []
    return [_repo_to_item(r, "github-trending", config) for r in data["items"][:limit]]


def github_repo_detail(config: CalloutConfig, repo_path: str) -> dict:
    """Full detail for one repo: info, README, issues, releases, contributors, commit activity."""
    repo = _get_json(f"{GITHUB_API}/repos/{repo_path}", headers=_github_headers(config))
    if not repo:
        return {"error": f"Could not fetch {repo_path}"}

    # README
    readme_data = _get_json(f"{GITHUB_API}/repos/{repo_path}/readme", headers=_github_headers(config))
    readme = ""
    if readme_data and readme_data.get("content"):
        try:
            readme = base64.b64decode(readme_data["content"]).decode("utf-8", errors="replace")[:4000]
        except Exception:
            pass

    # Issues
    issues = _get_json(f"{GITHUB_API}/repos/{repo_path}/issues?state=all&sort=updated&per_page=15",
                       headers=_github_headers(config)) or []

    # Releases
    releases = _get_json(f"{GITHUB_API}/repos/{repo_path}/releases?per_page=5",
                         headers=_github_headers(config)) or []

    # Contributors (top 10)
    contributors = _get_json(f"{GITHUB_API}/repos/{repo_path}/contributors?per_page=10",
                             headers=_github_headers(config)) or []

    # Commit activity (last year, weekly)
    commit_activity = _get_json(f"{GITHUB_API}/repos/{repo_path}/stats/commit_activity",
                                headers=_github_headers(config)) or []

    # Recent commits
    commits = _get_json(f"{GITHUB_API}/repos/{repo_path}/commits?per_page=10",
                        headers=_github_headers(config)) or []

    # Calculate velocity metrics
    recent_weeks = commit_activity[-4:] if isinstance(commit_activity, list) else []
    weekly_commits = [w.get("total", 0) for w in recent_weeks if isinstance(w, dict)]
    avg_weekly = sum(weekly_commits) / len(weekly_commits) if weekly_commits else 0

    return {
        "name": repo.get("full_name"),
        "description": repo.get("description") or "",
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "watchers": repo.get("subscribers_count", 0),
        "language": repo.get("language") or "",
        "topics": repo.get("topics", []),
        "created": repo.get("created_at", ""),
        "updated": repo.get("updated_at", ""),
        "pushed": repo.get("pushed_at", ""),
        "license": (repo.get("license") or {}).get("spdx_id", ""),
        "open_issues": repo.get("open_issues_count", 0),
        "default_branch": repo.get("default_branch", "main"),
        "homepage": repo.get("homepage") or "",
        "size_kb": repo.get("size", 0),
        "velocity": {
            "avg_weekly_commits": round(avg_weekly, 1),
            "last_4_weeks": weekly_commits,
            "total_contributors": len(contributors) if isinstance(contributors, list) else 0,
        },
        "top_contributors": [
            {"login": c.get("login", ""), "contributions": c.get("contributions", 0)}
            for c in (contributors if isinstance(contributors, list) else [])[:5]
        ],
        "recent_commits": [
            {
                "sha": c.get("sha", "")[:7],
                "message": (c.get("commit", {}).get("message") or "").split("\n")[0][:100],
                "date": (c.get("commit", {}).get("committer") or {}).get("date", ""),
                "author": (c.get("author") or {}).get("login", ""),
            }
            for c in (commits if isinstance(commits, list) else [])[:8]
        ],
        "readme_preview": readme[:3000],
        "recent_issues": [
            {
                "title": i.get("title", ""),
                "state": i.get("state", ""),
                "comments": i.get("comments", 0),
                "created": i.get("created_at", ""),
                "url": i.get("html_url", ""),
                "labels": [l.get("name", "") for l in i.get("labels", [])],
                "is_pr": "pull_request" in i,
            }
            for i in (issues if isinstance(issues, list) else [])[:15]
        ],
        "recent_releases": [
            {
                "tag": r.get("tag_name", ""),
                "name": r.get("name", ""),
                "published": r.get("published_at", ""),
                "body": (r.get("body") or "")[:400],
            }
            for r in (releases if isinstance(releases, list) else [])[:5]
        ],
    }


def _repo_to_item(repo: dict, source: str, config: CalloutConfig) -> dict:
    stars = int(repo.get("stargazers_count", 0) or 0)
    score = _github_score(repo, config)
    return {
        "source": source,
        "title": repo.get("full_name", ""),
        "url": repo.get("html_url", ""),
        "description": (repo.get("description") or "")[:200],
        "score": score,
        "stars": stars,
        "language": repo.get("language") or "",
        "topics": repo.get("topics", [])[:5],
        "created": repo.get("created_at", ""),
        "meta": {
            "forks": repo.get("forks_count", 0),
            "open_issues": repo.get("open_issues_count", 0),
        },
    }


def _github_score(repo: dict, config: CalloutConfig) -> float:
    score = 0.0
    stars = int(repo.get("stargazers_count", 0) or 0)
    if stars:
        score += min(30.0, math.log2(stars + 1) * 5.0)
    haystack = f"{repo.get('name', '')} {repo.get('description', '')} {' '.join(repo.get('topics', []))}".lower()
    score += min(25.0, sum(1 for kw in config.keywords if kw.lower() in haystack) * 5.0)
    score += min(15.0, sum(1 for t in config.github_topics if t.lower() in [x.lower() for x in repo.get("topics", [])]) * 5.0)
    lang = (repo.get("language") or "").lower()
    if any(l.lower() == lang for l in config.github_languages):
        score += 5.0
    created_at = repo.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            age_hours = max((datetime.now(UTC) - created).total_seconds() / 3600.0, 0.0)
            score += max(0.0, 20.0 - min(age_hours, 20.0))
        except ValueError:
            pass
    return round(score, 1)


# ---------------------------------------------------------------------------
# Source: Reddit
# ---------------------------------------------------------------------------

def reddit_scan(config: CalloutConfig, hours: int, limit: int) -> list[dict]:
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    results = []
    for sub in config.subreddits:
        data = _get_json(f"{REDDIT_BASE}/r/{sub}/hot.json?limit=25&raw_json=1")
        if not data or "data" not in data:
            continue
        for child in data["data"].get("children", []):
            post = child.get("data", {})
            created = post.get("created_utc", 0)
            if created < cutoff or post.get("stickied"):
                continue
            title = post.get("title", "")
            selftext = (post.get("selftext") or "")[:500]
            score = int(post.get("score", 0))
            comments = int(post.get("num_comments", 0))
            haystack = f"{title} {selftext}".lower()
            kw_hits = sum(1 for kw in config.keywords if kw.lower() in haystack)
            if kw_hits == 0 and sub not in ("MachineLearning", "LocalLLaMA"):
                continue
            results.append({
                "source": f"r/{sub}", "title": _clean_text(title),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "description": _clean_text(selftext[:200]),
                "score": round(score * 0.3 + comments * 0.5 + kw_hits * 10, 1),
                "stars": score, "language": "", "topics": [],
                "created": datetime.fromtimestamp(created, tz=UTC).isoformat(),
                "meta": {"comments": comments, "subreddit": sub, "upvote_ratio": post.get("upvote_ratio", 0)},
            })
        time.sleep(0.5)
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


def reddit_search(config: CalloutConfig, query: str, limit: int = 15, time_filter: str = "month") -> list[dict]:
    """Search Reddit across AI subreddits."""
    params = urllib.parse.urlencode({
        "q": query, "sort": "relevance", "t": time_filter,
        "limit": str(min(limit, 25)), "raw_json": "1",
    })
    results = []
    for sub in config.subreddits[:6]:
        data = _get_json(f"{REDDIT_BASE}/r/{sub}/search.json?{params}&restrict_sr=on")
        if not data or "data" not in data:
            continue
        for child in data["data"].get("children", []):
            post = child.get("data", {})
            results.append({
                "subreddit": post.get("subreddit", ""),
                "title": _clean_text(post.get("title", "")),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "score": int(post.get("score", 0)),
                "comments": int(post.get("num_comments", 0)),
                "created": datetime.fromtimestamp(post.get("created_utc", 0), tz=UTC).isoformat(),
                "selftext_preview": _clean_text((post.get("selftext") or "")[:300]),
                "upvote_ratio": post.get("upvote_ratio", 0),
                "flair": post.get("link_flair_text") or "",
            })
        time.sleep(0.5)
    results.sort(key=lambda x: -(x["score"] + x["comments"] * 2))
    return results[:limit]


def reddit_comments(url: str, limit: int = 30) -> list[dict]:
    """Fetch top comments from a Reddit thread for sentiment analysis."""
    json_url = url.rstrip("/") + ".json?raw_json=1&limit=50"
    data = _get_json(json_url)
    if not data or not isinstance(data, list) or len(data) < 2:
        return []
    comments_data = data[1].get("data", {}).get("children", [])
    results = []
    for child in comments_data:
        comment = child.get("data", {})
        if child.get("kind") != "t1":
            continue
        body = comment.get("body", "")
        if not body or body == "[deleted]":
            continue
        results.append({
            "body": _clean_text(body[:500]),
            "score": int(comment.get("score", 0)),
            "author": comment.get("author", ""),
        })
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ---------------------------------------------------------------------------
# Source: Hacker News
# ---------------------------------------------------------------------------

def hn_scan(config: CalloutConfig, hours: int, limit: int) -> list[dict]:
    cutoff = int((datetime.now(UTC) - timedelta(hours=hours)).timestamp())
    search_terms = ["AI", "LLM", "GPT", "Claude", "open source AI", "agent framework", "local model", "Ollama"]
    results = []
    seen_ids: set = set()
    for term in search_terms:
        params = urllib.parse.urlencode({
            "query": term, "tags": "story",
            "numericFilters": f"created_at_i>{cutoff}", "hitsPerPage": "20",
        })
        data = _get_json(f"{HN_ALGOLIA}/search_by_date?{params}")
        if not data or not isinstance(data.get("hits"), list):
            continue
        for hit in data["hits"]:
            obj_id = hit.get("objectID")
            if obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)
            points = int(hit.get("points") or 0)
            comments = int(hit.get("num_comments") or 0)
            results.append({
                "source": "hackernews", "title": _clean_text(hit.get("title", "")),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}",
                "description": "", "score": round(points * 0.4 + comments * 0.6, 1),
                "stars": points, "language": "", "topics": [],
                "created": hit.get("created_at", ""),
                "meta": {"comments": comments, "hn_id": obj_id},
            })
        time.sleep(0.3)
    results.sort(key=lambda x: -x["score"])
    return results[:limit]


def hn_search(config: CalloutConfig, query: str, limit: int = 15) -> list[dict]:
    params = urllib.parse.urlencode({"query": query, "tags": "story", "hitsPerPage": str(min(limit, 30))})
    data = _get_json(f"{HN_ALGOLIA}/search?{params}")
    if not data or not isinstance(data.get("hits"), list):
        return []
    return [
        {
            "title": h.get("title", ""),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID', '')}",
            "hn_url": f"https://news.ycombinator.com/item?id={h.get('objectID', '')}",
            "points": int(h.get("points") or 0),
            "comments": int(h.get("num_comments") or 0),
            "created": h.get("created_at", ""),
            "author": h.get("author", ""),
        }
        for h in data["hits"][:limit]
    ]


def hn_comments(story_id: str, limit: int = 30) -> list[dict]:
    """Fetch comments for an HN story for sentiment analysis."""
    data = _get_json(f"{HN_ALGOLIA}/search?tags=comment,story_{story_id}&hitsPerPage={limit}")
    if not data or not isinstance(data.get("hits"), list):
        return []
    return [
        {
            "body": _clean_text((h.get("comment_text") or "")[:500]),
            "author": h.get("author", ""),
            "points": int(h.get("points") or 0),
        }
        for h in data["hits"] if h.get("comment_text")
    ][:limit]


# ---------------------------------------------------------------------------
# Source: ArXiv
# ---------------------------------------------------------------------------

def arxiv_search(query: str, limit: int = 10) -> list[dict]:
    """Search ArXiv for recent AI papers."""
    params = urllib.parse.urlencode({
        "search_query": f"all:{query} AND (cat:cs.AI OR cat:cs.CL OR cat:cs.LG)",
        "sortBy": "submittedDate", "sortOrder": "descending",
        "max_results": str(min(limit, 20)),
    })
    xml = _get_text(f"{ARXIV_API}?{params}")
    if not xml:
        return []

    results = []
    # Simple XML parsing without lxml
    entries = re.findall(r"<entry>(.*?)</entry>", xml, re.DOTALL)
    for entry in entries:
        title = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        summary = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
        link = re.search(r'<id>(.*?)</id>', entry)
        published = re.search(r"<published>(.*?)</published>", entry)
        authors = re.findall(r"<name>(.*?)</name>", entry)

        if title:
            results.append({
                "title": _clean_text(title.group(1)),
                "url": link.group(1) if link else "",
                "summary": _clean_text(summary.group(1))[:300] if summary else "",
                "published": published.group(1) if published else "",
                "authors": authors[:3],
            })
    return results


# ---------------------------------------------------------------------------
# Source: Dev.to
# ---------------------------------------------------------------------------

def devto_search(query: str, limit: int = 10) -> list[dict]:
    """Search Dev.to for AI articles."""
    params = urllib.parse.urlencode({"tag": query.replace(" ", ""), "per_page": str(min(limit, 20))})
    data = _get_json(f"{DEVTO_API}/articles?{params}")
    if not data or not isinstance(data, list):
        # Fallback: search by query string
        params2 = urllib.parse.urlencode({"per_page": str(min(limit, 20))})
        data = _get_json(f"{DEVTO_API}/articles/latest?{params2}")
        if not data or not isinstance(data, list):
            return []

    results = []
    for article in data[:limit]:
        title = article.get("title", "")
        # Filter for AI relevance
        haystack = f"{title} {article.get('description', '')} {' '.join(article.get('tag_list', []))}".lower()
        if not any(kw in haystack for kw in ["ai", "llm", "gpt", "claude", "machine learning", "agent", "model"]):
            continue
        results.append({
            "title": title,
            "url": article.get("url", ""),
            "description": (article.get("description") or "")[:200],
            "reactions": article.get("positive_reactions_count", 0),
            "comments": article.get("comments_count", 0),
            "published": article.get("published_at", ""),
            "author": article.get("user", {}).get("username", ""),
            "tags": article.get("tag_list", []),
            "reading_time": article.get("reading_time_minutes", 0),
        })
    return results


# ---------------------------------------------------------------------------
# Competitive analysis
# ---------------------------------------------------------------------------

def compete(config: CalloutConfig, query: str, limit: int = 15) -> dict:
    """Find competing repos and compare them."""
    print("  Searching for competitors...", flush=True)

    # Search GitHub
    params = urllib.parse.urlencode({
        "q": f"{query} in:name,description,readme",
        "sort": "stars", "order": "desc", "per_page": str(min(limit * 2, 50)),
    })
    data = _get_json(f"{GITHUB_API}/search/repositories?{params}", headers=_github_headers(config))
    repos = data.get("items", []) if data else []

    competitors = []
    for repo in repos[:limit]:
        detail = {
            "name": repo.get("full_name", ""),
            "url": repo.get("html_url", ""),
            "description": (repo.get("description") or "")[:200],
            "stars": int(repo.get("stargazers_count", 0)),
            "forks": int(repo.get("forks_count", 0)),
            "open_issues": int(repo.get("open_issues_count", 0)),
            "language": repo.get("language") or "",
            "topics": repo.get("topics", [])[:8],
            "created": repo.get("created_at", ""),
            "updated": repo.get("updated_at", ""),
            "pushed": repo.get("pushed_at", ""),
            "license": (repo.get("license") or {}).get("spdx_id", ""),
            "size_kb": repo.get("size", 0),
        }

        # Calculate age in days
        try:
            created = datetime.fromisoformat(detail["created"].replace("Z", "+00:00"))
            detail["age_days"] = (datetime.now(UTC) - created).days
        except (ValueError, TypeError):
            detail["age_days"] = 0

        # Stars per day (growth velocity)
        if detail["age_days"] > 0:
            detail["stars_per_day"] = round(detail["stars"] / detail["age_days"], 2)
        else:
            detail["stars_per_day"] = detail["stars"]

        competitors.append(detail)

    # Sort by stars_per_day (momentum) not just absolute stars
    competitors.sort(key=lambda x: -x.get("stars_per_day", 0))

    # Reddit sentiment for top 3
    print("  Checking Reddit sentiment...", flush=True)
    sentiment = {}
    for comp in competitors[:3]:
        name = comp["name"].split("/")[-1]
        threads = reddit_search(config, name, limit=5, time_filter="year")
        if threads:
            total_score = sum(t["score"] for t in threads)
            total_comments = sum(t["comments"] for t in threads)
            sentiment[comp["name"]] = {
                "threads": len(threads),
                "total_upvotes": total_score,
                "total_comments": total_comments,
                "top_thread": threads[0]["title"] if threads else "",
            }
        time.sleep(0.5)

    return {
        "query": query,
        "competitors": competitors,
        "reddit_sentiment": sentiment,
    }


# ---------------------------------------------------------------------------
# Sentiment analysis
# ---------------------------------------------------------------------------

def sentiment(config: CalloutConfig, query: str, limit: int = 20) -> dict:
    """Gauge community sentiment on a topic from Reddit + HN."""
    print(f"  Analyzing sentiment for: {query}", flush=True)

    # Get Reddit threads
    threads = reddit_search(config, query, limit=limit, time_filter="month")

    # Get top comments from highest-engagement threads
    all_comments = []
    for thread in threads[:5]:
        comments = reddit_comments(thread["url"])
        for c in comments[:10]:
            c["source"] = f"r/{thread.get('subreddit', '')}"
            c["thread_title"] = thread["title"]
        all_comments.extend(comments[:10])
        time.sleep(0.5)

    # HN
    hn_stories = hn_search(config, query, limit=10)
    for story in hn_stories[:3]:
        hn_id = story.get("hn_url", "").split("id=")[-1]
        if hn_id:
            comments = hn_comments(hn_id, limit=10)
            for c in comments:
                c["source"] = "hackernews"
                c["thread_title"] = story["title"]
            all_comments.extend(comments)
        time.sleep(0.3)

    # Simple sentiment heuristics
    positive_words = {"great", "amazing", "love", "excellent", "awesome", "best", "impressive",
                      "useful", "helpful", "powerful", "fast", "easy", "clean", "solid", "beautiful"}
    negative_words = {"bad", "terrible", "awful", "hate", "broken", "slow", "buggy", "waste",
                      "expensive", "frustrating", "useless", "garbage", "overrated", "disappointing"}

    pos_count = 0
    neg_count = 0
    neutral_count = 0
    keyword_freq: dict[str, int] = {}

    for comment in all_comments:
        body_lower = comment.get("body", "").lower()
        has_pos = any(w in body_lower for w in positive_words)
        has_neg = any(w in body_lower for w in negative_words)

        if has_pos and not has_neg:
            pos_count += 1
            comment["sentiment"] = "positive"
        elif has_neg and not has_pos:
            neg_count += 1
            comment["sentiment"] = "negative"
        else:
            neutral_count += 1
            comment["sentiment"] = "neutral" if not has_pos else "mixed"

        # Keyword frequency
        for kw in AI_KEYWORDS[:30]:
            if kw.lower() in body_lower:
                keyword_freq[kw] = keyword_freq.get(kw, 0) + 1

    total = pos_count + neg_count + neutral_count
    top_keywords = sorted(keyword_freq.items(), key=lambda x: -x[1])[:10]

    return {
        "query": query,
        "threads_analyzed": len(threads),
        "comments_analyzed": len(all_comments),
        "sentiment": {
            "positive": pos_count,
            "negative": neg_count,
            "neutral": neutral_count,
            "ratio": round(pos_count / max(total, 1), 2),
            "label": "positive" if pos_count > neg_count * 1.5 else ("negative" if neg_count > pos_count * 1.5 else "mixed"),
        },
        "top_keywords": top_keywords,
        "top_positive": [c for c in all_comments if c.get("sentiment") == "positive"][:5],
        "top_negative": [c for c in all_comments if c.get("sentiment") == "negative"][:5],
        "threads": threads[:10],
        "hn_stories": hn_stories[:5],
    }


# ---------------------------------------------------------------------------
# Repo tracking
# ---------------------------------------------------------------------------

def track_add(config: CalloutConfig, repo: str) -> str:
    repo = repo.strip().lstrip("/")
    if "github.com/" in repo:
        match = re.search(r"github\.com/([^/]+/[^/\s?#]+)", repo)
        repo = match.group(1) if match else repo

    detail = _get_json(f"{GITHUB_API}/repos/{repo}", headers=_github_headers(config))
    if not detail:
        return f"Could not fetch {repo}"

    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO tracked_repos (repo, added_at, last_checked, last_stars, last_data) VALUES (?,?,?,?,?)",
        (repo, time.time(), time.time(), detail.get("stargazers_count", 0), json.dumps(detail)[:10000]),
    )
    conn.commit()
    conn.close()
    return f"Now tracking {repo} ({detail.get('stargazers_count', 0)} stars)"


def track_list() -> str:
    conn = _get_db()
    rows = conn.execute("SELECT repo, added_at, last_checked, last_stars FROM tracked_repos ORDER BY repo").fetchall()
    conn.close()
    if not rows:
        return "No repos being tracked. Use: callout track add owner/repo"
    lines = ["\n  TRACKED REPOS", f"  {'Repo':<40} {'Stars':>8} {'Added':>12} {'Last Check':>12}"]
    lines.append(f"  {'-'*40} {'-'*8} {'-'*12} {'-'*12}")
    for repo, added, checked, stars in rows:
        added_dt = datetime.fromtimestamp(added, tz=UTC).strftime("%Y-%m-%d")
        checked_dt = datetime.fromtimestamp(checked, tz=UTC).strftime("%Y-%m-%d") if checked else "never"
        lines.append(f"  {repo:<40} {stars:>8} {added_dt:>12} {checked_dt:>12}")
    return "\n".join(lines)


def track_check(config: CalloutConfig) -> str:
    conn = _get_db()
    rows = conn.execute("SELECT repo, last_stars, last_data FROM tracked_repos").fetchall()
    if not rows:
        conn.close()
        return "No repos being tracked."

    lines = ["\n  TRACKING UPDATE"]
    alerts = []
    for repo, old_stars, last_data_str in rows:
        detail = _get_json(f"{GITHUB_API}/repos/{repo}", headers=_github_headers(config))
        if not detail:
            lines.append(f"  {repo}: fetch failed")
            continue

        new_stars = detail.get("stargazers_count", 0)
        star_delta = new_stars - old_stars
        pushed = detail.get("pushed_at", "")

        lines.append(f"  {repo}: {new_stars:,} stars ({'+' if star_delta >= 0 else ''}{star_delta})")

        # Check for significant changes
        if star_delta > 100:
            alert_msg = f"{repo} gained {star_delta} stars since last check!"
            alerts.append(alert_msg)
            conn.execute(
                "INSERT INTO alerts (repo, alert_type, message, timestamp) VALUES (?,?,?,?)",
                (repo, "star_spike", alert_msg, time.time()),
            )

        # Check for new releases
        try:
            old_data = json.loads(last_data_str) if last_data_str else {}
        except json.JSONDecodeError:
            old_data = {}

        releases = _get_json(f"{GITHUB_API}/repos/{repo}/releases?per_page=1", headers=_github_headers(config)) or []
        if releases and isinstance(releases, list):
            latest = releases[0]
            old_latest = old_data.get("latest_release", "")
            if latest.get("tag_name") and latest["tag_name"] != old_latest:
                alert_msg = f"{repo} released {latest['tag_name']}"
                alerts.append(alert_msg)
                lines.append(f"    NEW RELEASE: {latest['tag_name']}")
                detail["latest_release"] = latest["tag_name"]

        conn.execute(
            "UPDATE tracked_repos SET last_checked=?, last_stars=?, last_data=? WHERE repo=?",
            (time.time(), new_stars, json.dumps(detail)[:10000], repo),
        )
        time.sleep(0.5)

    conn.commit()
    conn.close()

    if alerts:
        lines.append(f"\n  ALERTS ({len(alerts)}):")
        for a in alerts:
            lines.append(f"    ! {a}")
        # Save alerts to Hyphae
        _hyphae_save(config, f"Callout tracking alerts: {'; '.join(alerts)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def cmd_history(days: int = 7) -> str:
    conn = _get_db()
    cutoff = time.time() - (days * 86400)
    rows = conn.execute(
        "SELECT scan_type, query, timestamp, result_count FROM scans WHERE timestamp > ? ORDER BY timestamp DESC LIMIT 50",
        (cutoff,),
    ).fetchall()
    conn.close()
    if not rows:
        return f"No scans in the last {days} days."
    lines = [f"\n  CALLOUT HISTORY (last {days} days)"]
    lines.append(f"  {'Type':<12} {'Query':<40} {'Results':>8} {'When'}")
    lines.append(f"  {'-'*12} {'-'*40} {'-'*8} {'-'*20}")
    for stype, query, ts, count in rows:
        dt = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
        lines.append(f"  {stype:<12} {(query or '')[:40]:<40} {count:>8} {dt}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_pulse(items: list[dict], title: str = "") -> str:
    if not items:
        return "  (no results)\n"
    lines = []
    if title:
        lines.append(f"\n{'='*60}")
        lines.append(f"  {title}")
        lines.append(f"{'='*60}")
    for i, item in enumerate(items, 1):
        src = item["source"]
        stars_label = "pts" if src == "hackernews" else ("upvotes" if "r/" in src else "stars")
        stars = item.get("stars", 0)
        lines.append(f"\n  {i}. [{src}] {item['title']}")
        lines.append(f"     {item['url']}")
        if item.get("description"):
            lines.append(f"     {item['description'][:120]}")
        meta_parts = []
        if stars:
            meta_parts.append(f"{stars} {stars_label}")
        if item.get("meta", {}).get("comments"):
            meta_parts.append(f"{item['meta']['comments']} comments")
        if item.get("language"):
            meta_parts.append(item["language"])
        if meta_parts:
            lines.append(f"     [{', '.join(meta_parts)}]")
    return "\n".join(lines)


def format_research(brief: dict) -> str:
    lines = [
        f"\n{'='*60}",
        f"  CALLOUT — RESEARCH BRIEF",
        f"  Query: {brief['query']}",
        f"  Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"{'='*60}",
    ]
    sections = brief.get("sections", {})

    if "repo" in sections:
        r = sections["repo"]
        if "error" in r:
            lines.append(f"\n  Error: {r['error']}")
        else:
            lines.append(f"\n--- REPOSITORY: {r.get('name', '')} ---")
            lines.append(f"  {r.get('description', '')}")
            lines.append(f"  Stars: {r.get('stars', 0):,} | Forks: {r.get('forks', 0):,} | Issues: {r.get('open_issues', 0)} | License: {r.get('license', 'N/A')}")
            lines.append(f"  Language: {r.get('language', '')} | Topics: {', '.join(r.get('topics', []))}")
            lines.append(f"  Created: {r.get('created', '')[:10]} | Updated: {r.get('updated', '')[:10]} | Last push: {r.get('pushed', '')[:10]}")
            if r.get("homepage"):
                lines.append(f"  Homepage: {r['homepage']}")

            v = r.get("velocity", {})
            if v:
                lines.append(f"\n  Velocity: {v.get('avg_weekly_commits', 0)} commits/week (last 4 weeks: {v.get('last_4_weeks', [])})")
                lines.append(f"  Contributors: {v.get('total_contributors', 0)}")

            if r.get("top_contributors"):
                contribs = ", ".join(f"{c['login']} ({c['contributions']})" for c in r["top_contributors"][:5])
                lines.append(f"  Top contributors: {contribs}")

            if r.get("recent_releases"):
                lines.append(f"\n  Releases:")
                for rel in r["recent_releases"][:3]:
                    lines.append(f"    {rel['tag']} — {rel.get('name', '')} ({rel.get('published', '')[:10]})")
                    if rel.get("body"):
                        lines.append(f"      {rel['body'][:200]}")

            if r.get("recent_commits"):
                lines.append(f"\n  Recent commits:")
                for c in r["recent_commits"][:5]:
                    lines.append(f"    {c['sha']} {c['message']} ({c.get('author', '')})")

            if r.get("recent_issues"):
                prs = [i for i in r["recent_issues"] if i.get("is_pr")]
                issues = [i for i in r["recent_issues"] if not i.get("is_pr")]
                if issues:
                    lines.append(f"\n  Issues ({len(issues)}):")
                    for iss in issues[:5]:
                        state = "OPEN" if iss["state"] == "open" else "closed"
                        lines.append(f"    [{state}] {iss['title']} ({iss['comments']} comments)")
                if prs:
                    lines.append(f"\n  Pull Requests ({len(prs)}):")
                    for pr in prs[:5]:
                        state = "OPEN" if pr["state"] == "open" else "merged"
                        lines.append(f"    [{state}] {pr['title']}")

            if r.get("readme_preview"):
                lines.append(f"\n  README (first 600 chars):")
                for rline in r["readme_preview"][:600].split("\n")[:15]:
                    lines.append(f"    {rline}")

    if "github_repos" in sections and sections["github_repos"]:
        lines.append(f"\n--- GITHUB REPOS ({len(sections['github_repos'])} found) ---")
        for r in sections["github_repos"][:12]:
            lines.append(f"  {r['name']} ({r.get('stars', 0):,} stars, {r.get('language', '')})")
            lines.append(f"    {r['url']}")
            if r.get("description"):
                lines.append(f"    {r['description'][:140]}")

    if "arxiv" in sections and sections["arxiv"]:
        lines.append(f"\n--- ARXIV PAPERS ({len(sections['arxiv'])} found) ---")
        for p in sections["arxiv"][:8]:
            lines.append(f"  {p['title']}")
            lines.append(f"    {p['url']}")
            if p.get("summary"):
                lines.append(f"    {p['summary'][:150]}")
            if p.get("authors"):
                lines.append(f"    Authors: {', '.join(p['authors'][:3])}")

    if "devto" in sections and sections["devto"]:
        lines.append(f"\n--- DEV.TO ARTICLES ({len(sections['devto'])} found) ---")
        for a in sections["devto"][:6]:
            lines.append(f"  {a['title']} ({a.get('reactions', 0)} reactions, {a.get('reading_time', 0)}min)")
            lines.append(f"    {a['url']}")

    if "reddit" in sections and sections["reddit"]:
        lines.append(f"\n--- REDDIT ({len(sections['reddit'])} found) ---")
        for r in sections["reddit"][:10]:
            lines.append(f"  [{r.get('subreddit', '')}] {r['title']} ({r['score']} pts, {r['comments']} comments)")
            lines.append(f"    {r['url']}")
            if r.get("selftext_preview"):
                lines.append(f"    {r['selftext_preview'][:140]}")

    if "hackernews" in sections and sections["hackernews"]:
        lines.append(f"\n--- HACKER NEWS ({len(sections['hackernews'])} found) ---")
        for h in sections["hackernews"][:8]:
            lines.append(f"  {h['title']} ({h.get('points', 0)} pts, {h.get('comments', 0)} comments)")
            lines.append(f"    {h['url']}")
            lines.append(f"    Discussion: {h['hn_url']}")

    return "\n".join(lines)


def format_compete(result: dict) -> str:
    lines = [
        f"\n{'='*60}",
        f"  CALLOUT — COMPETITIVE ANALYSIS",
        f"  Query: {result['query']}",
        f"  Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"{'='*60}",
    ]

    comps = result.get("competitors", [])
    lines.append(f"\n  {'Repo':<35} {'Stars':>8} {'Growth/d':>9} {'Age':>6} {'Issues':>7} {'Lang':<10}")
    lines.append(f"  {'-'*35} {'-'*8} {'-'*9} {'-'*6} {'-'*7} {'-'*10}")

    for c in comps[:15]:
        name = c["name"][:35]
        lines.append(
            f"  {name:<35} {c['stars']:>8,} {c.get('stars_per_day', 0):>8.1f} "
            f"{c.get('age_days', 0):>5}d {c.get('open_issues', 0):>7} {c.get('language', ''):>10}"
        )

    sent = result.get("reddit_sentiment", {})
    if sent:
        lines.append(f"\n  REDDIT SENTIMENT (top 3):")
        for repo, s in sent.items():
            lines.append(f"  {repo}: {s['threads']} threads, {s['total_upvotes']} upvotes, {s['total_comments']} comments")
            if s.get("top_thread"):
                lines.append(f"    Top: {s['top_thread'][:80]}")

    return "\n".join(lines)


def format_sentiment(result: dict) -> str:
    s = result["sentiment"]
    lines = [
        f"\n{'='*60}",
        f"  CALLOUT — SENTIMENT ANALYSIS",
        f"  Topic: {result['query']}",
        f"  Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"{'='*60}",
        f"\n  Threads analyzed: {result['threads_analyzed']}",
        f"  Comments analyzed: {result['comments_analyzed']}",
        f"\n  SENTIMENT: {s['label'].upper()} (ratio: {s['ratio']:.0%})",
        f"    Positive: {s['positive']}  |  Negative: {s['negative']}  |  Neutral: {s['neutral']}",
    ]

    # Sentiment bar
    total = s["positive"] + s["negative"] + s["neutral"]
    if total > 0:
        bar_len = 40
        pos_len = int(s["positive"] / total * bar_len)
        neg_len = int(s["negative"] / total * bar_len)
        neu_len = bar_len - pos_len - neg_len
        lines.append(f"    [{'+'*pos_len}{'.'*neu_len}{'-'*neg_len}]")

    if result.get("top_keywords"):
        kw_str = ", ".join(f"{k} ({v})" for k, v in result["top_keywords"][:8])
        lines.append(f"\n  Top keywords: {kw_str}")

    if result.get("top_positive"):
        lines.append(f"\n  TOP POSITIVE COMMENTS:")
        for c in result["top_positive"][:3]:
            lines.append(f"    [{c.get('source', '')}] ({c.get('score', 0)} pts) {c['body'][:150]}")

    if result.get("top_negative"):
        lines.append(f"\n  TOP NEGATIVE COMMENTS:")
        for c in result["top_negative"][:3]:
            lines.append(f"    [{c.get('source', '')}] ({c.get('score', 0)} pts) {c['body'][:150]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_pulse(config: CalloutConfig, hours: int, limit: int, as_json: bool = False) -> str:
    output_parts = [
        f"\n{'='*60}",
        f"  CALLOUT — PULSE",
        f"  Window: last {hours}h | Limit: {limit} per source",
        f"  Scanned: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"{'='*60}",
    ]

    print("  Scanning GitHub new repos...", flush=True)
    gh_new = github_scan(config, hours, limit)
    output_parts.append(format_pulse(gh_new, "GITHUB — NEW REPOS"))

    print("  Scanning GitHub trending...", flush=True)
    gh_trending = github_trending(config, limit=15)
    output_parts.append(format_pulse(gh_trending, "GITHUB — TRENDING AI"))

    print("  Scanning Reddit...", flush=True)
    reddit_results = reddit_scan(config, hours, limit)
    output_parts.append(format_pulse(reddit_results, "REDDIT — AI DISCUSSIONS"))

    print("  Scanning Hacker News...", flush=True)
    hn_results = hn_scan(config, hours, limit)
    output_parts.append(format_pulse(hn_results, "HACKER NEWS — AI STORIES"))

    print("  Scanning ArXiv...", flush=True)
    arxiv_results = arxiv_search("large language model agent", limit=8)
    if arxiv_results:
        arxiv_lines = [f"\n{'='*60}", f"  ARXIV — RECENT PAPERS", f"{'='*60}"]
        for i, p in enumerate(arxiv_results, 1):
            arxiv_lines.append(f"\n  {i}. {p['title']}")
            arxiv_lines.append(f"     {p['url']}")
            if p.get("summary"):
                arxiv_lines.append(f"     {p['summary'][:120]}")
        output_parts.append("\n".join(arxiv_lines))

    full_output = "\n".join(output_parts)
    total = len(gh_new) + len(gh_trending) + len(reddit_results) + len(hn_results) + len(arxiv_results)
    full_output += (
        f"\n{'='*60}\n"
        f"  TOTALS: {len(gh_new)} new repos, {len(gh_trending)} trending, "
        f"{len(reddit_results)} Reddit, {len(hn_results)} HN, {len(arxiv_results)} ArXiv "
        f"({total} items)\n{'='*60}"
    )

    _save_scan("pulse", f"last_{hours}h", {"counts": {
        "github_new": len(gh_new), "github_trending": len(gh_trending),
        "reddit": len(reddit_results), "hn": len(hn_results), "arxiv": len(arxiv_results),
    }}, total)

    # Hyphae
    top_items = sorted(gh_new + reddit_results + hn_results, key=lambda x: -x["score"])[:5]
    if top_items:
        _hyphae_save(config,
            f"Callout pulse {datetime.now(UTC).strftime('%Y-%m-%d')}: "
            f"top: {'; '.join(i['title'][:60] for i in top_items)}"
        )

    if as_json:
        return json.dumps({
            "github_new": gh_new, "github_trending": gh_trending,
            "reddit": reddit_results, "hn": hn_results, "arxiv": arxiv_results,
        }, indent=2)

    return full_output


def cmd_research(config: CalloutConfig, query: str, limit: int = 15, depth: int = 2) -> str:
    """Research with configurable depth.

    Depth levels:
      1 — Quick: GitHub search + Reddit (fast, ~10s)
      2 — Standard: + HN + ArXiv (default, ~20s)
      3 — Deep: + Dev.to + more subreddits + more results + competitor scan
      4 — Thorough: + fetch README for top repos + sentiment on top threads
      5 — Exhaustive: all of the above + cross-reference findings + comment analysis
    """
    depth_names = {1: "QUICK", 2: "STANDARD", 3: "DEEP", 4: "THOROUGH", 5: "EXHAUSTIVE"}
    depth = max(1, min(5, depth))
    depth_limit = {1: min(limit, 8), 2: limit, 3: limit * 2, 4: limit * 2, 5: limit * 3}
    eff_limit = depth_limit[depth]

    print(f"  Researching [{depth_names[depth]}]: {query}", flush=True)
    is_github_url = "github.com/" in query
    brief: dict[str, Any] = {"query": query, "depth": depth, "depth_name": depth_names[depth], "sections": {}}

    # --- GitHub ---
    if is_github_url:
        match = re.search(r"github\.com/([^/]+/[^/\s?#]+)", query)
        repo_path = match.group(1).rstrip("/") if match else ""
        if repo_path:
            brief["sections"]["repo"] = github_repo_detail(config, repo_path)
        search_term = repo_path.split("/")[-1] if repo_path else query
    else:
        search_term = query
        print("  Searching GitHub repos...", flush=True)
        params = urllib.parse.urlencode({
            "q": f"{query} in:name,description,readme",
            "sort": "stars", "order": "desc", "per_page": str(min(eff_limit, 50)),
        })
        data = _get_json(f"{GITHUB_API}/search/repositories?{params}", headers=_github_headers(config))
        if data and isinstance(data.get("items"), list):
            brief["sections"]["github_repos"] = [
                {
                    "name": r.get("full_name", ""), "url": r.get("html_url", ""),
                    "description": (r.get("description") or "")[:200],
                    "stars": r.get("stargazers_count", 0), "forks": r.get("forks_count", 0),
                    "language": r.get("language") or "", "topics": r.get("topics", [])[:5],
                    "updated": r.get("updated_at", ""),
                }
                for r in data["items"][:eff_limit]
            ]

    # --- Reddit (all depths) ---
    print("  Searching Reddit...", flush=True)
    reddit_subs = config.subreddits[:4] if depth <= 2 else config.subreddits[:8]
    time_filter = "month" if depth <= 3 else "year"
    # Temporarily override config subreddits for wider search
    orig_subs = config.subreddits
    config.subreddits = reddit_subs
    brief["sections"]["reddit"] = reddit_search(config, search_term, eff_limit, time_filter)
    config.subreddits = orig_subs

    # --- HN (depth 2+) ---
    if depth >= 2:
        print("  Searching Hacker News...", flush=True)
        brief["sections"]["hackernews"] = hn_search(config, search_term, eff_limit)

    # --- ArXiv (depth 2+) ---
    if depth >= 2:
        print("  Searching ArXiv...", flush=True)
        brief["sections"]["arxiv"] = arxiv_search(search_term, limit=min(eff_limit, 12))

    # --- Dev.to (depth 3+) ---
    if depth >= 3:
        print("  Searching Dev.to...", flush=True)
        brief["sections"]["devto"] = devto_search(search_term, limit=min(eff_limit, 10))

    # --- Competitor scan (depth 3+, non-URL queries only) ---
    if depth >= 3 and not is_github_url:
        print("  Running competitive scan...", flush=True)
        comp_result = compete(config, search_term, limit=10)
        brief["sections"]["competitors"] = comp_result.get("competitors", [])[:10]

    # --- Fetch README for top GitHub repos (depth 4+) ---
    if depth >= 4 and "github_repos" in brief["sections"]:
        print("  Fetching READMEs for top repos...", flush=True)
        top_repos = brief["sections"]["github_repos"][:5]
        readme_details = []
        for repo_entry in top_repos:
            repo_name = repo_entry.get("name", "")
            if not repo_name:
                continue
            readme_data = _get_json(f"{GITHUB_API}/repos/{repo_name}/readme", headers=_github_headers(config))
            readme = ""
            if readme_data and readme_data.get("content"):
                try:
                    readme = base64.b64decode(readme_data["content"]).decode("utf-8", errors="replace")[:2000]
                except Exception:
                    pass
            if readme:
                readme_details.append({"repo": repo_name, "readme": readme})
            time.sleep(0.3)
        if readme_details:
            brief["sections"]["readmes"] = readme_details

    # --- Sentiment analysis on query (depth 4+) ---
    if depth >= 4:
        print("  Analyzing sentiment...", flush=True)
        sent_result = sentiment(config, search_term)
        brief["sections"]["sentiment"] = sent_result

    # --- Cross-reference: comment analysis from top threads (depth 5) ---
    if depth >= 5:
        print("  Pulling comments from top threads...", flush=True)
        all_comments = []
        # Top 5 Reddit threads
        for thread in brief["sections"].get("reddit", [])[:5]:
            comments = reddit_comments(thread["url"], limit=15)
            for c in comments:
                c["source_thread"] = thread["title"][:60]
                c["source"] = f"r/{thread.get('subreddit', '')}"
            all_comments.extend(comments)
            time.sleep(0.5)
        # Top 3 HN threads
        for story in brief["sections"].get("hackernews", [])[:3]:
            hn_id = story.get("hn_url", "").split("id=")[-1]
            if hn_id:
                comments = hn_comments(hn_id, limit=15)
                for c in comments:
                    c["source_thread"] = story["title"][:60]
                    c["source"] = "hackernews"
                all_comments.extend(comments)
            time.sleep(0.3)
        if all_comments:
            # Sort by score
            all_comments.sort(key=lambda x: -x.get("score", 0))
            brief["sections"]["top_comments"] = all_comments[:30]

    # --- Format output ---
    output = format_research(brief)

    # Append depth-specific sections
    extra_lines = []

    if "competitors" in brief["sections"] and brief["sections"]["competitors"]:
        extra_lines.append(f"\n--- COMPETITORS ({len(brief['sections']['competitors'])} found) ---")
        extra_lines.append(f"  {'Repo':<35} {'Stars':>8} {'Growth/d':>9} {'Lang':<10}")
        extra_lines.append(f"  {'-'*35} {'-'*8} {'-'*9} {'-'*10}")
        for c in brief["sections"]["competitors"][:10]:
            extra_lines.append(f"  {c['name'][:35]:<35} {c['stars']:>8,} {c.get('stars_per_day', 0):>8.1f} {c.get('language', ''):<10}")

    if "readmes" in brief["sections"]:
        extra_lines.append(f"\n--- README PREVIEWS ---")
        for rd in brief["sections"]["readmes"]:
            extra_lines.append(f"\n  [{rd['repo']}]")
            for line in rd["readme"][:600].split("\n")[:10]:
                extra_lines.append(f"    {line}")

    if "sentiment" in brief["sections"]:
        s = brief["sections"]["sentiment"].get("sentiment", {})
        extra_lines.append(f"\n--- SENTIMENT: {s.get('label', '?').upper()} ({s.get('ratio', 0):.0%} positive) ---")
        extra_lines.append(f"  Positive: {s.get('positive', 0)} | Negative: {s.get('negative', 0)} | Neutral: {s.get('neutral', 0)}")

    if "top_comments" in brief["sections"]:
        extra_lines.append(f"\n--- TOP COMMUNITY COMMENTS ({len(brief['sections']['top_comments'])} collected) ---")
        for c in brief["sections"]["top_comments"][:10]:
            src = c.get("source", "")
            score = c.get("score", 0)
            body = c.get("body", "")[:180]
            extra_lines.append(f"  [{src}] ({score} pts) {body}")

    if extra_lines:
        output += "\n" + "\n".join(extra_lines)

    total = sum(len(v) if isinstance(v, list) else (0 if isinstance(v, dict) and "error" in v else 1) for v in brief["sections"].values())
    _save_scan("research", query, {"depth": depth, "total": total}, total)

    _hyphae_save(config,
        f"Callout research [{depth_names[depth]}] '{query}': {total} results across "
        f"{len(brief['sections'])} sources"
    )

    return output


def cmd_compete(config: CalloutConfig, query: str, limit: int = 15) -> str:
    result = compete(config, query, limit)
    output = format_compete(result)
    _save_scan("compete", query, result, len(result.get("competitors", [])))
    _hyphae_save(config,
        f"Callout competitive analysis '{query}': {len(result.get('competitors', []))} competitors found. "
        f"Top: {', '.join(c['name'] for c in result.get('competitors', [])[:3])}"
    )
    return output


def cmd_sentiment(config: CalloutConfig, query: str) -> str:
    result = sentiment(config, query)
    output = format_sentiment(result)
    _save_scan("sentiment", query, result, result.get("comments_analyzed", 0))
    s = result["sentiment"]
    _hyphae_save(config,
        f"Callout sentiment '{query}': {s['label']} ({s['ratio']:.0%} positive), "
        f"{result['comments_analyzed']} comments from {result['threads_analyzed']} threads"
    )
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="callout",
        description="Callout — GitHub & community intelligence for AI development",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p_pulse = sub.add_parser("pulse", help="Scan all sources for recent AI developments")
    p_pulse.add_argument("--hours", type=int, default=None)
    p_pulse.add_argument("--limit", type=int, default=None)
    p_pulse.add_argument("--watch", action="store_true")
    p_pulse.add_argument("--json", action="store_true")
    p_pulse.add_argument("--config", type=str, default=None)

    p_research = sub.add_parser("research", help="Deep-dive research on a topic or repo")
    p_research.add_argument("query")
    p_research.add_argument("--limit", type=int, default=15)
    p_research.add_argument("--depth", type=int, default=2, choices=[1,2,3,4,5],
                            help="1=quick 2=standard 3=deep 4=thorough 5=exhaustive")
    p_research.add_argument("--config", type=str, default=None)

    p_compete = sub.add_parser("compete", help="Competitive analysis — find and compare similar repos")
    p_compete.add_argument("query")
    p_compete.add_argument("--limit", type=int, default=15)
    p_compete.add_argument("--config", type=str, default=None)

    p_sentiment = sub.add_parser("sentiment", help="Gauge community sentiment on a topic")
    p_sentiment.add_argument("query")
    p_sentiment.add_argument("--config", type=str, default=None)

    p_track = sub.add_parser("track", help="Track repos over time")
    p_track.add_argument("action", choices=["add", "list", "check"])
    p_track.add_argument("repo", nargs="?", default="")
    p_track.add_argument("--config", type=str, default=None)

    p_history = sub.add_parser("history", help="Browse past scans")
    p_history.add_argument("--days", type=int, default=7)

    sub.add_parser("config", help="Print example config")

    args = parser.parse_args()

    if args.command == "config":
        print(example_config())
        return
    if args.command is None:
        parser.print_help()
        return

    logging.basicConfig(level=logging.WARNING)
    config = CalloutConfig.from_file(getattr(args, "config", None))

    if args.command == "pulse":
        hours = args.hours or config.default_hours
        limit = args.limit or config.default_limit
        if args.watch:
            interval = config.watch_interval
            print(f"Callout watch mode — scanning every {interval}s (Ctrl+C to stop)")
            while True:
                print(cmd_pulse(config, hours, limit, args.json))
                print(f"\n  Next scan in {interval}s...")
                time.sleep(interval)
        else:
            print(cmd_pulse(config, hours, limit, args.json))

    elif args.command == "research":
        print(cmd_research(config, args.query, args.limit, args.depth))

    elif args.command == "compete":
        print(cmd_compete(config, args.query, args.limit))

    elif args.command == "sentiment":
        print(cmd_sentiment(config, args.query))

    elif args.command == "track":
        if args.action == "add":
            if not args.repo:
                print("Usage: callout track add owner/repo")
                return
            print(track_add(config, args.repo))
        elif args.action == "list":
            print(track_list())
        elif args.action == "check":
            print(track_check(config))

    elif args.command == "history":
        print(cmd_history(args.days))


def example_config() -> str:
    return """\
# Callout config — place at ~/.openkeel/callout.yaml
github:
  token_env: GITHUB_TOKEN
  languages: [Python, TypeScript, Rust, Go, C++, Java]
  topics: [ai, llm, agents, machine-learning, deep-learning, generative-ai, rag, nlp]

reddit:
  subreddits:
    - MachineLearning
    - LocalLLaMA
    - artificial
    - singularity
    - StableDiffusion
    - ChatGPT
    - ClaudeAI
    - opensource
    - selfhosted
    - coding
    - ExperiencedDevs

general:
  hyphae_url: http://127.0.0.1:8100
  default_hours: 24
  default_limit: 30
  watch_interval: 1800
  keywords:
    - llm
    - agent
    - inference
    - rag
    - fine-tune
    - open-source
    - local model
    - cursor
    - copilot
    - deepseek
    - qwen
  exclude_keywords:
    - awesome-list
    - tutorial
    - course
    - interview
"""


if __name__ == "__main__":
    main()
