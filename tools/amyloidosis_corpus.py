#!/usr/bin/env python3
"""
Amyloidosis Cardiac Research Corpus Downloader
===============================================
Downloads every available full-text paper on cardiac amyloidosis from PMC
(PubMed Central) Open Access, plus metadata/abstracts from PubMed.

Sources:
- PMC OA: Full-text XML/PDF (free, legal bulk download)
- PubMed: Abstracts + metadata for papers without full text
- Europe PMC: Additional full-text via REST API

Usage:
    python3 amyloidosis_corpus.py --output ~/amyloidosis_papers
    python3 amyloidosis_corpus.py --output ~/amyloidosis_papers --full-text-only
    python3 amyloidosis_corpus.py --output ~/amyloidosis_papers --resume
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

# ── Search queries covering all aspects of cardiac amyloidosis ──────────────
SEARCH_QUERIES = [
    # Core disease terms
    '"cardiac amyloidosis"',
    '"transthyretin cardiomyopathy"',
    '"ATTR cardiomyopathy"',
    '"ATTR amyloidosis"',
    '"AL amyloidosis" AND (cardiac OR heart)',
    '"amyloid cardiomyopathy"',
    '"senile cardiac amyloidosis"',
    '"wild-type transthyretin amyloidosis"',
    '"hereditary transthyretin amyloidosis"',
    '"familial amyloid cardiomyopathy"',
    # Diagnostics
    '"cardiac amyloid" AND (diagnosis OR imaging OR biopsy)',
    '"technetium pyrophosphate" AND amyloid',
    '"DPD scintigraphy" AND amyloid',
    '"cardiac MRI" AND amyloid',
    '"endomyocardial biopsy" AND amyloid',
    '"strain echocardiography" AND amyloid',
    # Treatments
    'tafamidis AND (cardiac OR heart OR cardiomyopathy)',
    'patisiran AND (cardiac OR heart)',
    'inotersen AND amyloid',
    'vutrisiran AND amyloid',
    'eplontersen AND amyloid',
    'diflunisal AND amyloid AND cardiac',
    'doxycycline AND amyloid AND cardiac',
    '"CRISPR" AND transthyretin',
    '"gene therapy" AND transthyretin',
    '"gene silencing" AND transthyretin',
    '"antisense oligonucleotide" AND transthyretin',
    '"liver transplant" AND amyloidosis AND cardiac',
    # Pathophysiology
    '"amyloid fibril" AND (cardiac OR myocardial)',
    '"transthyretin" AND (misfolding OR aggregation OR stabilizer)',
    '"TTR" AND (tetramer OR monomer) AND amyloid',
    '"serum amyloid P" AND cardiac',
    '"light chain" AND amyloid AND (cardiac OR heart)',
    # Biomarkers / prognosis
    '(troponin OR "NT-proBNP" OR "BNP") AND amyloidosis AND cardiac',
    '"cardiac biomarker" AND amyloidosis',
    '"amyloidosis prognosis" AND cardiac',
    # Clinical trials
    '"clinical trial" AND "cardiac amyloidosis"',
    '"clinical trial" AND transthyretin AND cardiomyopathy',
    # Reviews and guidelines
    '"cardiac amyloidosis" AND (review OR guideline OR consensus)',
]

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PMC_OA_BASE = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"

# Rate limit: NCBI allows 3 req/sec without API key, 10 with
RATE_LIMIT = 0.35  # seconds between requests


class AmyloidosisCorpus:
    def __init__(self, output_dir: str, api_key: str = None, full_text_only: bool = False):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "fulltext").mkdir(exist_ok=True)
        (self.output_dir / "abstracts").mkdir(exist_ok=True)
        (self.output_dir / "metadata").mkdir(exist_ok=True)

        self.api_key = api_key
        self.full_text_only = full_text_only
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "AmyloidosisResearch/1.0 (personal research; mailto:research@example.com)"
        })

        # SQLite tracker
        self.db_path = self.output_dir / "corpus.db"
        self.db = sqlite3.connect(str(self.db_path))
        self._init_db()

        # Stats
        self.stats = {"searched": 0, "new_pmids": 0, "downloaded_ft": 0, "downloaded_abs": 0, "errors": 0}

    def _init_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS papers (
                pmid TEXT PRIMARY KEY,
                pmcid TEXT,
                doi TEXT,
                title TEXT,
                authors TEXT,
                journal TEXT,
                year INTEGER,
                abstract TEXT,
                has_fulltext INTEGER DEFAULT 0,
                fulltext_path TEXT,
                abstract_path TEXT,
                source TEXT,
                query TEXT,
                downloaded_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_pmcid ON papers(pmcid);
            CREATE INDEX IF NOT EXISTS idx_doi ON papers(doi);
            CREATE INDEX IF NOT EXISTS idx_year ON papers(year);
            CREATE INDEX IF NOT EXISTS idx_has_fulltext ON papers(has_fulltext);
        """)
        self.db.commit()

    def _api_params(self, params: dict) -> dict:
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _rate_limit(self):
        time.sleep(RATE_LIMIT)

    # ── Phase 1: Collect all PMIDs ──────────────────────────────────────────

    def search_pubmed(self, query: str, retmax: int = 10000) -> list[str]:
        """Search PubMed and return list of PMIDs."""
        pmids = []
        retstart = 0

        while True:
            params = self._api_params({
                "db": "pubmed",
                "term": query,
                "retmax": min(retmax, 10000),
                "retstart": retstart,
                "rettype": "json",
                "retmode": "json",
            })
            self._rate_limit()
            try:
                r = self.session.get(f"{NCBI_BASE}/esearch.fcgi", params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
                result = data.get("esearchresult", {})
                ids = result.get("idlist", [])
                total = int(result.get("count", 0))

                if not ids:
                    break

                pmids.extend(ids)
                retstart += len(ids)
                print(f"    ... fetched {len(pmids)}/{total} PMIDs")

                if retstart >= total:
                    break
            except Exception as e:
                print(f"    [ERROR] Search failed: {e}")
                self.stats["errors"] += 1
                break

        return pmids

    def collect_all_pmids(self) -> set:
        """Run all search queries and collect unique PMIDs."""
        all_pmids = set()

        # Check what we already have
        existing = set(row[0] for row in self.db.execute("SELECT pmid FROM papers").fetchall())
        print(f"Already have {len(existing)} papers in database")

        for i, query in enumerate(SEARCH_QUERIES, 1):
            print(f"\n[{i}/{len(SEARCH_QUERIES)}] Searching: {query}")
            pmids = self.search_pubmed(query)
            new = set(pmids) - existing - all_pmids
            all_pmids.update(pmids)
            print(f"    Found {len(pmids)} results, {len(new)} new")

        # Deduplicate against existing
        new_pmids = all_pmids - existing
        print(f"\n{'='*60}")
        print(f"Total unique PMIDs found: {len(all_pmids)}")
        print(f"Already in database: {len(all_pmids & existing)}")
        print(f"New to download: {len(new_pmids)}")
        self.stats["new_pmids"] = len(new_pmids)
        return new_pmids

    # ── Phase 2: Fetch metadata + abstracts ─────────────────────────────────

    def fetch_metadata_batch(self, pmids: list[str]) -> list[dict]:
        """Fetch metadata for a batch of PMIDs using efetch."""
        papers = []
        params = self._api_params({
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "xml",
            "retmode": "xml",
        })
        self._rate_limit()
        try:
            r = self.session.get(f"{NCBI_BASE}/efetch.fcgi", params=params, timeout=60)
            r.raise_for_status()
            root = ET.fromstring(r.text)

            for article in root.findall(".//PubmedArticle"):
                paper = self._parse_pubmed_article(article)
                if paper:
                    papers.append(paper)
        except Exception as e:
            print(f"    [ERROR] Metadata fetch failed: {e}")
            self.stats["errors"] += 1

        return papers

    def _parse_pubmed_article(self, article) -> dict | None:
        """Parse a PubmedArticle XML element into a dict."""
        try:
            medline = article.find(".//MedlineCitation")
            if medline is None:
                return None

            pmid = medline.findtext(".//PMID", "")
            art = medline.find(".//Article")
            if art is None:
                return None

            title = art.findtext(".//ArticleTitle", "")

            # Authors
            authors = []
            for author in art.findall(".//Author"):
                last = author.findtext("LastName", "")
                first = author.findtext("ForeName", "")
                if last:
                    authors.append(f"{last} {first}".strip())

            # Journal
            journal = art.findtext(".//Journal/Title", "")

            # Year
            year = None
            for date_elem in [".//ArticleDate", ".//PubDate"]:
                y = art.findtext(f"{date_elem}/Year")
                if y:
                    year = int(y)
                    break

            # Abstract
            abstract_parts = []
            for abs_text in art.findall(".//Abstract/AbstractText"):
                label = abs_text.get("Label", "")
                text = "".join(abs_text.itertext())
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
            abstract = "\n\n".join(abstract_parts)

            # DOI
            doi = ""
            for eid in article.findall(".//ArticleId"):
                if eid.get("IdType") == "doi":
                    doi = eid.text or ""
                    break

            # PMCID
            pmcid = ""
            for eid in article.findall(".//ArticleId"):
                if eid.get("IdType") == "pmc":
                    pmcid = eid.text or ""
                    break

            return {
                "pmid": pmid,
                "pmcid": pmcid,
                "doi": doi,
                "title": title,
                "authors": "; ".join(authors),
                "journal": journal,
                "year": year,
                "abstract": abstract,
            }
        except Exception as e:
            print(f"    [WARN] Parse error: {e}")
            return None

    # ── Phase 3: Download full text from PMC ────────────────────────────────

    def get_pmcids_for_pmids(self, pmids: list[str]) -> dict:
        """Convert PMIDs to PMCIDs using ID converter API."""
        pmid_to_pmc = {}
        batch_size = 200

        for i in range(0, len(pmids), batch_size):
            batch = pmids[i:i + batch_size]
            params = {
                "ids": ",".join(batch),
                "format": "json",
                "tool": "amyloidosis_research",
                "email": "research@example.com",
            }
            self._rate_limit()
            try:
                r = self.session.get(
                    "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
                    params=params, timeout=30
                )
                r.raise_for_status()
                data = r.json()
                for rec in data.get("records", []):
                    pmid = rec.get("pmid", "")
                    pmcid = rec.get("pmcid", "")
                    if pmid and pmcid:
                        pmid_to_pmc[pmid] = pmcid
            except Exception as e:
                print(f"    [ERROR] ID conversion failed: {e}")
                self.stats["errors"] += 1

        return pmid_to_pmc

    def download_pmc_fulltext(self, pmcid: str) -> str | None:
        """Download full text XML from PMC OA service."""
        self._rate_limit()
        try:
            # Try OA service first
            r = self.session.get(PMC_OA_BASE, params={"id": pmcid}, timeout=30)
            r.raise_for_status()

            root = ET.fromstring(r.text)
            # Look for XML link
            for link in root.findall(".//link"):
                fmt = link.get("format", "")
                href = link.get("href", "")
                if fmt == "xml" and href:
                    return self._download_file(href, pmcid, "xml")
                elif fmt == "pdf" and href:
                    return self._download_file(href, pmcid, "pdf")

            # Fallback: try Europe PMC
            return self._download_europepmc(pmcid)

        except Exception as e:
            # Try Europe PMC as fallback
            return self._download_europepmc(pmcid)

    def _download_europepmc(self, pmcid: str) -> str | None:
        """Download full text from Europe PMC REST API."""
        self._rate_limit()
        try:
            url = f"{EUROPEPMC_BASE}/{pmcid}/fullTextXML"
            r = self.session.get(url, timeout=30)
            if r.status_code == 200 and len(r.text) > 500:
                filename = f"{pmcid}.xml"
                filepath = self.output_dir / "fulltext" / filename
                filepath.write_text(r.text, encoding="utf-8")
                return str(filepath)
        except Exception as e:
            pass
        return None

    def _download_file(self, url: str, pmcid: str, ext: str) -> str | None:
        """Download a file from URL."""
        self._rate_limit()
        try:
            r = self.session.get(url, timeout=60, stream=True)
            r.raise_for_status()
            filename = f"{pmcid}.{ext}"
            filepath = self.output_dir / "fulltext" / filename
            with open(filepath, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return str(filepath)
        except Exception as e:
            print(f"    [ERROR] Download failed for {pmcid}: {e}")
            self.stats["errors"] += 1
            return None

    # ── Phase 4: Save abstracts for non-fulltext papers ─────────────────────

    def save_abstract(self, paper: dict) -> str | None:
        """Save abstract as text file."""
        if not paper.get("abstract"):
            return None

        pmid = paper["pmid"]
        filepath = self.output_dir / "abstracts" / f"PMID_{pmid}.txt"

        content = f"""Title: {paper.get('title', '')}
Authors: {paper.get('authors', '')}
Journal: {paper.get('journal', '')}
Year: {paper.get('year', '')}
DOI: {paper.get('doi', '')}
PMID: {pmid}
PMCID: {paper.get('pmcid', '')}

{'='*60}
ABSTRACT
{'='*60}

{paper['abstract']}
"""
        filepath.write_text(content, encoding="utf-8")
        return str(filepath)

    # ── Main pipeline ───────────────────────────────────────────────────────

    def run(self):
        """Run the full download pipeline."""
        print("=" * 60)
        print("AMYLOIDOSIS CARDIAC RESEARCH CORPUS DOWNLOADER")
        print("=" * 60)

        # Phase 1: Collect PMIDs
        print("\n[PHASE 1] Searching PubMed for all cardiac amyloidosis papers...")
        new_pmids = self.collect_all_pmids()

        if not new_pmids:
            print("\nNo new papers to download!")
            self._print_stats()
            return

        pmid_list = sorted(new_pmids)

        # Phase 2: Fetch metadata in batches
        print(f"\n[PHASE 2] Fetching metadata for {len(pmid_list)} papers...")
        all_papers = []
        batch_size = 200

        for i in range(0, len(pmid_list), batch_size):
            batch = pmid_list[i:i + batch_size]
            print(f"  Batch {i // batch_size + 1}/{(len(pmid_list) + batch_size - 1) // batch_size} ({len(batch)} papers)")
            papers = self.fetch_metadata_batch(batch)
            all_papers.extend(papers)

        print(f"  Got metadata for {len(all_papers)} papers")

        # Phase 3: Identify which have PMC full text
        print(f"\n[PHASE 3] Checking PMC availability...")
        pmid_to_pmc = self.get_pmcids_for_pmids(pmid_list)
        pmc_available = len(pmid_to_pmc)
        print(f"  {pmc_available} papers have PMC full text available")

        # Phase 4: Download full text + save abstracts
        print(f"\n[PHASE 4] Downloading papers...")
        for i, paper in enumerate(all_papers, 1):
            pmid = paper["pmid"]
            pmcid = pmid_to_pmc.get(pmid, paper.get("pmcid", ""))

            if i % 50 == 0:
                print(f"  Progress: {i}/{len(all_papers)} | FT: {self.stats['downloaded_ft']} | Abs: {self.stats['downloaded_abs']} | Err: {self.stats['errors']}")

            fulltext_path = None
            abstract_path = None

            # Try full text download
            if pmcid:
                fulltext_path = self.download_pmc_fulltext(pmcid)
                if fulltext_path:
                    self.stats["downloaded_ft"] += 1

            # Save abstract if no full text (or always, for reference)
            if not self.full_text_only or not fulltext_path:
                abstract_path = self.save_abstract(paper)
                if abstract_path and not fulltext_path:
                    self.stats["downloaded_abs"] += 1

            # Store in database
            self.db.execute("""
                INSERT OR REPLACE INTO papers
                (pmid, pmcid, doi, title, authors, journal, year, abstract,
                 has_fulltext, fulltext_path, abstract_path, source, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pubmed+pmc', datetime('now'))
            """, (
                pmid, pmcid, paper.get("doi", ""), paper.get("title", ""),
                paper.get("authors", ""), paper.get("journal", ""),
                paper.get("year"), paper.get("abstract", ""),
                1 if fulltext_path else 0, fulltext_path, abstract_path,
            ))

            if i % 100 == 0:
                self.db.commit()

        self.db.commit()

        # Phase 5: Save metadata index
        print(f"\n[PHASE 5] Saving metadata index...")
        self._save_metadata_index()
        self._print_stats()

    def _save_metadata_index(self):
        """Export a JSON index of all papers."""
        rows = self.db.execute("""
            SELECT pmid, pmcid, doi, title, authors, journal, year,
                   has_fulltext, fulltext_path, abstract_path
            FROM papers ORDER BY year DESC
        """).fetchall()

        index = []
        for row in rows:
            index.append({
                "pmid": row[0], "pmcid": row[1], "doi": row[2],
                "title": row[3], "authors": row[4], "journal": row[5],
                "year": row[6], "has_fulltext": bool(row[7]),
                "fulltext_path": row[8], "abstract_path": row[9],
            })

        idx_path = self.output_dir / "metadata" / "paper_index.json"
        with open(idx_path, "w") as f:
            json.dump(index, f, indent=2)
        print(f"  Index saved: {idx_path} ({len(index)} papers)")

        # Year breakdown
        year_counts = {}
        for p in index:
            y = p.get("year") or "unknown"
            year_counts[y] = year_counts.get(y, 0) + 1
        print("\n  Papers by year (top 10):")
        for year, count in sorted(year_counts.items(), key=lambda x: -x[1])[:10]:
            print(f"    {year}: {count}")

    def _print_stats(self):
        total = self.db.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        ft = self.db.execute("SELECT COUNT(*) FROM papers WHERE has_fulltext=1").fetchone()[0]
        print(f"\n{'='*60}")
        print(f"CORPUS STATS")
        print(f"{'='*60}")
        print(f"  Total papers in database: {total}")
        print(f"  With full text: {ft}")
        print(f"  Abstract only: {total - ft}")
        print(f"  New full text downloaded: {self.stats['downloaded_ft']}")
        print(f"  New abstracts saved: {self.stats['downloaded_abs']}")
        print(f"  Errors: {self.stats['errors']}")
        print(f"  Database: {self.db_path}")
        print(f"  Full text dir: {self.output_dir / 'fulltext'}")


def main():
    parser = argparse.ArgumentParser(description="Download cardiac amyloidosis research corpus")
    parser.add_argument("--output", "-o", default=os.path.expanduser("~/amyloidosis_papers"),
                        help="Output directory (default: ~/amyloidosis_papers)")
    parser.add_argument("--api-key", help="NCBI API key (optional, increases rate limit)")
    parser.add_argument("--full-text-only", action="store_true",
                        help="Only save papers with full text available")
    parser.add_argument("--resume", action="store_true",
                        help="Skip already-downloaded papers (default behavior)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Search only, don't download")
    args = parser.parse_args()

    corpus = AmyloidosisCorpus(
        output_dir=args.output,
        api_key=args.api_key,
        full_text_only=args.full_text_only,
    )

    if args.dry_run:
        print("DRY RUN — searching only, no downloads\n")
        pmids = corpus.collect_all_pmids()
        print(f"\nWould download {len(pmids)} new papers")
    else:
        corpus.run()


if __name__ == "__main__":
    main()
