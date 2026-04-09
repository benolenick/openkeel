#!/usr/bin/env python3
"""chemister_prospector.py — Find chemistry researcher contacts for Chemister outreach.

Scrapes university chemistry department pages and PubMed for professor/PI emails.
Outputs Smartlead-ready CSV for cold email campaigns.

Usage:
    python3 tools/chemister_prospector.py                        # run dept + pubmed
    python3 tools/chemister_prospector.py --source dept           # dept pages only
    python3 tools/chemister_prospector.py --source pubmed         # PubMed only
    python3 tools/chemister_prospector.py --seeds custom.json     # custom seed URLs
    python3 tools/chemister_prospector.py --output leads.csv      # custom output
    python3 tools/chemister_prospector.py --ncbi-api-key KEY      # faster PubMed
    python3 tools/chemister_prospector.py --resume                # resume checkpoint
"""

import argparse
import csv
import json
import logging
import os
import re
import time
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("prospector")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, ".prospector_checkpoint.json")
DEFAULT_OUTPUT = os.path.join(SCRIPT_DIR, "chemister_prospects.csv")

USER_AGENT = "ChemisterProspector/1.0 (academic research outreach)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}

# Regex patterns for email extraction
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
OBFUSCATED_RE = re.compile(
    r"\b([A-Za-z0-9._%+\-]+)\s*[\[\(]\s*(?:at|AT)\s*[\]\)]\s*([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b"
)
PUBMED_EMAIL_RE = re.compile(r"[Ee]lectronic address:\s*([\w.+\-]+@[\w.\-]+\.\w+)")

# Top university chemistry departments (US, Canada, UK)
SEED_DEPARTMENTS = [
    {"university": "MIT", "url": "https://chemistry.mit.edu/faculty/"},
    {"university": "Stanford", "url": "https://chemistry.stanford.edu/people/faculty"},
    {"university": "UC Berkeley", "url": "https://chemistry.berkeley.edu/faculty"},
    {"university": "Caltech", "url": "https://www.chemistry.caltech.edu/people"},
    {"university": "Harvard", "url": "https://chemistry.harvard.edu/people"},
    {"university": "Yale", "url": "https://chem.yale.edu/people/faculty"},
    {"university": "Princeton", "url": "https://chemistry.princeton.edu/people/faculty"},
    {"university": "Columbia", "url": "https://www.chemistry.columbia.edu/content/faculty"},
    {"university": "U Chicago", "url": "https://chemistry.uchicago.edu/people/faculty"},
    {"university": "Northwestern", "url": "https://chemistry.northwestern.edu/people/faculty/"},
    {"university": "UPenn", "url": "https://www.chem.upenn.edu/people/faculty"},
    {"university": "Cornell", "url": "https://chemistry.cornell.edu/faculty"},
    {"university": "UCLA", "url": "https://www.chemistry.ucla.edu/faculty"},
    {"university": "UT Austin", "url": "https://cm.utexas.edu/faculty-and-research"},
    {"university": "U Michigan", "url": "https://lsa.umich.edu/chem/people/faculty.html"},
    {"university": "UIUC", "url": "https://chemistry.illinois.edu/directory/faculty"},
    {"university": "UW Madison", "url": "https://www.chem.wisc.edu/faculty/"},
    {"university": "Georgia Tech", "url": "https://chemistry.gatech.edu/people"},
    {"university": "U Toronto", "url": "https://www.chemistry.utoronto.ca/people/faculty"},
    {"university": "UBC", "url": "https://www.chem.ubc.ca/people"},
    {"university": "McGill", "url": "https://www.mcgill.ca/chemistry/academic-staff"},
    {"university": "Oxford", "url": "https://www.chem.ox.ac.uk/people"},
    {"university": "Cambridge", "url": "https://www.ch.cam.ac.uk/directory"},
    {"university": "ETH Zurich", "url": "https://chab.ethz.ch/en/the-department/people.html"},
    {"university": "U Washington", "url": "https://chem.washington.edu/people"},
    {"university": "Purdue", "url": "https://www.chem.purdue.edu/people/faculty/"},
    {"university": "Ohio State", "url": "https://chemistry.osu.edu/people/faculty"},
    {"university": "Penn State", "url": "https://science.psu.edu/chem/people"},
    {"university": "UC San Diego", "url": "https://chemistry.ucsd.edu/faculty-research/faculty/index.html"},
    {"university": "U Minnesota", "url": "https://cse.umn.edu/chem/faculty-research"},
]

