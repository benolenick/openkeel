#!/usr/bin/env python3
"""arXiv paper harvester — fetch papers and ingest into research shards.

Queries arXiv for papers on specific topics, fetches metadata + PDFs,
extracts insights, and populates research shards.

Usage:
  harvester = ArxivHarvester()
  harvester.harvest("routing inference llm", shard_name="routing-papers", max_results=20)
  # or for a topic with multiple search terms:
  harvester.harvest_multi([
    ("mixture of experts", "moe"),
    ("speculative decoding", "speculative"),
  ], shard_name="inference-opt")
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from openkeel.calcifer.research_shards import ResearchShard


@dataclass
class ArxivPaper:
    """Intermediate representation of an arXiv paper."""
    arxiv_id: str
    title: str
    abstract: str
    authors: list[str]
    published: str
    url: str


class ArxivHarvester:
    """Fetch papers from arXiv and ingest into research shards."""

    ARXIV_API = "http://export.arxiv.org/api/query"
    USER_AGENT = "openkeel-calcifer (+http://localhost; +email@example.com)"

    def __init__(self, rate_limit_delay: float = 2.0) -> None:
        """Initialize harvester with rate limiting."""
        self.rate_limit_delay = rate_limit_delay
        self.last_request_time = 0.0

    def _rate_limit(self) -> None:
        """Respect arXiv rate limits (3 req/sec = 0.33s min between requests)."""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)

    def search(
        self,
        query: str,
        max_results: int = 20,
        sort_by: str = "submittedDate",
        sort_order: str = "descending",
    ) -> list[ArxivPaper]:
        """Query arXiv for papers.

        Args:
            query: arXiv search query (e.g., "routing inference language models")
            max_results: max papers to fetch
            sort_by: "submittedDate", "relevance", "lastUpdatedDate"
            sort_order: "descending", "ascending"

        Returns:
            List of ArxivPaper objects.
        """
        params = {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": sort_by,
            "sortOrder": sort_order,
        }

        url = f"{self.ARXIV_API}?{urllib.parse.urlencode(params)}"

        self._rate_limit()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml = resp.read().decode("utf-8")
            self.last_request_time = time.time()
        except urllib.error.URLError as e:
            print(f"arXiv query failed: {e}")
            return []

        return self._parse_arxiv_xml(xml)

    def _parse_arxiv_xml(self, xml: str) -> list[ArxivPaper]:
        """Parse arXiv API XML response."""
        papers = []

        # Regex-based parsing (avoid adding XML library dependency)
        entry_pattern = r"<entry>(.*?)</entry>"
        entries = re.findall(entry_pattern, xml, re.DOTALL)

        for entry in entries:
            try:
                # Extract fields
                arxiv_id_match = re.search(
                    r"<id>http://arxiv\.org/abs/([\d.]+)(?:v\d+)?</id>", entry
                )
                if not arxiv_id_match:
                    continue
                arxiv_id = arxiv_id_match.group(1)

                title_match = re.search(r"<title>(.*?)</title>", entry)
                title = (
                    title_match.group(1).replace("\n", " ").strip()
                    if title_match
                    else ""
                )

                abstract_match = re.search(r"<summary>(.*?)</summary>", entry, re.DOTALL)
                abstract = (
                    abstract_match.group(1).replace("\n", " ").strip()
                    if abstract_match
                    else ""
                )

                published_match = re.search(r"<published>([\d\-T:Z]+)</published>", entry)
                published = published_match.group(1) if published_match else ""

                # Authors
                author_pattern = r"<name>(.*?)</name>"
                authors = re.findall(author_pattern, entry)[
                    :10
                ]  # first 10 to avoid bloat

                url = f"https://arxiv.org/abs/{arxiv_id}"

                paper = ArxivPaper(
                    arxiv_id=arxiv_id,
                    title=title,
                    abstract=abstract,
                    authors=authors,
                    published=published,
                    url=url,
                )
                papers.append(paper)
            except Exception:
                continue

        return papers

    def extract_insights(self, paper: ArxivPaper) -> str:
        """Extract key insights from paper abstract.

        This is a simple rule-based approach. For production, you'd want
        to fetch the PDF and summarize with an LLM.
        """
        abstract = paper.abstract.lower()

        insights = []

        # Routing/selection patterns
        if any(w in abstract for w in ["routing", "select", "route", "mixture of experts"]):
            insights.append("Addresses model/expert routing/selection")

        # Performance patterns
        if any(
            w in abstract
            for w in [
                "efficient",
                "fast",
                "speed",
                "latency",
                "low-latency",
                "optimization",
            ]
        ):
            insights.append("Focuses on efficiency/performance")

        # Cost patterns
        if any(w in abstract for w in ["cost", "budget", "token", "inference cost"]):
            insights.append("Addresses inference cost/token budget")

        # Quality patterns
        if any(
            w in abstract
            for w in [
                "quality",
                "accuracy",
                "performance",
                "benchmark",
                "sota",
                "state-of-art",
            ]
        ):
            insights.append("Discusses quality/accuracy metrics")

        # Speculative patterns
        if any(
            w in abstract
            for w in ["speculative", "draft", "verification", "constraint"]
        ):
            insights.append("Involves speculative/draft decoding")

        # Cache/memory patterns
        if any(w in abstract for w in ["cache", "memory", "kv", "context window"]):
            insights.append("Addresses caching/memory management")

        return " | ".join(insights) if insights else "General LLM/AI research"

    def harvest(
        self,
        query: str,
        shard_name: str,
        max_results: int = 20,
        extract_insights: bool = True,
    ) -> int:
        """Harvest papers from arXiv and ingest into a shard.

        Args:
            query: arXiv search query
            shard_name: name of research shard to populate
            max_results: max papers to fetch
            extract_insights: whether to extract insights from abstract

        Returns:
            Number of papers ingested.
        """
        print(f"Harvesting '{query}' into shard '{shard_name}'...")
        papers = self.search(query, max_results=max_results)

        if not papers:
            print(f"No papers found for query: {query}")
            return 0

        shard = ResearchShard.get_or_create(shard_name)
        count = 0

        for paper in papers:
            try:
                insights = (
                    self.extract_insights(paper) if extract_insights else ""
                )
                shard.add_paper(
                    paper_id=paper.arxiv_id,
                    title=paper.title,
                    abstract=paper.abstract,
                    url=paper.url,
                    authors=paper.authors,
                    published=paper.published,
                    insights=insights,
                    tags=["arxiv", query.split()[0]],  # basic tagging
                )
                count += 1
                print(f"  ✓ {paper.title[:60]}")
            except Exception as e:
                print(f"  ✗ Error ingesting {paper.arxiv_id}: {e}")

        print(f"\nIngested {count}/{len(papers)} papers into '{shard_name}'")
        print(f"Shard now contains {shard.count()} papers total")
        return count

    def harvest_multi(
        self,
        queries: list[tuple[str, str]],
        shard_name: str,
        max_results_per_query: int = 15,
    ) -> int:
        """Harvest multiple queries into a single shard.

        Args:
            queries: list of (query_string, tag) tuples
            shard_name: name of research shard to populate
            max_results_per_query: max papers per query

        Returns:
            Total number of papers ingested.
        """
        shard = ResearchShard.get_or_create(shard_name)
        total = 0

        for query, tag in queries:
            print(f"\n--- Query: {query} (tag: {tag}) ---")
            papers = self.search(query, max_results=max_results_per_query)

            for paper in papers:
                try:
                    insights = self.extract_insights(paper)
                    shard.add_paper(
                        paper_id=paper.arxiv_id,
                        title=paper.title,
                        abstract=paper.abstract,
                        url=paper.url,
                        authors=paper.authors,
                        published=paper.published,
                        insights=insights,
                        tags=["arxiv", tag],
                    )
                    total += 1
                    print(f"  ✓ {paper.title[:60]}")
                except Exception as e:
                    print(f"  ✗ Error: {e}")

        print(f"\nTotal ingested: {total} papers into '{shard_name}'")
        print(f"Shard now contains {shard.count()} papers total")
        return total


if __name__ == "__main__":
    # Example: harvest papers on routing and inference optimization
    harvester = ArxivHarvester()

    # Single query
    # harvester.harvest("routing inference language models", "routing-papers", max_results=20)

    # Multiple queries into one shard
    harvester.harvest_multi(
        [
            ("mixture of experts routing selection", "moe"),
            ("speculative decoding draft", "speculative"),
            ("token budget constraint inference", "efficiency"),
        ],
        shard_name="inference-routing",
        max_results_per_query=10,
    )
