"""GitHub repository discovery for newly created interesting projects.

This module polls GitHub's repository search API, scores recent repositories
against local preferences, and stores a seen-set so operators only get alerted
about new matches.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_CONFIG_PATH = Path.home() / ".openkeel" / "github_scout.yaml"
DEFAULT_STATE_PATH = Path.home() / ".openkeel" / "github_scout_state.json"


@dataclass
class GitHubScoutConfig:
    token_env: str = "GITHUB_TOKEN"
    state_path: str = str(DEFAULT_STATE_PATH)
    notify_command: str = ""
    include_languages: list[str] = field(default_factory=lambda: ["Python", "TypeScript", "Rust", "Go"])
    include_topics: list[str] = field(default_factory=lambda: ["ai", "agents", "llm", "browser-automation", "database"])
    include_keywords: list[str] = field(default_factory=lambda: ["agent", "inference", "rag", "compiler", "wasm", "robotics"])
    exclude_keywords: list[str] = field(default_factory=lambda: ["awesome", "tutorial", "dotfiles", "config", "interview"])
    watch_owners: list[str] = field(default_factory=list)
    min_score: float = 25.0
    min_stars: int = 0
    since_hours: int = 24
    interval_seconds: int = 900
    max_candidates: int = 100

    @classmethod
    def from_file(cls, path: str | os.PathLike[str] | None = None) -> "GitHubScoutConfig":
        config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
        if not config_path.exists():
            return cls()

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        github = raw.get("github", {}) if isinstance(raw, dict) else {}
        filters = raw.get("filters", {}) if isinstance(raw, dict) else {}
        scoring = raw.get("scoring", {}) if isinstance(raw, dict) else {}
        watch = raw.get("watch", {}) if isinstance(raw, dict) else {}

        return cls(
            token_env=github.get("token_env", "GITHUB_TOKEN"),
            state_path=github.get("state_path", str(DEFAULT_STATE_PATH)),
            notify_command=github.get("notify_command", ""),
            include_languages=_as_str_list(filters.get("include_languages", [])),
            include_topics=_as_str_list(filters.get("include_topics", [])),
            include_keywords=_as_str_list(filters.get("include_keywords", [])),
            exclude_keywords=_as_str_list(filters.get("exclude_keywords", [])),
            watch_owners=_as_str_list(filters.get("watch_owners", [])),
            min_score=float(scoring.get("min_score", 25)),
            min_stars=int(scoring.get("min_stars", 0)),
            since_hours=int(watch.get("since_hours", 24)),
            interval_seconds=int(watch.get("interval_seconds", 900)),
            max_candidates=int(watch.get("max_candidates", 100)),
        )


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def example_config_text() -> str:
    """Return a commented example config file."""
    return """github:
  token_env: GITHUB_TOKEN
  state_path: ~/.openkeel/github_scout_state.json
  # Optional. Command receives repo JSON on stdin and env vars like
  # OPENKEEL_GITHUB_SCOUT_REPO, OPENKEEL_GITHUB_SCOUT_URL, OPENKEEL_GITHUB_SCOUT_SCORE.
  notify_command: ""

filters:
  include_languages:
    - Python
    - TypeScript
    - Rust
    - Go
  include_topics:
    - ai
    - agents
    - llm
    - browser-automation
    - database
  include_keywords:
    - agent
    - inference
    - rag
    - compiler
    - wasm
    - robotics
  exclude_keywords:
    - awesome
    - tutorial
    - dotfiles
    - config
    - interview
  watch_owners: []

scoring:
  min_score: 25
  min_stars: 0

watch:
  since_hours: 24
  interval_seconds: 900
  max_candidates: 100
