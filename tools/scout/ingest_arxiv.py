#!/usr/bin/env python3
"""
Ingest recent cs.* arxiv papers into the scout Hyphae shard (port 8102).

Pulls from the official arxiv API (no key required). Default: last 1 day of
cs.* submissions, sorted by submittedDate. Dedupes on arxiv_id via a local
cursor file so repeated runs only ingest new papers.

Usage:
    python3 tools/scout/ingest_arxiv.py                    # last 1 day
    python3 tools/scout/ingest_arxiv.py --days 3           # last 3 days
    python3 tools/scout/ingest_arxiv.py --max 500          # cap results
    python3 tools/scout/ingest_arxiv.py --categories cs.LG,cs.AI,cs.CL
"""
import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HYPHAE_URL = "http://127.0.0.1:8103"
ARXIV_API = "http://export.arxiv.org/api/query"
CURSOR_PATH = Path.home() / ".hyphae" / "scout_arxiv_seen.json"
PAGE_SIZE = 100
REQUEST_DELAY = 3.5  # arxiv asks for >=3s between calls

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# Broad CS coverage by default. Trim via --categories if noisy.
DEFAULT_CATEGORIES = [
    "cs.AI", "cs.LG", "cs.CL", "cs.CV", "cs.NE", "cs.MA",  # ML / AI / NLP / CV
    "cs.DB", "cs.DC", "cs.DS", "cs.IR", "cs.SE", "cs.PL",  # systems / data / SE
    "cs.OS", "cs.AR", "cs.HC", "cs.SY", "cs.RO",           # OS / arch / HCI / robotics
]


def load_cursor() -> set[str]:
    if CURSOR_PATH.exists():
        try:
            return set(json.loads(CURSOR_PATH.read_text()))
        except Exception:
            return set()
    return set()


def save_cursor(seen: set[str]) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Keep cursor bounded — last 50k arxiv IDs is plenty
    trimmed = list(seen)[-50000:]
    CURSOR_PATH.write_text(json.dumps(trimmed))


def build_query(categories: list[str], days: int) -> str:
    cat_clause = "+OR+".join(f"cat:{c}" for c in categories)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d%H%M")
    now = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    # arxiv date range filter
    return f"({cat_clause})+AND+submittedDate:[{cutoff}+TO+{now}]"


def fetch_page(query: str, start: int, page_size: int) -> str:
    url = (
        f"{ARXIV_API}?search_query={query}"
        f"&start={start}&max_results={page_size}"
        f"&sortBy=submittedDate&sortOrder=descending"
    )
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def parse_entries(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    entries = []
    for entry in root.findall("atom:entry", NS):
        arxiv_url = entry.findtext("atom:id", default="", namespaces=NS).strip()
        if not arxiv_url:
            continue
        # id looks like http://arxiv.org/abs/2401.12345v1
        arxiv_id = arxiv_url.rsplit("/", 1)[-1]
        base_id = arxiv_id.split("v")[0]

        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=NS) or "").strip()
        published = (entry.findtext("atom:published", default="", namespaces=NS) or "").strip()
        updated = (entry.findtext("atom:updated", default="", namespaces=NS) or "").strip()

        authors = []
        for a in entry.findall("atom:author", NS):
            name = a.findtext("atom:name", default="", namespaces=NS)
            if name:
                authors.append(name.strip())

        cats = []
        primary = entry.find("arxiv:primary_category", NS)
        if primary is not None:
            pt = primary.get("term")
            if pt:
                cats.append(pt)
        for c in entry.findall("atom:category", NS):
            term = c.get("term")
            if term and term not in cats:
                cats.append(term)

        pdf_url = ""
        for link in entry.findall("atom:link", NS):
            if link.get("title") == "pdf":
                pdf_url = link.get("href", "")
                break

        entries.append({
            "arxiv_id": base_id,
            "version_id": arxiv_id,
            "title": " ".join(title.split()),
            "summary": " ".join(summary.split()),
            "authors": authors,
            "categories": cats,
            "published": published,
            "updated": updated,
            "abs_url": arxiv_url,
            "pdf_url": pdf_url,
        })
    return entries


def remember(text: str, tags: dict) -> bool:
    try:
        r = requests.post(
            f"{HYPHAE_URL}/remember",
            json={"text": text, "source": "scout-arxiv", "tags": tags},
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False


def format_fact(entry: dict) -> str:
    authors = ", ".join(entry["authors"][:6])
    if len(entry["authors"]) > 6:
        authors += f" +{len(entry['authors']) - 6} more"
    cats = " ".join(entry["categories"][:5])
    return (
        f"[ARXIV {entry['arxiv_id']}] {entry['title']}\n"
        f"Authors: {authors}\n"
        f"Categories: {cats}\n"
        f"Published: {entry['published'][:10]}\n"
        f"URL: {entry['abs_url']}\n\n"
        f"{entry['summary']}"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--max", type=int, default=2000, help="hard cap on results")
    ap.add_argument("--categories", type=str, default=",".join(DEFAULT_CATEGORIES))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    print("=" * 60)
    print("ARXIV → SCOUT HYPHAE")
    print("=" * 60)
    print(f"Categories: {' '.join(categories)}")
    print(f"Window: last {args.days} day(s)")
    print(f"Max: {args.max}")

    # Health check Hyphae (skip if dry run)
    if not args.dry_run:
        try:
            r = requests.get(f"{HYPHAE_URL}/health", timeout=5)
            h = r.json()
            print(f"Scout Hyphae: {h.get('facts', h.get('fact_count', '?'))} existing facts")
        except Exception:
            print(f"ERROR: scout Hyphae not reachable at {HYPHAE_URL}")
            print("Start it first: python3 tools/scout/scout_hyphae.py &")
            sys.exit(1)

    seen = load_cursor()
    print(f"Cursor: {len(seen)} arxiv_ids previously ingested")

    query = build_query(categories, args.days)
    stats = {"fetched": 0, "new": 0, "dupes": 0, "errors": 0}
    start = 0

    while stats["fetched"] < args.max:
        try:
            xml_text = fetch_page(query, start, PAGE_SIZE)
        except Exception as e:
            print(f"  fetch error at start={start}: {e}")
            stats["errors"] += 1
            break

        entries = parse_entries(xml_text)
        if not entries:
            break

        stats["fetched"] += len(entries)

        for entry in entries:
            if entry["arxiv_id"] in seen:
                stats["dupes"] += 1
                continue
            fact = format_fact(entry)
            tags = {
                "arxiv_id": entry["arxiv_id"],
                "primary_category": entry["categories"][0] if entry["categories"] else "",
                "published": entry["published"][:10],
                "source": "arxiv",
            }
            if args.dry_run:
                print(f"  [DRY] {entry['arxiv_id']}  {entry['title'][:80]}")
                ok = True
            else:
                ok = remember(fact, tags)
            if ok:
                stats["new"] += 1
                seen.add(entry["arxiv_id"])
            else:
                stats["errors"] += 1

        print(
            f"  page start={start} got={len(entries)} "
            f"new={stats['new']} dupes={stats['dupes']} errors={stats['errors']}"
        )

        if len(entries) < PAGE_SIZE:
            break
        start += PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    if not args.dry_run:
        save_cursor(seen)

    print("-" * 60)
    print(f"Fetched: {stats['fetched']}")
    print(f"New:     {stats['new']}")
    print(f"Dupes:   {stats['dupes']}")
    print(f"Errors:  {stats['errors']}")


if __name__ == "__main__":
    main()
