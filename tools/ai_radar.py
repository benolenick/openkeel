#!/usr/bin/env python3
"""AI Radar — track new AI developments and deep-dive research.

Two modes:
  pulse    — scan GitHub trending, Reddit AI subs, and Hacker News for
             recent AI developments. Output a ranked briefing.
  research — deep-dive a topic or repo: GitHub details, Reddit threads,
             HN discussions, synthesized into a research brief.

Usage:
  python tools/ai_radar.py pulse [--hours 24] [--limit 30] [--watch]
  python tools/ai_radar.py research "browser-use agent framework"
  python tools/ai_radar.py research "https://github.com/owner/repo"
  python tools/ai_radar.py config   # print example config
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import re
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

DEFAULT_CONFIG_PATH = Path.home() / ".openkeel" / "ai_radar.yaml"
HYPHAE_URL = os.getenv("HYPHAE_URL", "http://127.0.0.1:8100")

AI_SUBREDDITS = [
    "MachineLearning",
    "LocalLLaMA",
    "artificial",
    "singularity",
    "StableDiffusion",
    "ChatGPT",
    "ClaudeAI",
    "opensource",
]

AI_KEYWORDS = [
    "llm", "gpt", "claude", "gemini", "llama", "mistral", "agent", "rag",
    "fine-tune", "finetune", "inference", "transformer", "diffusion",
    "multimodal", "embedding", "vector", "mcp", "tool-use", "function-calling",
    "open-source", "self-hosted", "local model", "quantization", "gguf",
    "lora", "rlhf", "dpo", "moe", "mixture of experts", "benchmark",
    "context window", "reasoning", "chain-of-thought", "code generation",
    "browser automation", "computer use", "agentic", "openai", "anthropic",
    "huggingface", "vllm", "ollama", "groq", "together.ai",
]

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class RadarConfig:
    github_token_env: str = "GITHUB_TOKEN"
    hyphae_url: str = HYPHAE_URL
    subreddits: list[str] = field(default_factory=lambda: list(AI_SUBREDDITS))
    keywords: list[str] = field(default_factory=lambda: list(AI_KEYWORDS))
    github_languages: list[str] = field(default_factory=lambda: ["Python", "TypeScript", "Rust", "Go", "C++"])
    github_topics: list[str] = field(default_factory=lambda: [
        "ai", "llm", "agents", "machine-learning", "deep-learning",
        "generative-ai", "rag", "langchain", "transformers",
    ])
    exclude_keywords: list[str] = field(default_factory=lambda: [
        "awesome-list", "tutorial", "course", "interview", "dotfiles",
    ])
    watch_interval: int = 1800  # 30 min
    default_hours: int = 24
    default_limit: int = 30

    @classmethod
    def from_file(cls, path: str | None = None) -> RadarConfig:
        config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
        if not config_path.exists():
            return cls()
        try:
            import yaml  # noqa: F811
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
            github_languages=gh.get("languages", ["Python", "TypeScript", "Rust", "Go", "C++"]),
            github_topics=gh.get("topics", [
                "ai", "llm", "agents", "machine-learning", "deep-learning",
                "generative-ai", "rag", "langchain", "transformers",
            ]),
            exclude_keywords=general.get("exclude_keywords", [
                "awesome-list", "tutorial", "course", "interview", "dotfiles",
            ]),
            watch_interval=int(general.get("watch_interval", 1800)),
            default_hours=int(general.get("default_hours", 24)),
            default_limit=int(general.get("default_limit", 30)),
        )


def example_config() -> str:
    return """\
# AI Radar config — place at ~/.openkeel/ai_radar.yaml
github:
  token_env: GITHUB_TOKEN
  languages: [Python, TypeScript, Rust, Go, C++]
  topics: [ai, llm, agents, machine-learning, deep-learning, generative-ai, rag]

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
  exclude_keywords:
    - awesome-list
    - tutorial
    - course
    - interview