"""


class GitHubScout:
    """Poll GitHub and surface unseen interesting repositories."""

    def __init__(self, config: GitHubScoutConfig) -> None:
        self.config = config
        self.state_path = Path(config.state_path).expanduser()
        self.state = self._load_state()

    def scan(
        self,
        *,
        since_hours: int | None = None,
        limit: int = 20,
        min_score: float | None = None,
        include_seen: bool = False,
    ) -> list[dict[str, Any]]:
        horizon_hours = since_hours or self.config.since_hours
        floor_score = self.config.min_score if min_score is None else min_score
        candidates = self._search_recent_repositories(
            since_hours=horizon_hours,
            per_page=min(max(limit * 3, 30), self.config.max_candidates, 100),
        )

        seen_ids = set(self.state.get("seen_ids", []))
        matches: list[dict[str, Any]] = []
        require_interest_match = bool(
            self.config.include_topics
            or self.config.include_keywords
            or self.config.watch_owners
        )

        for repo in candidates:
            repo_id = repo.get("id")
            if not include_seen and repo_id in seen_ids:
                continue
            if repo.get("fork") or repo.get("archived"):
                continue
            if int(repo.get("stargazers_count", 0) or 0) < self.config.min_stars:
                continue

            score, reasons, hard_match_count = self._score_repository(repo)
            if require_interest_match and hard_match_count == 0:
                continue
            if score < floor_score:
                continue

            matches.append(
                {
                    "id": repo_id,
                    "name": repo.get("full_name") or repo.get("name"),
                    "url": repo.get("html_url", ""),
                    "description": repo.get("description") or "",
                    "language": repo.get("language") or "",
                    "stars": int(repo.get("stargazers_count", 0) or 0),
                    "forks": int(repo.get("forks_count", 0) or 0),
                    "topics": repo.get("topics", []),
                    "created_at": repo.get("created_at", ""),
                    "updated_at": repo.get("updated_at", ""),
                    "score": round(score, 1),
                    "reasons": reasons,
                    "owner": (repo.get("owner") or {}).get("login", ""),
                }
            )

        matches.sort(key=lambda item: (-item["score"], -item["stars"], item["name"]))
        return matches[:limit]

    def mark_seen(self, repos: list[dict[str, Any]]) -> None:
        if not repos:
            return
        seen_ids = self.state.setdefault("seen_ids", [])
        recent_hits = self.state.setdefault("recent_hits", [])
        seen_lookup = set(seen_ids)

        for repo in repos:
            repo_id = repo.get("id")
            if repo_id in seen_lookup:
                continue
            seen_ids.append(repo_id)
            seen_lookup.add(repo_id)
            recent_hits.append(
                {
                    "id": repo_id,
                    "name": repo.get("name", ""),
                    "url": repo.get("url", ""),
                    "score": repo.get("score", 0),
                    "seen_at": datetime.now(UTC).isoformat(),
                }
            )

        self.state["seen_ids"] = seen_ids[-20000:]
        self.state["recent_hits"] = recent_hits[-500:]
        self.state["last_scan_at"] = datetime.now(UTC).isoformat()
        self._save_state()

    def notify(self, repo: dict[str, Any]) -> None:
        if not self.config.notify_command:
            return

        env = os.environ.copy()
        env["OPENKEEL_GITHUB_SCOUT_REPO"] = repo.get("name", "")
        env["OPENKEEL_GITHUB_SCOUT_URL"] = repo.get("url", "")
        env["OPENKEEL_GITHUB_SCOUT_SCORE"] = str(repo.get("score", ""))
        env["OPENKEEL_GITHUB_SCOUT_LANGUAGE"] = repo.get("language", "")
        env["OPENKEEL_GITHUB_SCOUT_STARS"] = str(repo.get("stars", ""))

        subprocess.run(
            shlex.split(self.config.notify_command),
            input=json.dumps(repo, indent=2),
            text=True,
            env=env,
            check=False,
        )

    def watch(
        self,
        *,
        interval_seconds: int | None = None,
        since_hours: int | None = None,
        limit: int = 20,
        min_score: float | None = None,
    ) -> None:
        interval = interval_seconds or self.config.interval_seconds
        while True:
            hits = self.scan(
                since_hours=since_hours,
                limit=limit,
                min_score=min_score,
                include_seen=False,
            )
            if hits:
                for repo in hits:
                    self.notify(repo)
                self.mark_seen(hits)
            time.sleep(interval)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"seen_ids": [], "recent_hits": []}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("GitHubScout: failed to read state file %s", self.state_path)
            return {"seen_ids": [], "recent_hits": []}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def _search_recent_repositories(self, *, since_hours: int, per_page: int) -> list[dict[str, Any]]:
        since = datetime.now(UTC) - timedelta(hours=since_hours)
        query = f"is:public archived:false created:>={since.date().isoformat()}"
        params = urllib.parse.urlencode(
            {
                "q": query,
                "sort": "updated",
                "order": "desc",
                "per_page": str(per_page),
            }
        )
        url = f"{GITHUB_API}/search/repositories?{params}"
        payload = self._api_get(url)
        items = payload.get("items", [])
        if not isinstance(items, list):
            return []
        return items

    def _api_get(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub API unreachable: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "openkeel-github-scout",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.getenv(self.config.token_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _score_repository(self, repo: dict[str, Any]) -> tuple[float, list[str], int]:
        score = 0.0
        reasons: list[str] = []
        hard_interest_matches = 0

        haystack = " ".join(
            [
                str(repo.get("name", "")),
                str(repo.get("full_name", "")),
                str(repo.get("description", "")),
                " ".join(repo.get("topics", []) or []),
            ]
        ).lower()
        owner = ((repo.get("owner") or {}).get("login") or "").lower()
        language = (repo.get("language") or "").lower()
        stars = int(repo.get("stargazers_count", 0) or 0)
        forks = int(repo.get("forks_count", 0) or 0)

        created_at = repo.get("created_at")
        if created_at:
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_hours = max((datetime.now(UTC) - created).total_seconds() / 3600.0, 0.0)
                recency_points = max(0.0, 30.0 - min(age_hours, 30.0))
                score += recency_points
                reasons.append(f"new ({age_hours:.1f}h)")
            except ValueError:
                pass

        if stars:
            star_points = min(25.0, math.log2(stars + 1) * 6.0)
            score += star_points
            reasons.append(f"{stars} stars")

        if forks:
            fork_points = min(10.0, math.log2(forks + 1) * 3.0)
            score += fork_points
            reasons.append(f"{forks} forks")

        topic_hits = [topic for topic in self.config.include_topics if topic.lower() in (t.lower() for t in repo.get("topics", []) or [])]
        if topic_hits:
            score += 12.0 + (len(topic_hits) - 1) * 3.0
            reasons.append("topics: " + ", ".join(topic_hits[:3]))
            hard_interest_matches += len(topic_hits)

        keyword_hits = [kw for kw in self.config.include_keywords if kw.lower() in haystack]
        if keyword_hits:
            score += min(20.0, len(keyword_hits) * 6.0)
            reasons.append("keywords: " + ", ".join(keyword_hits[:3]))
            hard_interest_matches += len(keyword_hits)

        if self.config.include_languages and language:
            language_hits = [lang for lang in self.config.include_languages if lang.lower() == language]
            if language_hits:
                score += 8.0
                reasons.append(f"language: {repo.get('language')}")

        owner_hits = [item for item in self.config.watch_owners if item.lower() == owner]
        if owner_hits:
            score += 15.0
            reasons.append(f"owner: {owner_hits[0]}")
            hard_interest_matches += len(owner_hits)

        if any(bad.lower() in haystack for bad in self.config.exclude_keywords):
            score -= 100.0
            reasons.append("excluded keyword")

        description = (repo.get("description") or "").strip()
        if description:
            score += min(6.0, len(description) / 40.0)

        return score, reasons, hard_interest_matches


def format_hits(hits: list[dict[str, Any]]) -> str:
    """Render matches in a compact terminal-friendly format."""
    if not hits:
        return "No new interesting repositories found."

    lines: list[str] = []
    for idx, repo in enumerate(hits, start=1):
        lines.append(
            f"{idx}. [{repo['score']:.1f}] {repo['name']} "
            f"({repo['language'] or 'unknown'}, {repo['stars']} stars)"
        )
        lines.append(f"   {repo['url']}")
        if repo.get("description"):
            lines.append(f"   {repo['description']}")
        if repo.get("reasons"):
            lines.append(f"   Why: {', '.join(repo['reasons'][:4])}")
    return "\n".join(lines)
