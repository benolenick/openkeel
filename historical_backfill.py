#!/usr/bin/env python3
"""
historical_backfill.py — YouTube transcript backfill for NCMS.

Pulls ~1 year of daily financial media transcripts so the convergence
detection system has proper baseline data.

Strategy:
  Phase 1: For each channel, use yt-dlp flat extraction to get all video IDs.
            This is fast (single API call per channel) and doesn't trigger rate limits.
            Cache results to JSON files.
  Phase 2: For each video ID (oldest-looking first), fetch info + transcript
            in a single yt-dlp call. Check the date, and if within range, store it.
            Uses generous sleep intervals to avoid 429s.

Usage:
    python3 historical_backfill.py                          # full backfill
    python3 historical_backfill.py --start 2025-06-01 --end 2025-06-07
    python3 historical_backfill.py --test                   # quick test
    python3 historical_backfill.py --phase list             # only discover videos
    python3 historical_backfill.py --phase fetch            # only fetch transcripts
    python3 historical_backfill.py --sleep 20 40            # custom sleep range
"""
import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NCMS_ROOT = Path("/mnt/nvme/NCMS/ncms")
DB_PATH = NCMS_ROOT / "data" / "ncms.db"
CHANNELS_PATH = NCMS_ROOT / "config" / "channels.json"
TRANSCRIPT_DIR = NCMS_ROOT / "data" / "transcripts_backfill"
CACHE_DIR = NCMS_ROOT / "data" / "backfill_cache"

# Defaults (can be overridden via CLI)
DEFAULT_SLEEP_MIN = 20.0
DEFAULT_SLEEP_MAX = 40.0
SLEEP_BETWEEN_CHANNELS = (5.0, 10.0)
SLEEP_ON_429 = 600       # 10 minutes on 429
TRANSCRIPT_MIN_CHARS = 500
FLAT_PLAYLIST_LIMIT = 2500  # how many video IDs to fetch per channel

# VTT cleaning patterns
_RE_HTML_TAG = re.compile(r"<[^>]+>")
_RE_TIMESTAMP = re.compile(r"<\d{2}:\d{2}:\d{2}\.\d{3}>")
_RE_CUE_INDEX = re.compile(r"^\d+$")
_VTT_HEADERS = ("WEBVTT", "Kind:", "Language:", "align:", "position:", "size:", "line:")
_NOISE_BRACKETS = re.compile(
    r"^\[(?:music|applause|laughter|inaudible|silence|cheering)\]$", re.I
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_dir = NCMS_ROOT / "data" / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BACKFILL] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_dir / "backfill.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("backfill")


# ---------------------------------------------------------------------------
# VTT cleaning
# ---------------------------------------------------------------------------
def clean_vtt_line(line: str) -> str:
    line = _RE_HTML_TAG.sub("", line)
    line = _RE_TIMESTAMP.sub("", line)
    line = line.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    line = line.replace("\u200b", "")
    line = re.sub(r"\s+", " ", line).strip()
    if _NOISE_BRACKETS.match(line):
        return ""
    return line


def vtt_to_text(vtt_content: str) -> str:
    lines, prev = [], None
    for raw_line in vtt_content.splitlines():
        raw_line = raw_line.strip()
        if not raw_line or "-->" in raw_line:
            continue
        if raw_line.startswith(_VTT_HEADERS):
            continue
        if _RE_CUE_INDEX.match(raw_line):
            continue
        cleaned = clean_vtt_line(raw_line)
        if not cleaned or cleaned == prev:
            continue
        lines.append(cleaned)
        prev = cleaned
    return " ".join(lines)


def vtt_file_to_text(vtt_path: str) -> str:
    with open(vtt_path, "r", encoding="utf-8", errors="replace") as f:
        return vtt_to_text(f.read())


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_existing_episode_ids() -> set:
    conn = get_db()
    rows = conn.execute("SELECT episode_id FROM episodes").fetchall()
    conn.close()
    return {r["episode_id"] for r in rows}