"""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> Any:
    hdrs = headers or {}
    hdrs.setdefault("User-Agent", "openkeel-ai-radar/1.0")
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        logger.warning("GET %s failed: %s", url, exc)
        return None


def _github_headers(config: RadarConfig) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "openkeel-ai-radar",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv(config.github_token_env, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _hyphae_save(config: RadarConfig, text: str) -> None:
    """Best-effort save to Hyphae."""
    try:
        data = json.dumps({"text": text, "source": "ai-radar"}).encode()
        req = urllib.request.Request(
            f"{config.hyphae_url}/remember",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Hyphae may be offline


# ---------------------------------------------------------------------------
# Source: GitHub trending / search
# ---------------------------------------------------------------------------

def github_scan(config: RadarConfig, hours: int, limit: int) -> list[dict[str, Any]]:
    """Search GitHub for recently created AI repos."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    topics_query = " ".join(f"topic:{t}" for t in config.github_topics[:3])
    query = f"is:public archived:false created:>={since.date().isoformat()} {topics_query}"
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": str(min(limit * 2, 100)),
    })
    url = f"{GITHUB_API}/search/repositories?{params}"
    data = _get_json(url, headers=_github_headers(config))
    if not data or not isinstance(data.get("items"), list):
        return []

    results = []
    for repo in data["items"]:
        haystack = f"{repo.get('name', '')} {repo.get('description', '')} {' '.join(repo.get('topics', []))}".lower()
        if any(ex.lower() in haystack for ex in config.exclude_keywords):
            continue

        stars = int(repo.get("stargazers_count", 0) or 0)
        score = _github_score(repo, config)
        results.append({
            "source": "github",
            "title": repo.get("full_name", ""),
            "url": repo.get("html_url", ""),
            "description": (repo.get("description") or "")[:200],
            "score": score,
            "stars": stars,
            "language": repo.get("language") or "",
            "topics": repo.get("topics", [])[:5],
            "created": repo.get("created_at", ""),
            "meta": {"forks": repo.get("forks_count", 0)},
        })

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


def _github_score(repo: dict, config: RadarConfig) -> float:
    score = 0.0
    stars = int(repo.get("stargazers_count", 0) or 0)
    if stars:
        score += min(30.0, math.log2(stars + 1) * 5.0)

    haystack = f"{repo.get('name', '')} {repo.get('description', '')} {' '.join(repo.get('topics', []))}".lower()
    kw_hits = sum(1 for kw in config.keywords if kw.lower() in haystack)
    score += min(25.0, kw_hits * 5.0)

    topic_hits = sum(1 for t in config.github_topics if t.lower() in [x.lower() for x in repo.get("topics", [])])
    score += min(15.0, topic_hits * 5.0)

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
# Source: GitHub trending page (scrape-free — uses search sorted by stars)
# ---------------------------------------------------------------------------

def github_trending(config: RadarConfig, limit: int = 15) -> list[dict[str, Any]]:
    """Get trending repos using search API (most starred recently updated AI repos)."""
    since = datetime.now(UTC) - timedelta(days=7)
    # GitHub search doesn't support bare OR — use topic filters instead
    topics_query = " ".join(f"topic:{t}" for t in config.github_topics[:4])
    query = f"is:public archived:false pushed:>={since.date().isoformat()} stars:>50 {topics_query}"
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "stars",
        "order": "desc",
        "per_page": str(min(limit * 2, 100)),
    })
    url = f"{GITHUB_API}/search/repositories?{params}"
    data = _get_json(url, headers=_github_headers(config))
    if not data or not isinstance(data.get("items"), list):
        return []

    results = []
    for repo in data["items"][:limit]:
        results.append({
            "source": "github-trending",
            "title": repo.get("full_name", ""),
            "url": repo.get("html_url", ""),
            "description": (repo.get("description") or "")[:200],
            "score": int(repo.get("stargazers_count", 0) or 0),
            "stars": int(repo.get("stargazers_count", 0) or 0),
            "language": repo.get("language") or "",
            "topics": repo.get("topics", [])[:5],
            "created": repo.get("created_at", ""),
            "meta": {"forks": repo.get("forks_count", 0)},
        })
    return results


# ---------------------------------------------------------------------------
# Source: Reddit
# ---------------------------------------------------------------------------

