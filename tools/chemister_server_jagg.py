"""
Chemister Server — jagg
Full chemistry research assistant: FAISS search + DeepSeek synthesis + user auth + usage metering.
DO droplet is a dumb Caddy relay to this server.

Run: uvicorn chemister_server_jagg:app --host 0.0.0.0 --port 8300
"""

import json
import logging
import os
import re
import secrets
import sqlite3
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from typing import Optional

import bcrypt
import httpx
import jwt
import numpy as np
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ── Config ──────────────────────────────────────────────────────────────
BASE_DIR = "/mnt/nvme/FV_v4.0/chemister"
DB_PATH = os.path.join(BASE_DIR, "db", "corpus.db")
INDEX_PATH = os.path.join(BASE_DIR, "indexes", "bge_m3_flat.index")
CHUNK_ID_MAP_PATH = os.path.join(BASE_DIR, "indexes", "chunk_id_map.npy")
USERS_DB_PATH = os.path.join(BASE_DIR, "db", "users.db")
CONVERSATIONS_DIR = os.path.join(BASE_DIR, "conversations")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT = 120.0

JWT_SECRET = os.environ.get("CHEMISTER_JWT_SECRET", secrets.token_hex(32))
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 720  # 30 days

TOP_K = 8

# Tier limits
FREE_QUERIES_PER_DAY = 10
PRO_QUERIES_PER_DAY = 999999  # effectively unlimited
TRIAL_DURATION_DAYS = 90

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("chemister")

# ── Users database ──────────────────────────────────────────────────────