# PubMed search queries targeting chemistry subfields
PUBMED_QUERIES = [
    '"organic chemistry" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
    '"catalysis" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
    '"computational chemistry" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
    '"medicinal chemistry" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
    '"materials chemistry" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
    '"electrochemistry" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
    '"analytical chemistry" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
    '"chemical synthesis" AND "2024/01"[PDAT]:"2026/12"[PDAT]',
]


@dataclass
class Prospect:
    name: str = ""
    email: str = ""
    title: str = ""
    research_area: str = ""
    university: str = ""
    department_url: str = ""
    source: str = ""           # dept_page | pubmed
    confidence: float = 0.0

    @property
    def first_name(self) -> str:
        parts = self.name.strip().split()
        return parts[0] if parts else ""

    @property
    def last_name(self) -> str:
        parts = self.name.strip().split()
        return parts[-1] if len(parts) > 1 else ""


# ---------------------------------------------------------------------------
# Email extraction helpers
# ---------------------------------------------------------------------------

def extract_emails_from_soup(soup: BeautifulSoup) -> list[dict]:
    """Extract emails from a BeautifulSoup page using multiple strategies."""
    results = []

    # Strategy 1: mailto links (highest confidence)
    for link in soup.select('a[href^="mailto:"]'):
        email = link["href"].replace("mailto:", "").split("?")[0].strip()
        if EMAIL_RE.match(email):
            results.append({"email": email, "confidence": 1.0})

    # Strategy 2: obfuscated [at] patterns
    text = soup.get_text(" ", strip=True)
    for m in OBFUSCATED_RE.finditer(text):
        email = f"{m.group(1)}@{m.group(2)}"
        if email not in [r["email"] for r in results]:
            results.append({"email": email, "confidence": 0.8})

    # Strategy 3: plain text regex
    for m in EMAIL_RE.finditer(text):
        email = m.group(0)
        # Filter out common false positives
        if email.endswith((".png", ".jpg", ".gif", ".css", ".js")):
            continue
        if email in [r["email"] for r in results]:
            continue
        # Prefer .edu emails
        conf = 0.7 if ".edu" in email else 0.5
        results.append({"email": email, "confidence": conf})

    return results


def is_academic_email(email: str) -> bool:
    """Check if email looks like an academic institution."""
    domain = email.split("@")[1].lower() if "@" in email else ""
    academic_tlds = [".edu", ".ac.uk", ".ac.jp", ".ac.kr", ".edu.au", ".ac.nz",
                     ".edu.cn", ".edu.sg", ".ethz.ch", ".mpg.de", ".cnrs.fr",
                     ".utoronto.ca", ".ubc.ca", ".mcgill.ca", ".uwaterloo.ca",
                     ".queensu.ca", ".ualberta.ca", ".usask.ca", ".dal.ca"]
    return any(domain.endswith(tld) for tld in academic_tlds)


def polite_delay(base: float = 2.0, jitter: float = 2.0):
    """Sleep with random jitter to be polite."""
    time.sleep(base + random.random() * jitter)