def insert_episode(episode_id, channel_id, parent_group, url, title,
                   upload_date, transcript_chars):
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO episodes
           (episode_id, channel_id, parent_group, url, title,
            upload_date, transcript_chars, ingested_utc, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
        (episode_id, channel_id, parent_group, url, title,
         upload_date, transcript_chars, "backfill"),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Channel loading
# ---------------------------------------------------------------------------
def load_channels() -> list:
    with open(CHANNELS_PATH, "r") as f:
        data = json.load(f)
    return [ch for ch in data["channels"] if ch.get("enabled", True)]


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------
def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_video_cache(channel_id: str) -> list:
    """Load cached flat video listing. Returns list of {id, title}."""
    _ensure_cache_dir()
    cache_file = CACHE_DIR / f"flat_{channel_id}.json"
    if cache_file.exists():
        with open(cache_file, "r") as f:
            return json.load(f)
    return []


def save_video_cache(channel_id: str, videos: list):
    _ensure_cache_dir()
    cache_file = CACHE_DIR / f"flat_{channel_id}.json"
    with open(cache_file, "w") as f:
        json.dump(videos, f, indent=2)


def load_progress() -> dict:
    """Track which video IDs have been attempted (across all channels)."""
    _ensure_cache_dir()
    pf = CACHE_DIR / "progress.json"
    if pf.exists():
        with open(pf, "r") as f:
            return json.load(f)
    return {"done": [], "no_subs": [], "failed": [], "out_of_range": []}


def save_progress(progress: dict):
    _ensure_cache_dir()
    pf = CACHE_DIR / "progress.json"
    with open(pf, "w") as f:
        json.dump(progress, f)


# ---------------------------------------------------------------------------
# Phase 1: Flat-list all video IDs per channel (fast, no rate limits)
# ---------------------------------------------------------------------------
def flat_list_channel(channel_url: str, limit: int = FLAT_PLAYLIST_LIMIT) -> list:
    """
    Use extract_flat to get video IDs and titles quickly.
    Returns list of {id, title}.
    """
    url = channel_url.strip().rstrip("/")
    if re.match(r"^https?://www\.youtube\.com/@[^/]+$", url):
        url = url + "/videos"

    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "playlistend": limit,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        log.warning("Flat list failed for %s: %s", channel_url, exc)
        return []

    if not info:
        return []

    results = []
    for e in (info.get("entries") or []):
        if not e or not e.get("id"):
            continue
        vid = str(e["id"])
        if len(vid) != 11:
            continue
        results.append({
            "id": vid,
            "title": e.get("title") or "",
        })
    return results


def phase_list(channels: list):
    """Phase 1: Discover video IDs for all channels using flat extraction."""
    log.info("=" * 70)
    log.info("PHASE 1: VIDEO DISCOVERY (flat extraction)")
    log.info("=" * 70)

    total = 0
    for ch in channels:
        cid = ch["channel_id"]
        cached = load_video_cache(cid)
        if cached:
            log.info("[%s] Already cached: %d video IDs", cid, len(cached))
            total += len(cached)
            continue

        log.info("[%s] Flat-listing from %s...", cid, ch["youtube_url"])
        time.sleep(random.uniform(*SLEEP_BETWEEN_CHANNELS))

        videos = flat_list_channel(ch["youtube_url"])
        save_video_cache(cid, videos)
        total += len(videos)
        log.info("[%s] Discovered %d video IDs", cid, len(videos))

    log.info("=" * 70)
    log.info("PHASE 1 COMPLETE: %d total video IDs across all channels", total)
    log.info("=" * 70)


# ---------------------------------------------------------------------------


def save_transcript_file(video_id: str, channel_id: str, date_str: str,
                         transcript: str):
    day_dir = TRANSCRIPT_DIR / date_str / channel_id
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / f"{video_id}.txt").write_text(transcript, encoding="utf-8")


def fetch_metadata_only(video_id: str) -> tuple:
    """
    Fetch just upload_date and title for a video (no transcript).
    Returns (upload_date, title) or (None, None) on failure.
    Raises DownloadError on 429.
    """
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            if info:
                return (info.get("upload_date"), info.get("title") or "")
    except DownloadError as exc:
        if "429" in str(exc) or "rate-limited" in str(exc).lower():
            raise
    except Exception:
        pass
    return (None, None)


def fetch_transcript_only(video_id: str) -> str | None:
    """Fetch just the transcript for a video. Returns text or None."""
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "subtitlesformat": "vtt",
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "format": "all",
        "check_formats": False,
    }
    with tempfile.TemporaryDirectory() as tmp:
        opts["outtmpl"] = str(Path(tmp) / f"{video_id}.%(ext)s")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([video_url])
        except DownloadError as exc:
            if "429" in str(exc) or "rate-limited" in str(exc).lower():
                raise
            return None
        except Exception:
            return None

        vtts = [f for f in os.listdir(tmp) if f.lower().endswith(".vtt")]
        if not vtts:
            return None
        best = max(vtts, key=lambda f: os.path.getsize(os.path.join(tmp, f)))
        text = vtt_file_to_text(os.path.join(tmp, best))
        return text if text and len(text) >= 100 else None