def init_users_db():
    """Create users + usage tables if they don't exist."""
    with get_users_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                name TEXT DEFAULT '',
                tier TEXT DEFAULT 'free',
                trial_start TEXT,
                trial_end TEXT,
                created_at TEXT NOT NULL,
                last_login TEXT
            );

            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                query_count INTEGER DEFAULT 0,
                UNIQUE(user_id, date),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                messages TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_usage_user_date ON usage(user_id, date);
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        """)


@contextmanager
def get_users_db():
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user_id: int, email: str, tier: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "tier": tier,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def get_current_user(request: Request) -> Optional[dict]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return decode_token(auth[7:])
    return None


def get_user_tier(user_id: int) -> str:
    """Get effective tier, accounting for trial expiry."""
    with get_users_db() as conn:
        user = conn.execute("SELECT tier, trial_end FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return "free"
        tier = user["tier"]
        if tier == "pro_trial" and user["trial_end"]:
            if datetime.fromisoformat(user["trial_end"]) < datetime.now(timezone.utc):
                # Trial expired — downgrade
                conn.execute("UPDATE users SET tier = 'free' WHERE id = ?", (user_id,))
                return "free"
        return tier


def get_daily_query_count(user_id: int) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_users_db() as conn:
        row = conn.execute(
            "SELECT query_count FROM usage WHERE user_id = ? AND date = ?",
            (user_id, today)
        ).fetchone()
        return row["query_count"] if row else 0


def increment_query_count(user_id: int) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with get_users_db() as conn:
        conn.execute("""
            INSERT INTO usage (user_id, date, query_count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, date)
            DO UPDATE SET query_count = query_count + 1
        """, (user_id, today))
        row = conn.execute(
            "SELECT query_count FROM usage WHERE user_id = ? AND date = ?",
            (user_id, today)
        ).fetchone()
        return row["query_count"]


def get_daily_limit(tier: str) -> int:
    if tier in ("pro", "pro_trial"):
        return PRO_QUERIES_PER_DAY
    return FREE_QUERIES_PER_DAY


# ── Rate limiting ───────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self._hits: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str, max_requests: int, window_sec: int = 60) -> bool:
        now = time.time()
        cutoff = now - window_sec
        hits = self._hits[key]
        self._hits[key] = [t for t in hits if t > cutoff]
        if len(self._hits[key]) >= max_requests:
            return False
        self._hits[key].append(now)
        return True

_rate_limiter = RateLimiter()

_login_fails: dict[str, dict] = {}
LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION = 900

def _check_lockout(ip: str) -> bool:
    info = _login_fails.get(ip)
    if not info:
        return False
    if info.get("locked_until", 0) > time.time():
        return True
    if info.get("locked_until", 0) > 0 and info["locked_until"] <= time.time():
        del _login_fails[ip]
    return False

def _record_login_fail(ip: str):
    now = time.time()
    info = _login_fails.get(ip)
    if not info:
        _login_fails[ip] = {"count": 1, "first": now, "locked_until": 0}
        return
    if now - info["first"] > LOCKOUT_DURATION:
        _login_fails[ip] = {"count": 1, "first": now, "locked_until": 0}
        return
    info["count"] += 1
    if info["count"] >= LOCKOUT_THRESHOLD:
        info["locked_until"] = now + LOCKOUT_DURATION

def _record_login_success(ip: str):
    _login_fails.pop(ip, None)

def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── FAISS search ────────────────────────────────────────────────────────
_faiss_index = None
_chunk_id_map = None
_embed_model = None

def load_search_engine():
    """Load FAISS index and embedding model."""
    global _faiss_index, _chunk_id_map, _embed_model
    import faiss

    log.info("Loading FAISS index from %s...", INDEX_PATH)
    _faiss_index = faiss.read_index(INDEX_PATH)
    log.info("FAISS index loaded: %d vectors", _faiss_index.ntotal)

    log.info("Loading chunk ID map...")
    _chunk_id_map = np.load(CHUNK_ID_MAP_PATH, allow_pickle=True)
    log.info("Chunk ID map loaded: %d entries", len(_chunk_id_map))

    log.info("Loading BGE-M3 embedding model...")
    from sentence_transformers import SentenceTransformer
    _embed_model = SentenceTransformer("BAAI/bge-m3", device="cuda:0")
    log.info("Embedding model loaded on GPU")


def search_papers(query: str, top_k: int = TOP_K) -> list[dict]:
    """Search FAISS index and return paper chunks with metadata."""
    if _faiss_index is None or _embed_model is None:
        return []

    # Embed query
    query_vec = _embed_model.encode([query], normalize_embeddings=True)
    query_vec = query_vec.astype(np.float32)

    # Search
    scores, indices = _faiss_index.search(query_vec, top_k)

    # Batch fetch all chunk_ids
    chunk_ids = []
    valid = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(_chunk_id_map):
            continue
        chunk_ids.append(_chunk_id_map[idx])
        valid.append(float(score))

    if not chunk_ids:
        return []

    # Fetch chunks + paper metadata from corpus DB
    results = []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = conn.execute(f"""
            SELECT c.chunk_id, c.text, c.doi, c.section_header,
                   p.filename, p.journal, p.year
            FROM chunks c
            LEFT JOIN papers p ON c.paper_id = p.paper_id
            WHERE c.chunk_id IN ({placeholders})
        """, chunk_ids).fetchall()
        conn.close()

        # Map by chunk_id for ordering
        row_map = {r["chunk_id"]: r for r in rows}
        for cid, score in zip(chunk_ids, valid):
            row = row_map.get(cid)
            if row:
                # Use filename as title fallback (often contains paper title info)
                title = row["journal"] or row["filename"] or "Untitled"
                if row["year"]:
                    title += f" ({row['year']})"
                results.append({
                    "text": row["text"],
                    "title": title,
                    "doi": row["doi"] or "",
                    "section": row["section_header"] or "",
                    "score": score,
                })
    except Exception as e:
        log.error("Chunk fetch error: %s", e)

    return results


# ── Structure search (SMILES → PubChem ECFP similarity) ────────────────
PUBCHEM_ECFP_PATH = os.path.join(BASE_DIR, "indexes", "pubchem_ecfp.index")
PUBCHEM_CIDS_PATH = os.path.join(BASE_DIR, "indexes", "pubchem_cids.npy")

_struct_index = None
_pubchem_cids = None

def load_structure_search():
    """Load PubChem ECFP fingerprint index."""
    global _struct_index, _pubchem_cids
    import faiss
    if not os.path.exists(PUBCHEM_ECFP_PATH):
        log.warning("PubChem ECFP index not found, structure search disabled")
        return
    log.info("Loading PubChem ECFP index...")
    _struct_index = faiss.read_index(PUBCHEM_ECFP_PATH)
    log.info("PubChem ECFP index: %d compounds", _struct_index.ntotal)
    _pubchem_cids = np.load(PUBCHEM_CIDS_PATH)
    log.info("PubChem CIDs loaded: %d", len(_pubchem_cids))


def structure_search_smiles(smiles: str, top_k: int = 10) -> list[dict]:
    """Search for similar molecules by SMILES string."""
    from rdkit import Chem
    from rdkit.Chem import AllChem

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return []

    # Generate ECFP fingerprint (Morgan radius 2, 2048 bits)
    fp = AllChem.GetMorganFingerprintAsBitVect(mol, radius=2, nBits=2048)
    fp_array = np.zeros(2048, dtype=np.float32)
    for bit in fp.GetOnBits():
        fp_array[bit] = 1.0
    fp_array = fp_array.reshape(1, -1)

    if _struct_index is None:
        # Fallback: text search in corpus for SMILES string
        return _text_search_smiles(smiles)

    scores, indices = _struct_index.search(fp_array, top_k)

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(_pubchem_cids):
            continue
        cid = int(_pubchem_cids[idx])
        results.append({
            "cid": cid,
            "similarity": float(score),
            "pubchem_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        })

    # Also search corpus for mentions of this SMILES
    corpus_hits = _text_search_smiles(smiles, limit=5)
    return {"similar_compounds": results, "paper_mentions": corpus_hits}


def _text_search_smiles(smiles: str, limit: int = 10) -> list[dict]:
    """Search corpus text for SMILES string mentions."""
    results = []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT c.text, c.doi, c.section_header, p.filename, p.journal, p.year
            FROM chunks c
            LEFT JOIN papers p ON c.paper_id = p.paper_id
            WHERE c.text LIKE ? LIMIT ?
        """, (f"%{smiles}%", limit)).fetchall()
        conn.close()
        for r in rows:
            title = r["journal"] or r["filename"] or "Untitled"
            if r["year"]:
                title += f" ({r['year']})"
            results.append({
                "text": r["text"][:500],
                "title": title,
                "doi": r["doi"] or "",
                "section": r["section_header"] or "",
            })
    except Exception as e:
        log.error("Text search error: %s", e)
    return results