def check_robots(url: str, session: requests.Session) -> bool:
    """Check if robots.txt allows scraping."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        rp = RobotFileParser()
        rp.set_url(robots_url)
        resp = session.get(robots_url, timeout=5)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            return rp.can_fetch(USER_AGENT, url)
    except Exception:
        pass
    return True  # Allow if robots.txt can't be fetched


# ---------------------------------------------------------------------------
# Source 1: Department Page Scraper
# ---------------------------------------------------------------------------

class DeptPageScraper:
    """Scrapes university chemistry department faculty pages for emails."""

    def __init__(self, seeds: list[dict], session: requests.Session, delay: float = 2.0):
        self.seeds = seeds
        self.session = session
        self.delay = delay

    def scrape_all(self, max_per_dept: int = 100) -> list[Prospect]:
        """Scrape all seed departments."""
        all_prospects = []
        for dept in self.seeds:
            LOG.info(f"Scraping {dept['university']}...")
            try:
                prospects = self._scrape_department(dept, max_per_dept)
                all_prospects.extend(prospects)
                LOG.info(f"  Found {len(prospects)} contacts at {dept['university']}")
            except Exception as e:
                LOG.warning(f"  Failed to scrape {dept['university']}: {e}")
            polite_delay(self.delay)
        return all_prospects

    def _scrape_department(self, dept: dict, max_per_dept: int) -> list[Prospect]:
        """Scrape a single department."""
        url = dept["url"]
        university = dept["university"]

        if not check_robots(url, self.session):
            LOG.info(f"  Blocked by robots.txt: {url}")
            return []

        resp = self.session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # Extract faculty cards/entries from the listing page
        faculty = self._parse_listing(soup, url, university)
        LOG.info(f"  Found {len(faculty)} faculty entries on listing page")

        prospects = []
        for person in faculty[:max_per_dept]:
            if person.email:
                prospects.append(person)
                continue

            # Need to visit profile page
            if person.department_url and person.department_url != url:
                polite_delay(self.delay)
                try:
                    prospect = self._scrape_profile(person)
                    if prospect.email:
                        prospects.append(prospect)
                except Exception as e:
                    LOG.debug(f"  Profile failed for {person.name}: {e}")

        return prospects

    def _parse_listing(self, soup: BeautifulSoup, base_url: str, university: str) -> list[Prospect]:
        """Parse a faculty listing page into Prospect objects."""
        prospects = []

        # Try multiple CSS selector strategies
        cards = (
            soup.select(".views-row") or
            soup.select(".person-card") or
            soup.select(".faculty-member") or
            soup.select("article.node--type-person") or
            soup.select(".profile-card") or
            soup.select(".people-listing .card") or
            soup.select(".directory-item") or
            soup.select(".person") or
            soup.select("li.faculty") or
            soup.select(".member") or
            []
        )

        if cards:
            for card in cards:
                prospect = self._parse_card(card, base_url, university)
                if prospect and prospect.name:
                    prospects.append(prospect)
        else:
            # Fallback: look for any page structure with names + links
            prospects = self._fallback_parse(soup, base_url, university)

        return prospects

    def _parse_card(self, card, base_url: str, university: str) -> Optional[Prospect]:
        """Parse a single faculty card element."""
        # Extract name from headings or links
        name_el = (
            card.select_one("h2 a") or card.select_one("h3 a") or
            card.select_one("h2") or card.select_one("h3") or
            card.select_one("h4 a") or card.select_one("h4") or
            card.select_one(".field--name-title a") or
            card.select_one(".name a") or card.select_one(".name") or
            card.select_one("a.title") or card.select_one(".person-name") or
            None
        )
        if not name_el:
            return None

        name = name_el.get_text(strip=True)
        if not name or len(name) < 3 or len(name) > 80:
            return None

        # Extract profile URL
        profile_url = ""
        link = name_el if name_el.name == "a" else name_el.find("a")
        if link and link.get("href"):
            profile_url = urljoin(base_url, link["href"])

        # Extract title/role
        title_el = (
            card.select_one(".field--name-field-title") or
            card.select_one(".title") or card.select_one(".role") or
            card.select_one(".position") or card.select_one(".person-title") or
            card.select_one("p.subtitle") or
            None
        )
        title = title_el.get_text(strip=True) if title_el else ""

        # Extract research area
        research_el = (
            card.select_one(".field--name-field-research-interests") or
            card.select_one(".research") or card.select_one(".interests") or
            card.select_one(".research-area") or
            None
        )
        research = research_el.get_text(strip=True)[:200] if research_el else ""

        # Try to find email directly on the card
        emails = extract_emails_from_soup(card)
        email = ""
        confidence = 0.0
        if emails:
            # Prefer .edu emails
            edu_emails = [e for e in emails if is_academic_email(e["email"])]
            best = edu_emails[0] if edu_emails else emails[0]
            email = best["email"]
            confidence = best["confidence"]

        return Prospect(
            name=name,
            email=email,
            title=title,
            research_area=research,
            university=university,
            department_url=profile_url or base_url,
            source="dept_page",
            confidence=confidence,
        )

    def _fallback_parse(self, soup: BeautifulSoup, base_url: str, university: str) -> list[Prospect]:
        """Fallback: look for any links that look like faculty profiles."""
        prospects = []
        seen = set()

        # Look for links to profile-like URLs
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True)

            # Skip navigation/generic links
            if not text or len(text) < 4 or len(text) > 60:
                continue
            if any(skip in href.lower() for skip in ["login", "search", "contact", "news", "#", "javascript"]):
                continue

            # Look for profile-like URLs
            if any(kw in href.lower() for kw in ["/people/", "/profile/", "/faculty/", "/person/", "/directory/"]):
                full_url = urljoin(base_url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    # Text is likely the person's name
                    if " " in text and not any(c.isdigit() for c in text):
                        prospects.append(Prospect(
                            name=text,
                            department_url=full_url,
                            university=university,
                            source="dept_page",
                        ))

        return prospects

    def _scrape_profile(self, person: Prospect) -> Prospect:
        """Scrape an individual profile page for email."""
        if not check_robots(person.department_url, self.session):
            return person

        resp = self.session.get(person.department_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        emails = extract_emails_from_soup(soup)
        if emails:
            edu_emails = [e for e in emails if is_academic_email(e["email"])]
            best = edu_emails[0] if edu_emails else emails[0]
            person.email = best["email"]
            person.confidence = best["confidence"]

        # Try to fill in missing title/research from profile
        if not person.title:
            for sel in [".field--name-field-title", ".title", ".position", ".role", "h2"]:
                el = soup.select_one(sel)
                if el:
                    text = el.get_text(strip=True)
                    if "professor" in text.lower() or "lecturer" in text.lower():
                        person.title = text
                        break

        if not person.research_area:
            for sel in [".field--name-field-research-interests", ".research", ".interests"]:
                el = soup.select_one(sel)
                if el:
                    person.research_area = el.get_text(strip=True)[:200]
                    break

        return person


# ---------------------------------------------------------------------------
# Source 2: OpenAlex (free scholarly API — 250M+ works, emails in affiliations)
# ---------------------------------------------------------------------------

# OpenAlex topic IDs for chemistry subfields
OPENALEX_TOPICS = [
    ("T10134", "Chemistry"),
    ("T10566", "Organic Chemistry"),
    ("T11332", "Catalysis"),
    ("T10201", "Analytical Chemistry"),
    ("T10897", "Inorganic Chemistry"),
    ("T11863", "Electrochemistry"),
    ("T12175", "Computational Chemistry"),
    ("T10265", "Materials Chemistry"),
    ("T11489", "Medicinal Chemistry"),
    ("T10711", "Polymer Chemistry"),
    ("T12063", "Physical Chemistry"),
    ("T11674", "Biochemistry"),
]


class OpenAlexMiner:
    """Mine OpenAlex for chemistry researcher emails from paper affiliations."""

    BASE = "https://api.openalex.org"

    def __init__(self, session: requests.Session, contact_email: str = "ben@kwr.kr",
                 delay: float = 0.2):
        self.session = session
        self.contact_email = contact_email
        self.delay = delay

    def mine_all(self, max_pages: int = 10, per_page: int = 200) -> list[Prospect]:
        """Mine emails from recent chemistry papers across multiple subfields."""
        all_prospects = []
        seen_emails = set()

        for topic_id, topic_name in OPENALEX_TOPICS:
            LOG.info(f"  OpenAlex: mining {topic_name} papers...")
            try:
                prospects = self._mine_topic(topic_id, topic_name, max_pages, per_page, seen_emails)
                all_prospects.extend(prospects)
                LOG.info(f"    {topic_name}: {len(prospects)} new emails")
            except Exception as e:
                LOG.warning(f"    {topic_name}: FAILED - {e}")
            time.sleep(self.delay)

        return all_prospects

    def _mine_topic(self, topic_id: str, topic_name: str, max_pages: int,
                    per_page: int, seen_emails: set) -> list[Prospect]:
        """Mine a single topic for emails."""
        prospects = []
        cursor = "*"

        for page_num in range(max_pages):
            params = {
                "filter": f"topics.id:{topic_id},from_publication_date:2023-01-01,"
                          f"type:article,authorships.institutions.type:education",
                "per_page": per_page,
                "sort": "cited_by_count:desc",
                "select": "id,title,authorships,publication_date,topics",
                "mailto": self.contact_email,
                "cursor": cursor,
            }

            resp = self.session.get(f"{self.BASE}/works", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            if not results:
                break

            for work in results:
                work_prospects = self._extract_from_work(work, topic_name, seen_emails)
                prospects.extend(work_prospects)

            # Get next cursor
            cursor = data.get("meta", {}).get("next_cursor")
            if not cursor:
                break

            time.sleep(self.delay)

        return prospects

    def _extract_from_work(self, work: dict, topic_name: str, seen_emails: set) -> list[Prospect]:
        """Extract emails from a single OpenAlex work."""
        prospects = []

        for authorship in work.get("authorships", []):
            # Check raw_affiliation_strings for emails
            for aff_str in authorship.get("raw_affiliation_strings", []):
                emails = EMAIL_RE.findall(aff_str)
                for email in emails:
                    email = email.lower().rstrip(".")
                    if email in seen_emails:
                        continue
                    if email.endswith((".png", ".jpg", ".gif", ".css", ".js")):
                        continue

                    seen_emails.add(email)
                    author = authorship.get("author", {})
                    name = author.get("display_name", "")

                    # Get institution
                    institutions = authorship.get("institutions", [])
                    inst_name = ""
                    for inst in institutions:
                        if inst.get("type") == "education":
                            inst_name = inst.get("display_name", "")
                            break
                    if not inst_name and institutions:
                        inst_name = institutions[0].get("display_name", "")

                    # Determine confidence
                    confidence = 0.85
                    if is_academic_email(email):
                        confidence = 0.9
                    if authorship.get("is_corresponding"):
                        confidence = 0.95

                    prospects.append(Prospect(
                        name=name,
                        email=email,
                        title="",
                        research_area=topic_name,
                        university=inst_name,
                        department_url=author.get("id", ""),
                        source="openalex",
                        confidence=confidence,
                    ))

        return prospects


# ---------------------------------------------------------------------------
# Source 3: PubMed E-utilities
# ---------------------------------------------------------------------------

class PubMedMiner:
    """Extract researcher emails from PubMed papers."""

    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, api_key: Optional[str] = None, session: Optional[requests.Session] = None,
                 delay: float = 0.35):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.delay = delay

    def mine_all(self, queries: list[str], max_per_query: int = 200) -> list[Prospect]:
        """Run multiple PubMed queries and extract author emails."""
        all_prospects = []
        for query in queries:
            LOG.info(f"PubMed search: {query[:60]}...")
            try:
                pmids = self._search(query, max_per_query)
                if pmids:
                    prospects = self._fetch_details(pmids)
                    all_prospects.extend(prospects)
                    LOG.info(f"  {len(pmids)} papers -> {len(prospects)} emails")
                time.sleep(self.delay)
            except Exception as e:
                LOG.warning(f"  PubMed query failed: {e}")
        return all_prospects

    def _search(self, query: str, retmax: int = 200) -> list[str]:
        """Search PubMed and return PMIDs."""
        params = {
            "db": "pubmed",
            "term": query,
            "retmax": retmax,
            "retmode": "json",
        }
        if self.api_key:
            params["api_key"] = self.api_key

        resp = self.session.get(f"{self.BASE}/esearch.fcgi", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("esearchresult", {}).get("idlist", [])

    def _fetch_details(self, pmids: list[str], batch_size: int = 200) -> list[Prospect]:
        """Fetch article details and extract author emails."""
        prospects = []

        for i in range(0, len(pmids), batch_size):
            batch = pmids[i:i + batch_size]
            params = {
                "db": "pubmed",
                "id": ",".join(batch),
                "retmode": "xml",
            }
            if self.api_key:
                params["api_key"] = self.api_key

            resp = self.session.get(f"{self.BASE}/efetch.fcgi", params=params, timeout=30)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            for article in root.iter("PubmedArticle"):
                article_prospects = self._parse_article(article)
                prospects.extend(article_prospects)

            time.sleep(self.delay)

        return prospects

    def _parse_article(self, article) -> list[Prospect]:
        """Extract author emails from a single PubMed article."""
        prospects = []

        # Get article title for context
        title_el = article.find(".//ArticleTitle")
        article_title = title_el.text if title_el is not None and title_el.text else ""

        # Get journal
        journal_el = article.find(".//Journal/Title")
        journal = journal_el.text if journal_el is not None and journal_el.text else ""

        for author in article.iter("Author"):
            last = author.findtext("LastName", "")
            first = author.findtext("ForeName", "")
            if not last:
                continue

            name = f"{first} {last}".strip()

            # Check all affiliations for email
            for aff in author.iter("AffiliationInfo"):
                aff_text = aff.findtext("Affiliation", "")
                if not aff_text:
                    continue

                # Extract email from affiliation
                email_match = PUBMED_EMAIL_RE.search(aff_text)
                if email_match:
                    email = email_match.group(1)
                else:
                    email_match = EMAIL_RE.search(aff_text)
                    if not email_match:
                        continue
                    email = email_match.group(0)
                email = email.rstrip(".")

                # Extract university from affiliation
                university = self._parse_university(aff_text)

                # Determine research area from MeSH terms or article context
                research = self._get_mesh_terms(article)

                prospects.append(Prospect(
                    name=name,
                    email=email,
                    title="",  # PubMed doesn't give titles
                    research_area=research,
                    university=university,
                    department_url=f"https://pubmed.ncbi.nlm.nih.gov/?term={last}+{first}",
                    source="pubmed",
                    confidence=0.8 if is_academic_email(email) else 0.6,
                ))
                break  # One email per author is enough

        return prospects

    def _parse_university(self, affiliation: str) -> str:
        """Extract university name from affiliation string."""
        # Common patterns
        patterns = [
            r"(University of [\w\s]+)",
            r"([\w\s]+ University)",
            r"([\w\s]+ Institute of Technology)",
            r"([\w\s]+ College)",
            r"(ETH [\w]+)",
            r"(MIT|Caltech|Stanford|Harvard|Yale|Princeton|Oxford|Cambridge)",
        ]
        for pat in patterns:
            m = re.search(pat, affiliation)
            if m:
                return m.group(1).strip()
        # Fallback: return first part of affiliation
        parts = affiliation.split(",")
        return parts[0].strip()[:60] if parts else ""

    def _get_mesh_terms(self, article) -> str:
        """Get MeSH terms as research area proxy."""
        terms = []
        for mesh in article.iter("DescriptorName"):
            if mesh.text:
                terms.append(mesh.text)
        return "; ".join(terms[:5])


# ---------------------------------------------------------------------------
# Pipeline: Deduplicate, merge, export
# ---------------------------------------------------------------------------

class ProspectPipeline:
    """Deduplicates and merges prospects from multiple sources."""

    def __init__(self):
        self.prospects: dict[str, Prospect] = {}

    def add(self, prospect: Prospect):
        if not prospect.email:
            return
        key = prospect.email.lower().strip()
        if key in self.prospects:
            self._merge(self.prospects[key], prospect)
        else:
            self.prospects[key] = prospect

    def add_many(self, prospects: list[Prospect]):
        for p in prospects:
            self.add(p)

    def _merge(self, existing: Prospect, new: Prospect):
        """Merge new data into existing prospect, filling blanks."""
        if not existing.title and new.title:
            existing.title = new.title
        if not existing.research_area and new.research_area:
            existing.research_area = new.research_area
        if not existing.university and new.university:
            existing.university = new.university
        # Bump confidence if seen in multiple sources
        if new.source != existing.source:
            existing.confidence = min(1.0, existing.confidence + 0.1)

    def filter_academic(self):
        """Remove non-academic emails."""
        self.prospects = {
            k: v for k, v in self.prospects.items()
            if is_academic_email(v.email)
        }

    def stats(self) -> dict:
        total = len(self.prospects)
        by_source = {}
        by_university = {}
        for p in self.prospects.values():
            by_source[p.source] = by_source.get(p.source, 0) + 1
            if p.university:
                by_university[p.university] = by_university.get(p.university, 0) + 1
        return {
            "total": total,
            "by_source": by_source,
            "top_universities": dict(sorted(by_university.items(), key=lambda x: -x[1])[:20]),
        }

    def export_csv(self, path: str):
        """Export to Smartlead-compatible CSV."""
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "email", "first_name", "last_name", "company_name",
                "title", "custom1", "custom2", "custom3"
            ])
            for p in sorted(self.prospects.values(), key=lambda x: -x.confidence):
                writer.writerow([
                    p.email,
                    p.first_name,
                    p.last_name,
                    p.university,
                    p.title,
                    p.research_area[:200],     # custom1: research area
                    p.department_url,           # custom2: source URL
                    f"{p.source}:{p.confidence:.1f}",  # custom3: source:confidence
                ])
        LOG.info(f"Exported {len(self.prospects)} prospects to {path}")

    def export_json(self, path: str):
        """Export full data as JSON."""
        data = [asdict(p) for p in self.prospects.values()]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Checkpoint / Resume
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH) as f:
            return json.load(f)
    return {"completed_depts": [], "completed_pubmed": False, "prospects": []}


def save_checkpoint(data: dict):
    with open(CHECKPOINT_PATH, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Find chemistry researcher contacts for Chemister outreach")
    parser.add_argument("--source", choices=["dept", "pubmed", "openalex", "all"], default="all",
                        help="Which source to scrape (default: all)")
    parser.add_argument("--seeds", help="Custom seeds JSON file with department URLs")
    parser.add_argument("--output", "-o", default=DEFAULT_OUTPUT, help="Output CSV path")
    parser.add_argument("--ncbi-api-key", help="NCBI API key for faster PubMed access")
    parser.add_argument("--max-per-dept", type=int, default=100, help="Max faculty per department")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--academic-only", action="store_true", default=True,
                        help="Only keep academic (.edu etc) emails")
    parser.add_argument("--delay", type=float, default=2.0, help="Base delay between requests (seconds)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    session = requests.Session()
    session.headers.update(HEADERS)

    pipeline = ProspectPipeline()
    checkpoint = load_checkpoint() if args.resume else {"completed_depts": [], "completed_pubmed": False}

    # Load seeds
    seeds = SEED_DEPARTMENTS
    if args.seeds:
        with open(args.seeds) as f:
            custom = json.load(f)
        seeds = custom if isinstance(custom, list) else seeds
        LOG.info(f"Loaded {len(seeds)} custom seed departments")

    # Source 1: Department pages
    if args.source in ("dept", "all"):
        # Filter out already-completed departments
        remaining = [d for d in seeds if d["university"] not in checkpoint.get("completed_depts", [])]
        LOG.info(f"Scraping {len(remaining)} department pages...")

        scraper = DeptPageScraper(remaining, session, delay=args.delay)
        for dept in remaining:
            try:
                prospects = scraper._scrape_department(dept, args.max_per_dept)
                pipeline.add_many(prospects)
                checkpoint.setdefault("completed_depts", []).append(dept["university"])
                save_checkpoint(checkpoint)
                LOG.info(f"  {dept['university']}: {len(prospects)} contacts")
            except Exception as e:
                LOG.warning(f"  {dept['university']}: FAILED - {e}")
            polite_delay(args.delay)

    # Source 2: OpenAlex
    if args.source in ("openalex", "all") and not checkpoint.get("completed_openalex"):
        LOG.info("Mining OpenAlex for author emails (this is the big one)...")
        miner = OpenAlexMiner(session=session)
        openalex_prospects = miner.mine_all(max_pages=5, per_page=200)
        pipeline.add_many(openalex_prospects)
        checkpoint["completed_openalex"] = True
        save_checkpoint(checkpoint)
        LOG.info(f"  OpenAlex total: {len(openalex_prospects)} emails extracted")

    # Source 3: PubMed
    if args.source in ("pubmed", "all") and not checkpoint.get("completed_pubmed"):
        LOG.info("Mining PubMed for author emails...")
        miner = PubMedMiner(api_key=args.ncbi_api_key, session=session)
        pubmed_prospects = miner.mine_all(PUBMED_QUERIES)
        pipeline.add_many(pubmed_prospects)
        checkpoint["completed_pubmed"] = True
        save_checkpoint(checkpoint)

    # Filter and export
    if args.academic_only:
        before = len(pipeline.prospects)
        pipeline.filter_academic()
        LOG.info(f"Filtered to academic emails: {before} -> {len(pipeline.prospects)}")

    stats = pipeline.stats()
    LOG.info(f"\n=== RESULTS ===")
    LOG.info(f"Total unique contacts: {stats['total']}")
    LOG.info(f"By source: {stats['by_source']}")
    if stats['top_universities']:
        LOG.info(f"Top universities:")
        for uni, count in list(stats['top_universities'].items())[:10]:
            LOG.info(f"  {uni}: {count}")

    pipeline.export_csv(args.output)

    # Also export JSON for reference
    json_path = args.output.replace(".csv", ".json")
    pipeline.export_json(json_path)

    # Clean up checkpoint on successful completion
    if os.path.exists(CHECKPOINT_PATH):
        os.remove(CHECKPOINT_PATH)

    LOG.info(f"\nDone! CSV: {args.output} | JSON: {json_path}")


if __name__ == "__main__":
    main()