def phase_fetch(channels: list, start_date: str, end_date: str,
                sleep_range: tuple, title_filters_map: dict):
    """
    Phase 2: Process each channel newest-first. For each video:
      1. Check metadata (get date). If date < start_date, stop channel.
      2. If date in range, fetch transcript and store.
    This avoids wasting API calls on ancient videos.
    """
    log.info("=" * 70)
    log.info("PHASE 2: TRANSCRIPT FETCHING (per-channel, newest-first)")
    log.info("Date range: %s to %s", start_date, end_date)
    log.info("Sleep between fetches: %.0f-%.0fs", sleep_range[0], sleep_range[1])
    log.info("=" * 70)

    start_compact = start_date.replace("-", "")
    end_compact = end_date.replace("-", "")

    existing_ids = get_existing_episode_ids()
    progress = load_progress()
    all_attempted = set(
        progress["done"] + progress["no_subs"] +
        progress["failed"] + progress["out_of_range"]
    )

    total_harvested = 0
    total_no_subs = 0
    total_failed = 0
    total_out_of_range = 0
    total_skipped = 0
    consecutive_429s = 0

    for ch in channels:
        cid = ch["channel_id"]
        pgroup = ch["parent_group"]
        filters = title_filters_map.get(cid, [])
        cached = load_video_cache(cid)

        if not cached:
            log.info("[%s] No cached videos. Skip.", cid)
            continue

        # Filter to pending videos (newest first, which is the natural order)
        pending = [v for v in cached
                   if v["id"] not in existing_ids and v["id"] not in all_attempted]

        log.info("[%s] %d cached | %d pending | processing newest-first...",
                 cid, len(cached), len(pending))

        if not pending:
            continue

        channel_harvested = 0
        channel_out_of_range_streak = 0
        MAX_OUT_OF_RANGE_STREAK = 10  # stop channel after 10 consecutive out-of-range

        for i, v in enumerate(pending):
            vid = v["id"]
            flat_title = v.get("title", "")

            # Apply title filters without API call
            if filters and flat_title:
                title_lower = flat_title.lower()
                if not any(kw.lower() in title_lower for kw in filters):
                    all_attempted.add(vid)
                    progress["out_of_range"].append(vid)
                    total_skipped += 1
                    continue

            log.info("  [%s %d/%d] %s | %s",
                     cid, i + 1, len(pending), vid, flat_title[:55])

            # Sleep
            sleep_time = random.uniform(*sleep_range)
            log.info("    sleeping %.0fs...", sleep_time)
            time.sleep(sleep_time)

            # Step 1: Get metadata (date check)
            try:
                upload_date, title = fetch_metadata_only(vid)
                consecutive_429s = 0
            except DownloadError:
                consecutive_429s += 1
                log.warning("    429 RATE LIMITED (consecutive: %d)", consecutive_429s)
                if consecutive_429s >= 3:
                    log.error("3 consecutive 429s. Saving progress and stopping.")
                    log.error("Wait 1+ hour, then resume with: --phase fetch")
                    save_progress(progress)
                    return
                log.info("    Pausing %d seconds...", SLEEP_ON_429)
                time.sleep(SLEEP_ON_429)
                all_attempted.add(vid)
                progress["failed"].append(vid)
                total_failed += 1
                save_progress(progress)
                continue

            if not upload_date:
                log.info("    -> No metadata, skipping")
                all_attempted.add(vid)
                progress["failed"].append(vid)
                total_failed += 1
                continue

            # Date check
            if upload_date > end_compact:
                # Newer than our range (unlikely but possible)
                log.info("    -> Date %s newer than range, skip", upload_date)
                all_attempted.add(vid)
                progress["out_of_range"].append(vid)
                total_out_of_range += 1
                channel_out_of_range_streak = 0  # reset, still in recent territory
                continue

            if upload_date < start_compact:
                # Older than our range - since list is newest-first,
                # all remaining videos are also older
                log.info("    -> Date %s older than range. Stopping channel %s.",
                         upload_date, cid)
                all_attempted.add(vid)
                progress["out_of_range"].append(vid)
                total_out_of_range += 1
                channel_out_of_range_streak += 1
                if channel_out_of_range_streak >= MAX_OUT_OF_RANGE_STREAK:
                    log.info("    -> %d consecutive out-of-range. Moving to next channel.",
                             MAX_OUT_OF_RANGE_STREAK)
                    break
                continue

            # Date is in range! Reset streak counter.
            channel_out_of_range_streak = 0

            # Step 2: Fetch transcript (separate call to avoid wasting bandwidth on out-of-range)
            log.info("    -> Date %s in range. Fetching transcript...", upload_date)
            time.sleep(random.uniform(3, 8))  # small extra delay

            try:
                transcript = fetch_transcript_only(vid)
                consecutive_429s = 0
            except DownloadError:
                consecutive_429s += 1
                log.warning("    429 on transcript fetch (consecutive: %d)", consecutive_429s)
                if consecutive_429s >= 3:
                    save_progress(progress)
                    return
                time.sleep(SLEEP_ON_429)
                all_attempted.add(vid)
                progress["failed"].append(vid)
                total_failed += 1
                save_progress(progress)
                continue

            if not transcript or len(transcript) < TRANSCRIPT_MIN_CHARS:
                log.info("    -> No usable transcript (%d chars)",
                         len(transcript) if transcript else 0)
                all_attempted.add(vid)
                progress["no_subs"].append(vid)
                total_no_subs += 1
                continue

            # Store
            date_display = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
            insert_episode(
                vid, cid, pgroup,
                f"https://www.youtube.com/watch?v={vid}",
                title or flat_title, upload_date, len(transcript),
            )
            save_transcript_file(vid, cid, date_display, transcript)

            all_attempted.add(vid)
            progress["done"].append(vid)
            total_harvested += 1
            channel_harvested += 1

            log.info("    -> OK | %s | %d chars | channel: %d | total: %d",
                     date_display, len(transcript), channel_harvested, total_harvested)

            # Checkpoint
            if total_harvested % 10 == 0:
                save_progress(progress)
                log.info("    [checkpoint saved]")

        log.info("[%s] Done. Harvested %d transcripts.", cid, channel_harvested)
        save_progress(progress)

    save_progress(progress)
    log.info("=" * 70)
    log.info("PHASE 2 COMPLETE")
    log.info("  Harvested:      %d", total_harvested)
    log.info("  No subtitles:   %d", total_no_subs)
    log.info("  Out of range:   %d", total_out_of_range)
    log.info("  Title-filtered: %d", total_skipped)
    log.info("  Failed:         %d", total_failed)
    log.info("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def backfill(start_date: str, end_date: str, phase: str, sleep_range: tuple):
    channels = load_channels()
    log.info("Loaded %d enabled channels", len(channels))

    # Build title filters map
    title_filters_map = {}
    for ch in channels:
        title_filters_map[ch["channel_id"]] = ch.get("title_filters", [])

    if phase in ("list", "both"):
        phase_list(channels)

    if phase in ("fetch", "both"):
        phase_fetch(channels, start_date, end_date, sleep_range, title_filters_map)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="NCMS Historical YouTube Transcript Backfill")
    parser.add_argument("--start", default="2025-03-24",
                        help="Start date YYYY-MM-DD (default: 2025-03-24)")
    parser.add_argument("--end", default="2026-03-24",
                        help="End date YYYY-MM-DD (default: 2026-03-24)")
    parser.add_argument("--phase", choices=["list", "fetch", "both"],
                        default="both",
                        help="Which phase to run (default: both)")
    parser.add_argument("--sleep", nargs=2, type=float,
                        default=[DEFAULT_SLEEP_MIN, DEFAULT_SLEEP_MAX],
                        metavar=("MIN", "MAX"),
                        help="Sleep range in seconds between fetches")
    parser.add_argument("--test", action="store_true",
                        help="Quick test: list 1 channel, fetch 2 transcripts")
    args = parser.parse_args()

    sleep_range = (args.sleep[0], args.sleep[1])

    if args.test:
        log.info("=" * 70)
        log.info("TEST MODE")
        log.info("=" * 70)

        channels = load_channels()[:1]  # just first channel
        ch = channels[0]
        cid = ch["channel_id"]

        # Phase 1: flat list
        log.info("[%s] Flat-listing...", cid)
        videos = flat_list_channel(ch["youtube_url"], limit=20)
        log.info("[%s] Got %d video IDs", cid, len(videos))
        for v in videos[:5]:
            log.info("  %s | %s", v["id"], v["title"][:60])

        if not videos:
            log.info("No videos found. Test failed.")
            sys.exit(1)

        # Phase 2: fetch 2 transcripts
        existing = get_existing_episode_ids()
        test_vids = [v for v in videos if v["id"] not in existing][:2]

        for v in test_vids:
            vid = v["id"]
            log.info("Fetching: %s | %s", vid, v["title"][:50])
            time.sleep(random.uniform(15, 25))

            try:
                upload_date, title = fetch_metadata_only(vid)
                transcript = fetch_transcript_only(vid) if upload_date else None
            except Exception as exc:
                log.error("Failed: %s", exc)
                continue

            log.info("  date=%s title=%s", upload_date, (title or "")[:50])
            if transcript:
                log.info("  transcript: %d chars", len(transcript))
                log.info("  preview: %s...", transcript[:200])

                # Store in DB
                insert_episode(
                    vid, cid, ch["parent_group"],
                    f"https://www.youtube.com/watch?v={vid}",
                    title or v["title"], upload_date or "", len(transcript),
                )
                date_display = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}" if upload_date and len(upload_date) == 8 else "unknown"
                save_transcript_file(vid, cid, date_display, transcript)
                log.info("  -> Stored in DB + file")
            else:
                log.info("  -> No transcript available")

        log.info("TEST COMPLETE")
    else:
        backfill(args.start, args.end, phase=args.phase, sleep_range=sleep_range)
