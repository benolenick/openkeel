#!/usr/bin/env python3
"""
Ingest amyloidosis papers into a dedicated Hyphae shard.

Reads from the amyloidosis_papers corpus (SQLite + text files)
and loads each paper as a Hyphae fact with embeddings.

Usage:
    # First start the amyloidosis Hyphae instance:
    HYPHAE_DB=~/.hyphae/amyloidosis.db python -m uvicorn hyphae.server:app --host 127.0.0.1 --port 8101

    # Then ingest:
    python3 amyloidosis_ingest.py
"""

import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

CORPUS_DB = Path.home() / "amyloidosis_papers" / "corpus.db"
FULLTEXT_DIR = Path.home() / "amyloidosis_papers" / "fulltext"
ABSTRACTS_DIR = Path.home() / "amyloidosis_papers" / "abstracts"
HYPHAE_URL = "http://127.0.0.1:8101"

# Max text length per fact (Hyphae embeds with MiniLM — 256 token window)
# We'll chunk long papers into multiple facts
CHUNK_SIZE = 1500  # chars (~375 tokens)
CHUNK_OVERLAP = 200


def extract_text_from_xml(xml_path: str) -> str:
    """Extract readable text from PMC JATS XML."""
    try:
        content = Path(xml_path).read_text(encoding="utf-8", errors="replace")
        # Strip DOCTYPE
        content = re.sub(r'<!DOCTYPE[^>]*>', '', content)
        root = ET.fromstring(content)

        parts = []

        # Title
        title_el = root.find(".//article-title")
        if title_el is not None:
            title = "".join(title_el.itertext()).strip()
            if title:
                parts.append(f"TITLE: {title}")

        # Abstract
        for abs_el in root.findall(".//abstract"):
            for p in abs_el.findall(".//p"):
                t = "".join(p.itertext()).strip()
                if t:
                    parts.append(t)

        # Body
        body = root.find(".//body")
        if body is not None:
            for sec in body.iter():
                if sec.tag in ("title", "p"):
                    t = "".join(sec.itertext()).strip()
                    if t and len(t) > 20:
                        parts.append(t)

        return "\n\n".join(parts)
    except Exception as e:
        return ""


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks."""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]

        # Try to break at sentence boundary
        if end < len(text):
            last_period = chunk.rfind('. ')
            if last_period > chunk_size // 2:
                end = start + last_period + 2
                chunk = text[start:end]

        chunks.append(chunk.strip())
        start = end - overlap

    return [c for c in chunks if len(c) > 50]


def remember(text: str, source: str = "amyloidosis-corpus", tags: dict = None):
    """Send a fact to the amyloidosis Hyphae instance."""
    payload = {
        "text": text,
        "source": source,
        "tags": tags or {},
    }
    try:
        r = requests.post(f"{HYPHAE_URL}/remember", json=payload, timeout=30)
        return r.status_code == 200
    except Exception as e:
        return False


def ingest():
    print("=" * 60)
    print("AMYLOIDOSIS CORPUS → HYPHAE INGESTION")
    print("=" * 60)

    # Check Hyphae is running
    try:
        r = requests.get(f"{HYPHAE_URL}/health", timeout=5)
        health = r.json()
        print(f"Hyphae amyloidosis shard: {health.get('facts', health.get('fact_count', '?'))} existing facts")
    except Exception:
        print(f"ERROR: Hyphae not reachable at {HYPHAE_URL}")
        print(f"Start it with:")
        print(f"  cd /home/om/Desktop/Hyphae/hyphae")
        print(f"  HYPHAE_DB=~/.hyphae/amyloidosis.db .venv/bin/python -m uvicorn hyphae.server:app --host 127.0.0.1 --port 8101")
        sys.exit(1)

    # Connect to corpus
    db = sqlite3.connect(str(CORPUS_DB))
    db.row_factory = sqlite3.Row
    papers = db.execute("""
        SELECT pmid, pmcid, doi, title, authors, journal, year,
               abstract, has_fulltext, fulltext_path, abstract_path
        FROM papers ORDER BY year DESC
    """).fetchall()

    total = len(papers)
    print(f"Papers in corpus: {total}")

    stats = {"ingested": 0, "chunks": 0, "skipped": 0, "errors": 0}
    start_time = time.time()

    for i, paper in enumerate(papers):
        pmid = paper["pmid"]
        title = paper["title"] or ""
        authors = paper["authors"] or ""
        journal = paper["journal"] or ""
        year = paper["year"] or ""
        abstract = paper["abstract"] or ""
        has_ft = paper["has_fulltext"]
        ft_path = paper["fulltext_path"]

        if i % 100 == 0 and i > 0:
            elapsed = time.time() - start_time
            rate = i / elapsed
            eta = (total - i) / rate if rate > 0 else 0
            print(f"  [{i}/{total}] chunks={stats['chunks']} errors={stats['errors']} "
                  f"rate={rate:.1f}/s ETA={eta/60:.0f}min")

        tags = {
            "pmid": pmid,
            "year": str(year),
            "journal": journal[:100],
            "type": "fulltext" if has_ft else "abstract",
        }
        if paper["doi"]:
            tags["doi"] = paper["doi"]

        # Build the text to ingest
        full_text = ""

        # Try full text XML first
        if has_ft and ft_path and os.path.exists(ft_path):
            full_text = extract_text_from_xml(ft_path)

        if not full_text and abstract:
            # Use abstract
            header = f"TITLE: {title}\nAUTHORS: {authors}\nJOURNAL: {journal} ({year})\nPMID: {pmid}\n\n"
            full_text = header + abstract

        if not full_text:
            stats["skipped"] += 1
            continue

        # Chunk and ingest
        chunks = chunk_text(full_text)
        for j, chunk in enumerate(chunks):
            chunk_tags = dict(tags)
            if len(chunks) > 1:
                chunk_tags["chunk"] = f"{j+1}/{len(chunks)}"

            # Prepend title to each chunk for context
            if j > 0 and title:
                chunk = f"[{title}] {chunk}"

            ok = remember(chunk, source="amyloidosis-corpus", tags=chunk_tags)
            if ok:
                stats["chunks"] += 1
            else:
                stats["errors"] += 1

        stats["ingested"] += 1

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"INGESTION COMPLETE ({elapsed/60:.1f} min)")
    print(f"  Papers processed: {stats['ingested']}")
    print(f"  Total chunks ingested: {stats['chunks']}")
    print(f"  Skipped (no text): {stats['skipped']}")
    print(f"  Errors: {stats['errors']}")

    # Final health check
    try:
        r = requests.get(f"{HYPHAE_URL}/health", timeout=5)
        health = r.json()
        print(f"  Hyphae total facts: {health.get('facts', health.get('fact_count', '?'))}")
    except Exception:
        pass


if __name__ == "__main__":
    ingest()