# ── DeepSeek synthesis ─────────────────────────────────────────────────
async def call_deepseek(prompt: str, system: str = None) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    async with httpx.AsyncClient(timeout=DEEPSEEK_TIMEOUT) as client:
        resp = await client.post(
            DEEPSEEK_URL,
            json={
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "temperature": 0.4,
                "max_tokens": 2048,
            },
            headers={
                "Authorization": "Bearer " + DEEPSEEK_API_KEY,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()


def build_prompt(question: str, chunks: list[dict], history: list = None) -> tuple:
    parts = []
    for i, c in enumerate(chunks, 1):
        header = f"[Source {i}] {c.get('title', 'Unknown')}"
        if c.get("doi"):
            header += f" (DOI: {c['doi']})"
        parts.append(header + "\n" + c.get("text", ""))

    context = "\n\n---\n\n".join(parts)

    system = (
        "You are Chemister, an expert chemistry research assistant. "
        "You answer questions using excerpts from real chemistry research papers.\n\n"
        "INSTRUCTIONS:\n"
        "- Answer thoroughly and accurately based on the provided paper excerpts\n"
        "- Use clear, well-structured prose suitable for a chemistry-literate audience\n"
        "- Reference source numbers like [Source 1] when citing\n"
        "- If excerpts don't fully answer, say so honestly\n"
        "- Do NOT invent facts not present in the excerpts\n"
        "- Use markdown: **bold** for key terms, bullet points for lists"
    )

    hist_text = ""
    if history:
        for msg in history[-6:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            hist_text += f"{'User' if role == 'user' else 'Assistant'}: {content}\n"

    prompt = "RESEARCH PAPER EXCERPTS:\n" + context + "\n\n"
    if hist_text.strip():
        prompt += "CONVERSATION HISTORY:\n" + hist_text + "\n"
    prompt += "QUESTION: " + question + "\n\nProvide a comprehensive answer based on the paper excerpts above."

    return prompt, system


# ── Conversation persistence ───────────────────────────────────────────
def _load_conversation(user_id: int) -> list:
    with get_users_db() as conn:
        row = conn.execute("SELECT messages FROM conversations WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            try:
                return json.loads(row["messages"])
            except Exception:
                pass
    return []


def _save_conversation(user_id: int, messages: list):
    now = datetime.now(timezone.utc).isoformat()
    with get_users_db() as conn:
        conn.execute("""
            INSERT INTO conversations (user_id, messages, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET messages = excluded.messages, updated_at = excluded.updated_at
        """, (user_id, json.dumps(messages, ensure_ascii=False), now))


# ── FastAPI app ─────────────────────────────────────────────────────────
app = FastAPI(title="Chemister", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chemistry.automaite.ca", "http://localhost:3000"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

startup_time = time.time()


@app.on_event("startup")
async def on_startup():
    init_users_db()
    try:
        load_search_engine()
    except Exception as e:
        log.error("Failed to load search engine: %s", e)
        log.error(traceback.format_exc())
    try:
        load_structure_search()
    except Exception as e:
        log.warning("Structure search not available: %s", e)


# ── Request models ──────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    email: str
    password: str
    name: str = ""

    @field_validator("email")
    @classmethod
    def validate_email(cls, v):
        v = v.strip().lower()
        if len(v) < 2 or len(v) > 200:
            raise ValueError("Invalid username/email")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


class ChatRequest(BaseModel):
    message: str
    history: list = []

    @field_validator("message")
    @classmethod
    def validate_message(cls, v):
        if len(v) > 2000:
            raise ValueError("Message too long (max 2000 characters)")
        return v


class StructSearchRequest(BaseModel):
    smiles: str

    @field_validator("smiles")
    @classmethod
    def validate_smiles(cls, v):
        if len(v) > 500:
            raise ValueError("SMILES string too long")
        return v


# ── Endpoints ───────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "chemister-jagg",
        "uptime_sec": round(time.time() - startup_time, 1),
        "vectors": _faiss_index.ntotal if _faiss_index else 0,
    }


@app.post("/api/signup")
async def signup(req: SignupRequest, request: Request, trial: str = Query(default="")):
    ip = _get_client_ip(request)
    if not _rate_limiter.is_allowed(ip + ":signup", max_requests=3, window_sec=300):
        raise HTTPException(429, detail="Too many signup attempts")

    email = req.email.strip().lower()
    now = datetime.now(timezone.utc)

    # Determine tier
    tier = "free"
    trial_start = None
    trial_end = None
    if trial:
        tier = "pro_trial"
        trial_start = now.isoformat()
        trial_end = (now + timedelta(days=TRIAL_DURATION_DAYS)).isoformat()

    with get_users_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise HTTPException(409, detail="Account already exists. Please log in.")

        pw_hash = hash_password(req.password)
        conn.execute("""
            INSERT INTO users (email, password_hash, name, tier, trial_start, trial_end, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (email, pw_hash, req.name, tier, trial_start, trial_end, now.isoformat()))

        user = conn.execute("SELECT id, tier FROM users WHERE email = ?", (email,)).fetchone()

    token = create_token(user["id"], email, user["tier"])
    log.info("Signup: %s tier=%s from %s", email, tier, ip)

    return {
        "token": token,
        "user": {
            "email": email,
            "name": req.name,
            "tier": tier,
            "trial_end": trial_end,
            "daily_limit": get_daily_limit(tier),
            "queries_today": 0,
        },
    }


@app.post("/api/login")
async def login(req: LoginRequest, request: Request):
    ip = _get_client_ip(request)

    if _check_lockout(ip):
        raise HTTPException(429, detail="Too many failed attempts. Try again later.")
    if not _rate_limiter.is_allowed(ip + ":login", max_requests=5):
        raise HTTPException(429, detail="Rate limit exceeded")

    email = req.email.strip().lower()

    with get_users_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password(req.password, user["password_hash"]):
            _record_login_fail(ip)
            raise HTTPException(401, detail="Invalid credentials")

        _record_login_success(ip)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user["id"]))

    tier = get_user_tier(user["id"])
    token = create_token(user["id"], email, tier)
    conversation = _load_conversation(user["id"])
    queries_today = get_daily_query_count(user["id"])

    log.info("Login: %s tier=%s from %s", email, tier, ip)

    return {
        "token": token,
        "user": {
            "email": email,
            "name": user["name"],
            "tier": tier,
            "trial_end": user["trial_end"],
            "daily_limit": get_daily_limit(tier),
            "queries_today": queries_today,
        },
        "history": conversation,
    }


@app.get("/api/me")
async def me(request: Request):
    """Get current user info + usage."""
    payload = get_current_user(request)
    if not payload:
        raise HTTPException(401, detail="Unauthorized")

    user_id = int(payload["sub"])
    tier = get_user_tier(user_id)
    queries_today = get_daily_query_count(user_id)
    daily_limit = get_daily_limit(tier)

    with get_users_db() as conn:
        user = conn.execute("SELECT email, name, trial_end FROM users WHERE id = ?", (user_id,)).fetchone()

    return {
        "email": user["email"],
        "name": user["name"],
        "tier": tier,
        "trial_end": user["trial_end"],
        "daily_limit": daily_limit,
        "queries_today": queries_today,
        "queries_remaining": max(0, daily_limit - queries_today),
    }


def _check_usage_or_raise(request: Request) -> tuple[int, str]:
    """Check auth + usage limits. Returns (user_id, tier) or raises."""
    payload = get_current_user(request)
    if not payload:
        raise HTTPException(401, detail="Unauthorized")

    user_id = int(payload["sub"])
    tier = get_user_tier(user_id)
    daily_limit = get_daily_limit(tier)
    queries_today = get_daily_query_count(user_id)

    if queries_today >= daily_limit:
        raise HTTPException(
            429,
            detail={
                "error": "daily_limit_reached",
                "message": f"You've used all {daily_limit} queries for today.",
                "tier": tier,
                "queries_today": queries_today,
                "daily_limit": daily_limit,
                "upgrade_url": "https://chemistry.automaite.ca/upgrade",
                "resets_at": (datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0) + timedelta(days=1)).isoformat(),
            }
        )

    return user_id, tier


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    ip = _get_client_ip(request)
    if not _rate_limiter.is_allowed(ip + ":chat", max_requests=15):
        raise HTTPException(429, detail="Rate limit exceeded")

    user_id, tier = _check_usage_or_raise(request)
    question = req.message.strip()
    if not question:
        raise HTTPException(400, detail="Empty message")

    log.info("Query from user %d: %s", user_id, question[:120])

    # 1. Search
    chunks = search_papers(question)
    log.info("Retrieved %d chunks", len(chunks))

    # 2. Build sources
    sources = []
    for c in chunks:
        sources.append({
            "title": c.get("title", "Untitled"),
            "doi": c.get("doi"),
            "snippet": c.get("text", "")[:300],
            "score": c.get("score"),
        })

    # 3. Synthesize
    try:
        if chunks:
            prompt, system = build_prompt(question, chunks, req.history)
            answer = await call_deepseek(prompt, system)
        else:
            answer = await call_deepseek(
                "The paper search returned no results. Answer briefly if you can, "
                "noting no paper citations are available.\n\nQuestion: " + question,
                "You are Chemister, a chemistry research assistant."
            )
    except Exception as e:
        log.error("DeepSeek error: %s", e)
        answer = "Sorry, the synthesis service is temporarily unavailable. Please try again."

    # 4. Increment usage
    new_count = increment_query_count(user_id)
    daily_limit = get_daily_limit(tier)

    # 5. Persist conversation
    now = datetime.now(timezone.utc).isoformat()
    conversation = _load_conversation(user_id)
    conversation.append({"role": "user", "content": question, "timestamp": now})
    conversation.append({"role": "assistant", "content": answer, "sources": sources, "timestamp": now})
    _save_conversation(user_id, conversation)

    return {
        "answer": answer,
        "sources": sources,
        "usage": {
            "queries_today": new_count,
            "daily_limit": daily_limit,
            "queries_remaining": max(0, daily_limit - new_count),
        },
    }


@app.post("/api/search")
async def api_search(request: Request):
    """Direct search endpoint (for structure search etc)."""
    user_id, tier = _check_usage_or_raise(request)

    body = await request.json()
    query = body.get("query", "").strip()
    top_k = min(body.get("top_k", TOP_K), 20)

    if not query:
        raise HTTPException(400, detail="Empty query")

    chunks = search_papers(query, top_k)
    increment_query_count(user_id)

    return {"results": chunks}


@app.get("/api/history")
async def get_history(request: Request):
    payload = get_current_user(request)
    if not payload:
        raise HTTPException(401, detail="Unauthorized")
    user_id = int(payload["sub"])
    return {"history": _load_conversation(user_id)}


@app.delete("/api/history")
async def delete_history(request: Request):
    payload = get_current_user(request)
    if not payload:
        raise HTTPException(401, detail="Unauthorized")
    user_id = int(payload["sub"])
    _save_conversation(user_id, [])
    return {"status": "cleared"}


@app.post("/api/structure-search")
async def api_structure_search(req: StructSearchRequest, request: Request):
    """Search for similar molecules by SMILES."""
    ip = _get_client_ip(request)
    if not _rate_limiter.is_allowed(ip + ":struct", max_requests=10):
        raise HTTPException(429, detail="Rate limit exceeded")

    user_id, tier = _check_usage_or_raise(request)

    results = structure_search_smiles(req.smiles)
    increment_query_count(user_id)
    return results


# ── Admin endpoints (for Ben) ──────────────────────────────────────────

ADMIN_EMAILS = {"ben.olenick@gmail.com", "ben@kwr.kr"}

def _require_admin(request: Request) -> int:
    payload = get_current_user(request)
    if not payload or payload.get("email") not in ADMIN_EMAILS:
        raise HTTPException(403, detail="Admin only")
    return int(payload["sub"])


@app.get("/api/admin/users")
async def admin_users(request: Request):
    """List all users with usage stats."""
    _require_admin(request)
    with get_users_db() as conn:
        users = conn.execute("""
            SELECT u.id, u.email, u.name, u.tier, u.trial_end, u.created_at, u.last_login,
                   COALESCE(SUM(g.query_count), 0) as total_queries,
                   MAX(g.date) as last_active_date
            FROM users u
            LEFT JOIN usage g ON u.id = g.user_id
            GROUP BY u.id
            ORDER BY total_queries DESC
        """).fetchall()
    return [dict(u) for u in users]


@app.get("/api/admin/universities")
async def admin_universities(request: Request):
    """Show university clusters (users grouped by email domain)."""
    _require_admin(request)
    with get_users_db() as conn:
        users = conn.execute("""
            SELECT u.email, u.tier, COALESCE(SUM(g.query_count), 0) as total_queries
            FROM users u
            LEFT JOIN usage g ON u.id = g.user_id
            GROUP BY u.id
        """).fetchall()

    # Group by domain
    clusters: dict[str, list] = defaultdict(list)
    for u in users:
        domain = u["email"].split("@")[1] if "@" in u["email"] else "unknown"
        clusters[domain].append({
            "email": u["email"],
            "tier": u["tier"],
            "total_queries": u["total_queries"],
        })

    # Sort by number of users (institutional upsell targets)
    result = []
    for domain, members in sorted(clusters.items(), key=lambda x: -len(x[1])):
        result.append({
            "domain": domain,
            "user_count": len(members),
            "total_queries": sum(m["total_queries"] for m in members),
            "users": members,
        })

    return result


@app.get("/api/admin/usage")
async def admin_usage(request: Request, days: int = Query(default=7)):
    """Usage stats for the last N days."""
    _require_admin(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_users_db() as conn:
        rows = conn.execute("""
            SELECT date, SUM(query_count) as total, COUNT(DISTINCT user_id) as unique_users
            FROM usage
            WHERE date >= ?
            GROUP BY date
            ORDER BY date
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8300, log_level="info")