def reddit_scan(config: RadarConfig, hours: int, limit: int) -> list[dict[str, Any]]:
    """Scan AI subreddits for hot/new posts."""
    cutoff = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
    results = []

    for sub in config.subreddits:
        url = f"{REDDIT_BASE}/r/{sub}/hot.json?limit=25&raw_json=1"
        data = _get_json(url)
        if not data or "data" not in data:
            continue
        for child in data["data"].get("children", []):
            post = child.get("data", {})
            created = post.get("created_utc", 0)
            if created < cutoff:
                continue
            if post.get("stickied"):
                continue

            title = post.get("title", "")
            selftext = (post.get("selftext") or "")[:500]
            score = int(post.get("score", 0))
            comments = int(post.get("num_comments", 0))

            # Relevance check: must match at least one AI keyword
            haystack = f"{title} {selftext}".lower()
            kw_hits = sum(1 for kw in config.keywords if kw.lower() in haystack)
            if kw_hits == 0 and sub not in ("MachineLearning", "LocalLLaMA"):
                continue

            radar_score = score * 0.3 + comments * 0.5 + kw_hits * 10
            results.append({
                "source": f"r/{sub}",
                "title": _clean_text(title),
                "url": f"https://reddit.com{post.get('permalink', '')}",
                "description": _clean_text(selftext[:200]),
                "score": round(radar_score, 1),
                "stars": score,  # upvotes
                "language": "",
                "topics": [],
                "created": datetime.fromtimestamp(created, tz=UTC).isoformat(),
                "meta": {"comments": comments, "subreddit": sub},
            })
        # Be polite to Reddit
        time.sleep(0.5)

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ---------------------------------------------------------------------------
# Source: Hacker News
# ---------------------------------------------------------------------------

def hn_scan(config: RadarConfig, hours: int, limit: int) -> list[dict[str, Any]]:
    """Search HN via Algolia for recent AI stories."""
    cutoff = int((datetime.now(UTC) - timedelta(hours=hours)).timestamp())
    # Search for multiple AI terms
    search_terms = ["AI", "LLM", "GPT", "Claude", "open source AI", "agent framework"]
    results = []
    seen_ids: set[int] = set()

    for term in search_terms:
        params = urllib.parse.urlencode({
            "query": term,
            "tags": "story",
            "numericFilters": f"created_at_i>{cutoff}",
            "hitsPerPage": "20",
        })
        url = f"{HN_ALGOLIA}/search_by_date?{params}"
        data = _get_json(url)
        if not data or not isinstance(data.get("hits"), list):
            continue

        for hit in data["hits"]:
            obj_id = hit.get("objectID")
            if obj_id in seen_ids:
                continue
            seen_ids.add(obj_id)

            points = int(hit.get("points") or 0)
            comments = int(hit.get("num_comments") or 0)
            title = hit.get("title", "")

            radar_score = points * 0.4 + comments * 0.6
            results.append({
                "source": "hackernews",
                "title": _clean_text(title),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}",
                "description": "",
                "score": round(radar_score, 1),
                "stars": points,
                "language": "",
                "topics": [],
                "created": hit.get("created_at", ""),
                "meta": {"comments": comments, "hn_id": obj_id},
            })
        time.sleep(0.3)

    results.sort(key=lambda x: -x["score"])
    return results[:limit]


# ---------------------------------------------------------------------------
# Deep research mode
# ---------------------------------------------------------------------------

def research(config: RadarConfig, query: str, limit: int = 20) -> dict[str, Any]:
    """Deep research on a topic or specific repo."""
    is_github_url = "github.com/" in query
    brief: dict[str, Any] = {"query": query, "sections": {}}

    if is_github_url:
        brief["sections"]["repo"] = _research_github_repo(config, query)
        # Extract repo name for further searches
        match = re.search(r"github\.com/([^/]+/[^/\s?#]+)", query)
        search_term = match.group(1) if match else query
    else:
        search_term = query
        brief["sections"]["github_repos"] = _research_github_search(config, search_term, limit)

    brief["sections"]["reddit"] = _research_reddit(config, search_term, limit)
    brief["sections"]["hackernews"] = _research_hn(config, search_term, limit)

    return brief


def _research_github_repo(config: RadarConfig, url: str) -> dict[str, Any]:
    """Fetch full details for a specific GitHub repo."""
    match = re.search(r"github\.com/([^/]+/[^/\s?#]+)", url)
    if not match:
        return {"error": "Could not parse GitHub URL"}
    repo_path = match.group(1).rstrip("/")

    # Get repo info
    repo_url = f"{GITHUB_API}/repos/{repo_path}"
    repo = _get_json(repo_url, headers=_github_headers(config))
    if not repo:
        return {"error": f"Could not fetch repo {repo_path}"}

    # Get README
    readme_url = f"{GITHUB_API}/repos/{repo_path}/readme"
    readme_data = _get_json(readme_url, headers=_github_headers(config))
    readme_text = ""
    if readme_data and readme_data.get("content"):
        import base64
        try:
            readme_text = base64.b64decode(readme_data["content"]).decode("utf-8", errors="replace")[:3000]
        except Exception:
            pass

    # Get recent issues
    issues_url = f"{GITHUB_API}/repos/{repo_path}/issues?state=all&sort=updated&per_page=10"
    issues = _get_json(issues_url, headers=_github_headers(config)) or []

    # Get recent releases
    releases_url = f"{GITHUB_API}/repos/{repo_path}/releases?per_page=5"
    releases = _get_json(releases_url, headers=_github_headers(config)) or []

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
        "license": (repo.get("license") or {}).get("spdx_id", ""),
        "open_issues": repo.get("open_issues_count", 0),
        "readme_preview": readme_text[:2000],
        "recent_issues": [
            {
                "title": i.get("title", ""),
                "state": i.get("state", ""),
                "comments": i.get("comments", 0),
                "created": i.get("created_at", ""),
                "url": i.get("html_url", ""),
                "labels": [l.get("name", "") for l in i.get("labels", [])],
            }
            for i in (issues if isinstance(issues, list) else [])[:10]
        ],
        "recent_releases": [
            {
                "tag": r.get("tag_name", ""),
                "name": r.get("name", ""),
                "published": r.get("published_at", ""),
                "body": (r.get("body") or "")[:300],
            }
            for r in (releases if isinstance(releases, list) else [])[:5]
        ],
    }


def _research_github_search(config: RadarConfig, query: str, limit: int) -> list[dict]:
    """Search GitHub for repos matching a query."""
    params = urllib.parse.urlencode({
        "q": f"{query} in:name,description,readme",
        "sort": "stars",
        "order": "desc",
        "per_page": str(min(limit, 30)),
    })
    url = f"{GITHUB_API}/search/repositories?{params}"
    data = _get_json(url, headers=_github_headers(config))
    if not data or not isinstance(data.get("items"), list):
        return []

    return [
        {
            "name": r.get("full_name", ""),
            "url": r.get("html_url", ""),
            "description": (r.get("description") or "")[:200],
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "language": r.get("language") or "",
            "topics": r.get("topics", [])[:5],
            "updated": r.get("updated_at", ""),
        }
        for r in data["items"][:limit]
    ]


def _research_reddit(config: RadarConfig, query: str, limit: int) -> list[dict]:
    """Search Reddit for discussions about a topic."""
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "relevance",
        "t": "month",
        "limit": str(min(limit, 25)),
        "raw_json": "1",
    })
    results = []
    # Search across AI subreddits
    for sub in config.subreddits[:4]:  # top 4 subs to keep it fast
        url = f"{REDDIT_BASE}/r/{sub}/search.json?{params}&restrict_sr=on"
        data = _get_json(url)
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
                "created": datetime.fromtimestamp(
                    post.get("created_utc", 0), tz=UTC
                ).isoformat(),
                "selftext_preview": _clean_text((post.get("selftext") or "")[:200]),
            })
        time.sleep(0.5)

    results.sort(key=lambda x: -(x["score"] + x["comments"] * 2))
    return results[:limit]


def _research_hn(config: RadarConfig, query: str, limit: int) -> list[dict]:
    """Search HN for stories and comments about a topic."""
    params = urllib.parse.urlencode({
        "query": query,
        "tags": "story",
        "hitsPerPage": str(min(limit, 30)),
    })
    url = f"{HN_ALGOLIA}/search?{params}"
    data = _get_json(url)
    if not data or not isinstance(data.get("hits"), list):
        return []

    return [
        {
            "title": hit.get("title", ""),
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
            "hn_url": f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
            "points": int(hit.get("points") or 0),
            "comments": int(hit.get("num_comments") or 0),
            "created": hit.get("created_at", ""),
            "author": hit.get("author", ""),
        }
        for hit in data["hits"][:limit]
    ]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"[\n\r]+", " ", text).strip()
    return text


def format_pulse(items: list[dict[str, Any]], title: str = "") -> str:
    if not items:
        return f"  (no results)\n"
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


def format_research(brief: dict[str, Any]) -> str:
    lines = [
        f"\n{'='*60}",
        f"  AI RADAR — RESEARCH BRIEF",
        f"  Query: {brief['query']}",
        f"  Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"{'='*60}",
    ]

    sections = brief.get("sections", {})

    # Single repo detail
    if "repo" in sections:
        repo = sections["repo"]
        if "error" in repo:
            lines.append(f"\n  GitHub Repo: {repo['error']}")
        else:
            lines.append(f"\n--- REPOSITORY: {repo.get('name', '')} ---")
            lines.append(f"  {repo.get('description', '')}")
            lines.append(f"  Stars: {repo.get('stars', 0)} | Forks: {repo.get('forks', 0)} | "
                        f"Issues: {repo.get('open_issues', 0)} | License: {repo.get('license', 'N/A')}")
            lines.append(f"  Language: {repo.get('language', '')} | Topics: {', '.join(repo.get('topics', []))}")
            lines.append(f"  Created: {repo.get('created', '')} | Updated: {repo.get('updated', '')}")

            if repo.get("recent_releases"):
                lines.append(f"\n  Recent Releases:")
                for rel in repo["recent_releases"][:3]:
                    lines.append(f"    {rel['tag']} — {rel.get('name', '')} ({rel.get('published', '')[:10]})")
                    if rel.get("body"):
                        lines.append(f"      {rel['body'][:150]}")

            if repo.get("recent_issues"):
                lines.append(f"\n  Recent Issues:")
                for iss in repo["recent_issues"][:5]:
                    state = "open" if iss["state"] == "open" else "closed"
                    lines.append(f"    [{state}] {iss['title']} ({iss['comments']} comments)")
                    if iss.get("labels"):
                        lines.append(f"      Labels: {', '.join(iss['labels'][:4])}")

            if repo.get("readme_preview"):
                # Just first 500 chars
                lines.append(f"\n  README Preview:")
                for rline in repo["readme_preview"][:500].split("\n")[:15]:
                    lines.append(f"    {rline}")

    # GitHub search results
    if "github_repos" in sections and sections["github_repos"]:
        lines.append(f"\n--- GITHUB REPOS ({len(sections['github_repos'])} found) ---")
        for r in sections["github_repos"][:10]:
            lines.append(f"  {r['name']} ({r.get('stars', 0)} stars, {r.get('language', '')})")
            lines.append(f"    {r['url']}")
            if r.get("description"):
                lines.append(f"    {r['description'][:120]}")

    # Reddit
    if "reddit" in sections and sections["reddit"]:
        lines.append(f"\n--- REDDIT DISCUSSIONS ({len(sections['reddit'])} found) ---")
        for r in sections["reddit"][:10]:
            lines.append(f"  [{r.get('subreddit', '')}] {r['title']} ({r['score']} pts, {r['comments']} comments)")
            lines.append(f"    {r['url']}")
            if r.get("selftext_preview"):
                lines.append(f"    {r['selftext_preview'][:120]}")

    # HN
    if "hackernews" in sections and sections["hackernews"]:
        lines.append(f"\n--- HACKER NEWS ({len(sections['hackernews'])} found) ---")
        for h in sections["hackernews"][:10]:
            lines.append(f"  {h['title']} ({h.get('points', 0)} pts, {h.get('comments', 0)} comments)")
            lines.append(f"    {h['url']}")
            lines.append(f"    Discussion: {h['hn_url']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main commands
# ---------------------------------------------------------------------------

def cmd_pulse(config: RadarConfig, hours: int, limit: int, save_hyphae: bool = True) -> str:
    """Run a full pulse scan across all sources."""
    output_parts = [
        f"\n{'='*60}",
        f"  AI RADAR — PULSE",
        f"  Window: last {hours}h | Limit: {limit} per source",
        f"  Scanned: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"{'='*60}",
    ]

    # GitHub new repos
    print("  Scanning GitHub new repos...", flush=True)
    gh_new = github_scan(config, hours, limit)
    output_parts.append(format_pulse(gh_new, "GITHUB — NEW REPOS"))

    # GitHub trending
    print("  Scanning GitHub trending...", flush=True)
    gh_trending = github_trending(config, limit=15)
    output_parts.append(format_pulse(gh_trending, "GITHUB — TRENDING AI"))

    # Reddit
    print("  Scanning Reddit...", flush=True)
    reddit_results = reddit_scan(config, hours, limit)
    output_parts.append(format_pulse(reddit_results, "REDDIT — AI DISCUSSIONS"))

    # HN
    print("  Scanning Hacker News...", flush=True)
    hn_results = hn_scan(config, hours, limit)
    output_parts.append(format_pulse(hn_results, "HACKER NEWS — AI STORIES"))

    full_output = "\n".join(output_parts)

    # Summary counts
    total = len(gh_new) + len(gh_trending) + len(reddit_results) + len(hn_results)
    summary = (
        f"\n{'='*60}\n"
        f"  TOTALS: {len(gh_new)} new repos, {len(gh_trending)} trending, "
        f"{len(reddit_results)} Reddit threads, {len(hn_results)} HN stories "
        f"({total} items)\n"
        f"{'='*60}"
    )
    full_output += summary

    # Save summary to Hyphae
    if save_hyphae:
        top_items = []
        for item in sorted(gh_new + reddit_results + hn_results, key=lambda x: -x["score"])[:5]:
            top_items.append(f"{item['source']}: {item['title']}")
        if top_items:
            _hyphae_save(config,
                f"AI Radar pulse {datetime.now(UTC).strftime('%Y-%m-%d')}: "
                f"top items: {'; '.join(top_items)}"
            )

    return full_output


def cmd_research(config: RadarConfig, query: str, limit: int = 15) -> str:
    """Run deep research on a topic or repo."""
    print(f"  Researching: {query}", flush=True)
    brief = research(config, query, limit)
    output = format_research(brief)

    # Save to Hyphae
    sections = brief.get("sections", {})
    gh_count = len(sections.get("github_repos", []))
    reddit_count = len(sections.get("reddit", []))
    hn_count = len(sections.get("hackernews", []))
    _hyphae_save(config,
        f"AI Radar research on '{query}': found {gh_count} repos, "
        f"{reddit_count} Reddit threads, {hn_count} HN stories"
    )

    return output


def cmd_watch(config: RadarConfig, hours: int, limit: int) -> None:
    """Continuously watch for new AI developments."""
    interval = config.watch_interval
    print(f"AI Radar watch mode — scanning every {interval}s (Ctrl+C to stop)")
    while True:
        output = cmd_pulse(config, hours, limit, save_hyphae=True)
        print(output)
        print(f"\n  Next scan in {interval}s...")
        time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Radar — track AI developments across GitHub, Reddit, and HN",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # pulse
    p_pulse = sub.add_parser("pulse", help="Scan all sources for recent AI developments")
    p_pulse.add_argument("--hours", type=int, default=None, help="Time window in hours (default: config or 24)")
    p_pulse.add_argument("--limit", type=int, default=None, help="Max results per source (default: config or 30)")
    p_pulse.add_argument("--watch", action="store_true", help="Continuous watch mode")
    p_pulse.add_argument("--config", type=str, default=None, help="Path to config YAML")

    # research
    p_research = sub.add_parser("research", help="Deep-dive research on a topic or repo")
    p_research.add_argument("query", help="Search query or GitHub repo URL")
    p_research.add_argument("--limit", type=int, default=15, help="Max results per source")
    p_research.add_argument("--config", type=str, default=None, help="Path to config YAML")

    # config
    sub.add_parser("config", help="Print example config file")

    args = parser.parse_args()

    if args.command == "config":
        print(example_config())
        return

    if args.command is None:
        parser.print_help()
        return

    logging.basicConfig(level=logging.WARNING)
    config = RadarConfig.from_file(getattr(args, "config", None))

    if args.command == "pulse":
        hours = args.hours or config.default_hours
        limit = args.limit or config.default_limit
        if args.watch:
            cmd_watch(config, hours, limit)
        else:
            output = cmd_pulse(config, hours, limit)
            print(output)

    elif args.command == "research":
        output = cmd_research(config, args.query, args.limit)
        print(output)


if __name__ == "__main__":
    main()
