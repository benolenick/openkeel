"""Token Saver Proxy — Phase 0 passthrough with measurement tap.

Runs on 127.0.0.1:8787. Point Claude Code at it via:
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8787

Phase 0 goal: byte-identical passthrough + per-turn usage logging to
~/.openkeel/proxy_trace.jsonl. No rewriting yet. Measure first.

Non-negotiables (see docs/token_saver_final.md):
  - SSE must stream with aiter_raw, never buffer
  - Headers passed through exactly (case/order preserved)
  - Any exception → fall through to upstream untouched
  - Latency budget 200ms or bypass
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

UPSTREAM = "https://api.anthropic.com"
TRACE_PATH = Path(os.path.expanduser("~/.openkeel/proxy_trace.jsonl"))
TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI()
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0, connect=10.0), http2=True)

HOP_BY_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
              "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate",
              "accept-encoding"}  # strip so upstream sends plaintext SSE

MMR_MODULES = (
    # "mmr_advanced.py",
    # "multi_model_router.py",
)


def _clean_headers(h) -> list[tuple[str, str]]:
    return [(k, v) for k, v in h.items() if k.lower() not in HOP_BY_HOP]


def _log(entry: dict) -> None:
    try:
        entry["ts"] = time.time()
        with TRACE_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


async def _startup_self_test() -> None:
    """Fail fast on local misconfiguration, warn on upstream reachability.

    This is intentionally split into:
    - hard failures for local invariants the proxy itself controls
    - soft warnings for upstream connectivity, which may fluctuate
    """
    route_paths = {
        getattr(route, "path", None)
        for route in app.router.routes
        if getattr(route, "path", None)
    }

    required_local_routes = {"/health", "/", "/{path:path}"}
    missing = sorted(required_local_routes - route_paths)
    if missing:
        raise RuntimeError(f"startup self-test failed: missing routes {missing}")

    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TRACE_PATH.open("a"):
            pass
    except Exception as e:
        raise RuntimeError(f"startup self-test failed: trace path not writable: {e}") from e

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0), http2=True) as probe:
            resp = await probe.get(UPSTREAM)
        _log({
            "event": "startup_self_test",
            "ok": True,
            "upstream_status": resp.status_code,
        })
    except Exception as e:
        _log({
            "event": "startup_self_test",
            "ok": True,
            "warning": f"upstream_probe_failed:{type(e).__name__}",
        })


@app.on_event("startup")
async def startup_check() -> None:
    await _startup_self_test()


# --- Rewriters (try-safe, fall-through on exception) ---
STRIP_PREFIXES = (
    "<system-reminder>\nSessionStart:startup hook success: [TOKEN SAVER]",
    "<system-reminder>\nSessionStart:compact hook success: [TOKEN SAVER]",
    "<system-reminder>\nSessionStart:startup hook success: [OPENKEEL HYPHAE]",
    "<system-reminder>\nSessionStart:compact hook success: [OPENKEEL HYPHAE]",
)


# Tool schema diet: strip rarely-used tools from tools[]
STRIP_TOOL_NAMES = frozenset({
    "ExitPlanMode", "EnterPlanMode",
    "CronCreate", "CronDelete", "CronList",
    "EnterWorktree", "ExitWorktree",
    "RemoteTrigger", "NotebookEdit",
    "TaskStop", "TaskOutput",
})

def _diet_tools(data: dict) -> bool:
    try:
        tools = data.get("tools", [])
        if not tools:
            return False
        used = set()
        for m in data.get("messages", []):
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        used.add(b.get("name",""))
        safe = STRIP_TOOL_NAMES - used
        new_tools = [t for t in tools if t.get("name") not in safe]
        if len(new_tools) == len(tools):
            return False
        data["tools"] = new_tools
        return True
    except Exception:
        return False


# History eviction: truncate old large tool_results
EVICT_MIN_AGE = 20
EVICT_MIN_CHARS = 5000
EVICT_KEEP_CHARS = 800

# Rate limit protection: hard body-size ceiling for premium models
# 429 typically happens around 250k+ chars on Sonnet, 500k+ on Opus
# We're hitting limits at 200k+, so cap aggressively
SONNET_MAX_BODY_CHARS = 150000
OPUS_MAX_BODY_CHARS = 300000
HAIKU_MAX_BODY_CHARS = 50000

def _evict_history(data: dict, body_chars: int = 0) -> bool:
    try:
        msgs = data.get("messages", [])
        n = len(msgs)
        if n < 2:
            return False

        # Aggressive eviction if body is already huge (rate limit zone)
        if body_chars > SONNET_MAX_BODY_CHARS:
            # Evict everything except last 3 messages
            cutoff = max(0, n - 3)
            min_chars_threshold = 1000  # Evict anything > 1k
            evict_keep = 300  # Keep only 300 chars of old results
        elif body_chars > SONNET_MAX_BODY_CHARS * 0.7:
            # Evict older than 10 messages
            cutoff = max(0, n - 10)
            min_chars_threshold = EVICT_MIN_CHARS
            evict_keep = EVICT_KEEP_CHARS // 2
        else:
            # Normal eviction (old behavior)
            if n < EVICT_MIN_AGE:
                return False
            cutoff = n - EVICT_MIN_AGE
            min_chars_threshold = EVICT_MIN_CHARS
            evict_keep = EVICT_KEEP_CHARS

        changed = False
        for msg in msgs[:cutoff]:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                inner = block.get("content")
                if isinstance(inner, str) and len(inner) > min_chars_threshold:
                    block["content"] = inner[:evict_keep] + f"\n\n[...{len(inner)-evict_keep} chars evicted by token_saver_proxy — re-run tool if needed]"
                    changed = True
                elif isinstance(inner, list):
                    for b in inner:
                        if isinstance(b, dict) and b.get("type") == "text":
                            t = b.get("text", "")
                            if len(t) > min_chars_threshold:
                                b["text"] = t[:evict_keep] + f"\n\n[...{len(t)-evict_keep} chars evicted by token_saver_proxy — re-run tool if needed]"
                                changed = True
        return changed
    except Exception:
        return False


# Model routing: trivial turns → Haiku (1/12 Opus cost)
HAIKU_MODEL = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"

# Local LLM classifier (qwen2.5:3b on jagg)
OLLAMA_URL = os.environ.get("TSPROXY_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("TSPROXY_OLLAMA_MODEL", "gemma3:1b")
CLASSIFIER_TIMEOUT = 2.0

def _qwen_classify(user_text: str) -> dict:
    """Returns {model, confidence, reason, latency_ms, ok}.

    model: "haiku" | "sonnet" | "opus" | None
    confidence: "high" | "medium" | "low" | None
    """
    result = {"model": None, "confidence": None, "reason": "", "latency_ms": 0, "ok": False}
    if not user_text or len(user_text) < 3:
        result["reason"] = "empty_text"
        return result
    t0 = time.time()
    try:
        import urllib.request
        prompt = (
            "Default is SONNET. Only pick opus for explicitly hard tasks. Only pick haiku for truly trivial one-line questions.\n\n"
            "Rules:\n"
            "- haiku: math, yes/no, single lookup (e.g. 'what is 5+3', 'say hi', 'what port does X use')\n"
            "- sonnet: ALL normal coding (write/edit/debug/test/refactor/explain). When in doubt, pick this.\n"
            "- opus: ONLY if the turn explicitly says 'architect', 'think hard', 'security audit', or is a multi-file gnarly bug.\n\n"
            f"Turn: {user_text[:800]}\n\n"
            "One word answer (haiku|sonnet|opus):"
        )
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "system": "You are a turn classifier. Output ONE WORD: haiku, sonnet, or opus. Nothing else.",
            "prompt": prompt, "stream": False,
            "options": {"temperature": 0.0, "num_predict": 4},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=CLASSIFIER_TIMEOUT) as resp:
            data = json.loads(resp.read())
        ans = (data.get("response") or "").strip().lower().split()
        result["latency_ms"] = int((time.time() - t0) * 1000)
        result["ok"] = True
        if not ans:
            result["reason"] = "empty_response"
            return result
        m = ans[0]
        if m.startswith("hai"):
            result["model"] = "haiku"
        elif m.startswith("son"):
            result["model"] = "sonnet"
        elif m.startswith("opu"):
            result["model"] = "opus"
        else:
            result["reason"] = f"unknown:{m}"
            return result
        result["reason"] = f"qwen:{m}"
    except Exception as e:
        result["reason"] = f"error:{type(e).__name__}"
        result["latency_ms"] = int((time.time() - t0) * 1000)
    return result


# Hard rules override the classifier
FORCE_OPUS_KEYWORDS = ("think hard", "ultrathink", "carefully", "architect", "audit the", "security", "vulnerab")
FORCE_SONNET_MAX_KEYWORDS = ("quick", "just ", "simple ", "real quick")

# Haiku: aggressive threshold
HAIKU_MAX_USER_CHARS = 1500
HAIKU_COMPLEX_BLOCKERS = (
    "refactor", "debug the", "implement", "build a", "design",
    "explain why", "architectural", "security", "audit",
    "create a", "write a class", "write a module", "write code",
)

# Sonnet: default for most coding / conversational work
SONNET_KEEP_OPUS_KEYWORDS = (
    "architect", "think hard", "ultrathink", "audit", "security review",
    "refactor the entire", "gnarly", "tricky",
    "should we use", "which approach",
)


def _extract_user_text(data: dict) -> str:
    """Pull the user's actual typed message out of messages[-1].content (skipping system-reminders)."""
    msgs = data.get("messages", [])
    if not msgs:
        return ""
    # Scan from the end backward for the most recent user message with real text.
    # Earlier impl only looked at msgs[-1]; on multi-turn requests that's usually
    # a tool_result turn with no free text → routing bailed with no_user_text and
    # Opus was never reclassified.
    for m in reversed(msgs):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            s = content.strip()
            if s and not s.startswith("<system-reminder>"):
                return content
            continue
        if isinstance(content, list):
            texts = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    t = b.get("text", "")
                    if t and not t.startswith("<system-reminder>"):
                        texts.append(t)
            if texts:
                return min(texts, key=len)
    return ""


def _classify_model(data: dict, body_chars: int = 0) -> tuple:
    """Returns (target_model_or_None, routing_decision_dict).
    routing_decision has: source, confidence, reason, qwen_latency_ms, body_chars
    """
    decision = {"source": "none", "confidence": None, "reason": "", "qwen_latency_ms": 0, "user_chars": 0, "body_chars": body_chars}
    try:
        msgs = data.get("messages", [])
        if not msgs:
            decision["reason"] = "no_messages"
            return None, decision

        user_text = _extract_user_text(data)
        decision["user_chars"] = len(user_text)
        low = user_text.lower()
        if not user_text:
            decision["reason"] = "no_user_text"
            return None, decision

        # Safety gate 0: body-size protection against rate limits
        if body_chars > OPUS_MAX_BODY_CHARS:
            decision["source"] = "safety_gate"
            decision["reason"] = f"body_too_large:{body_chars}"
            return HAIKU_MODEL, decision

        # Hard rule 1: force-opus keywords (but downgrade if body approaching limit)
        for kw in FORCE_OPUS_KEYWORDS:
            if kw in low:
                if body_chars > OPUS_MAX_BODY_CHARS * 0.8:
                    decision["source"] = "hard_rule_opus_downgr"
                    decision["reason"] = f"opus_keyword_but_body_near_limit"
                    return SONNET_MODEL, decision
                decision["source"] = "hard_rule_opus"
                decision["reason"] = f"matched:{kw}"
                return None, decision

        # Hard rule 2: "quick"/"simple"/"just" → Sonnet max
        for kw in FORCE_SONNET_MAX_KEYWORDS:
            if kw in low:
                decision["source"] = "hard_rule_sonnet"
                decision["reason"] = f"matched:{kw}"
                return SONNET_MODEL, decision

        # Check for tool history
        has_tools = False
        for m in msgs[:-1]:
            c = m.get("content")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result"):
                        has_tools = True
                        break
            if has_tools:
                break

        # qwen2.5:3b classifier + length-based Haiku gate
        if not os.environ.get("TSPROXY_NO_QWEN_ROUTER"):
            q = _qwen_classify(user_text)
            decision["qwen_latency_ms"] = q.get("latency_ms", 0)
            if q.get("ok") and q.get("model"):
                decision["source"] = "qwen"
                decision["reason"] = q["reason"]
                model = q["model"]
                if model == "haiku":
                    # Haiku gate: short AND no tool history AND no complex blockers
                    short_enough = len(user_text) <= HAIKU_MAX_USER_CHARS
                    has_blocker = any(kw in low for kw in HAIKU_COMPLEX_BLOCKERS)
                    if not short_enough or has_blocker:
                        decision["reason"] += " (demoted:gate_fail)"
                        return SONNET_MODEL, decision
                    decision["confidence"] = "high"
                    return HAIKU_MODEL, decision
                if model == "sonnet":
                    decision["confidence"] = "medium"
                    return SONNET_MODEL, decision
                if model == "opus":
                    decision["confidence"] = "high"
                    return None, decision
            else:
                decision["reason"] = q.get("reason", "qwen_failed")

        # Fallback heuristic
        decision["source"] = "fallback_heuristic"
        if len(user_text) <= HAIKU_MAX_USER_CHARS:
            if not any(kw in low for kw in HAIKU_COMPLEX_BLOCKERS):
                decision["reason"] = "short_no_blockers"
                return HAIKU_MODEL, decision
        # Default to Sonnet unless OPUS keywords present → aggressively downgrade
        if any(kw in low for kw in SONNET_KEEP_OPUS_KEYWORDS):
            decision["reason"] = "keep_opus_keyword"
            return None, decision
        decision["reason"] = "default_sonnet"
        return SONNET_MODEL, decision
    except Exception as e:
        decision["reason"] = f"exception:{type(e).__name__}"
        return None, decision


def _rewrite_body(body: bytes):
    """Returns (new_body_bytes, target_model_or_none, route_decision_dict)."""
    routed = None
    route_decision = None
    try:
        data = json.loads(body)
    except Exception:
        return body, routed, route_decision
    try:
        changed = False
        orig_body_chars = len(body)
        NO_SESSID = os.environ.get("TSPROXY_NO_SESSID")
        NO_STRIP = os.environ.get("TSPROXY_NO_STRIP")
        NO_MARKER = os.environ.get("TSPROXY_NO_MARKER")
        NO_ROUTER = os.environ.get("TSPROXY_NO_ROUTER")
        NO_EVICT = os.environ.get("TSPROXY_NO_EVICT")
        NO_DIET = os.environ.get("TSPROXY_NO_DIET")
        if not NO_DIET and _diet_tools(data):
            changed = True
        if not NO_EVICT and _evict_history(data, body_chars=orig_body_chars):
            changed = True
        # Model router: 3-way classify (opus / sonnet / haiku)
        if not NO_ROUTER and data.get("model","").startswith("claude-opus"):
            target, route_decision = _classify_model(data, body_chars=orig_body_chars)
            if target:
                data["model"] = target
                if target == HAIKU_MODEL:
                    for k in ("thinking", "output_config", "context_management"):
                        data.pop(k, None)
                routed = target
                changed = True
        # (route_decision captured for handler to log via return)
        md = data.get("metadata")
        if not NO_SESSID and isinstance(md, dict) and isinstance(md.get("user_id"), str):
            try:
                uid = json.loads(md["user_id"])
                if isinstance(uid, dict) and "session_id" in uid:
                    uid["session_id"] = "stable"
                    md["user_id"] = json.dumps(uid)
                    changed = True
            except Exception:
                pass
        if not NO_STRIP:
            for msg in data.get("messages", []):
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                new_content = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        txt = block.get("text", "")
                        if any(txt.startswith(p) for p in STRIP_PREFIXES):
                            changed = True
                            continue
                    new_content.append(block)
                msg["content"] = new_content
        msgs = data.get("messages", [])
        if not NO_MARKER and msgs:
            content = msgs[0].get("content")
            if isinstance(content, list) and len(content) >= 2:
                last = content[-1]
                prev = content[-2]
                if isinstance(last, dict) and isinstance(prev, dict):
                    cc = last.get("cache_control")
                    if cc and "cache_control" not in prev:
                        prev["cache_control"] = cc
                        last.pop("cache_control", None)
                        changed = True
        if changed:
            return json.dumps(data, separators=(",", ":")).encode(), routed, route_decision
    except Exception:
        pass
    return body, routed, route_decision


def _extract_request_stats(body: bytes) -> dict:
    try:
        data = json.loads(body)
    except Exception:
        return {}
    msgs = data.get("messages", [])
    sys = data.get("system", "")
    sys_len = len(sys) if isinstance(sys, str) else sum(len(b.get("text", "")) for b in sys if isinstance(b, dict))
    tools = data.get("tools", [])
    tools_len = sum(len(json.dumps(t)) for t in tools)
    return {
        "model": data.get("model"),
        "n_messages": len(msgs),
        "n_tools": len(tools),
        "system_chars": sys_len,
        "tools_chars": tools_len,
        "body_chars": len(body),
        "has_cache_control": b"cache_control" in body,
    }


@app.get("/health")
async def health():
    """Local liveness endpoint for Claude and operator checks.

    This must never proxy upstream. If /health falls through to the catch-all
    proxy route, local health checks can hang on Anthropic and make the whole
    Claude path appear dead before any real request is attempted.
    """
    return {
        "ok": True,
        "service": "token_saver_proxy",
        "upstream": UPSTREAM,
    }


@app.get("/")
async def root():
    """Small local root endpoint for manual smoke tests."""
    return {
        "ok": True,
        "service": "token_saver_proxy",
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy(path: str, request: Request):
    t0 = time.time()
    body = await request.body()
    orig_body_chars = len(body)
    routed_model = None
    route_decision = None
    if path.startswith("v1/messages") and not os.environ.get("TSPROXY_PASSTHROUGH"):
        body, routed_model, route_decision = _rewrite_body(body)
    req_stats = _extract_request_stats(body) if path.startswith("v1/messages") else {}

    # Multi-model router: replace intermediate turns with local LLMs
    if path.startswith("v1/messages") and not os.environ.get("TSPROXY_NO_MMR"):
        try:
            import importlib.util, sys as _sys
            _mmr_data = json.loads(body)
            _is_stream = _mmr_data.get("stream", False)
            _mmr_resp = None
            _mmr_name = None
            for _mmr_file in MMR_MODULES:
                _mmr_path = os.path.join(os.path.dirname(__file__), _mmr_file)
                if _mmr_path not in _sys.modules:
                    _spec = importlib.util.spec_from_file_location(f"tsproxy_{_mmr_file[:-3]}", _mmr_path)
                    _mmr_mod = importlib.util.module_from_spec(_spec)
                    _spec.loader.exec_module(_mmr_mod)
                    _sys.modules[_mmr_path] = _mmr_mod
                else:
                    _mmr_mod = _sys.modules[_mmr_path]
                _mmr_resp = _mmr_mod.attempt(_mmr_data, streaming=_is_stream)
                if _mmr_resp:
                    _mmr_name = _mmr_file[:-3]
                    break
            if _mmr_resp:
                _ct = "text/event-stream" if _is_stream else "application/json"
                _log({"event": "mmr_used", "router": _mmr_name, "req": req_stats})
                return Response(content=_mmr_resp, status_code=200,
                                headers={"content-type": _ct, "x-mmr": _mmr_name or "1"})
        except Exception as _mmre:
            _log({"event": "mmr_error", "error": str(_mmre)[:100]})
            pass
    req_stats["orig_body_chars"] = orig_body_chars
    req_stats["routed_model"] = routed_model
    req_stats["route_decision"] = route_decision
    routed_haiku = routed_model == HAIKU_MODEL
    # DEBUG: dump full request body for analysis
    if path.startswith("v1/messages") and os.environ.get("TSPROXY_DUMP"):
        import hashlib
        h = hashlib.md5(body).hexdigest()[:8]
        with open(f"/tmp/tsproxy_body_{int(t0)}_{h}.json", "wb") as f:
            f.write(body)

    url = "/" + path
    if request.url.query:
        url += "?" + request.url.query

    headers = _clean_headers(request.headers)
    headers.append(("accept-encoding", "identity"))  # force plaintext so we can parse SSE
    if routed_haiku:
        # Haiku 4.5 doesn't support context-1m long-context beta. Strip it.
        new_headers = []
        for k, v in headers:
            if k.lower() == "anthropic-beta":
                parts = [p.strip() for p in v.split(",") if "context-1m" not in p]
                if parts:
                    new_headers.append((k, ", ".join(parts)))
            else:
                new_headers.append((k, v))
        headers = new_headers

    try:
        upstream_req = client.build_request(request.method, url, content=body, headers=headers)
        upstream = await client.send(upstream_req, stream=True)
    except Exception as e:
        _log({"event": "upstream_error", "path": path, "error": str(e)})
        return Response(content=json.dumps({"error": str(e)}).encode(), status_code=502)

    resp_headers = [(k, v) for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP]
    is_sse = "text/event-stream" in upstream.headers.get("content-type", "")

    usage_capture = {"in": 0, "cache_read": 0, "cache_create": 0, "out": 0}

    diag = {"chunks": 0, "bytes": 0, "enc": upstream.headers.get("content-encoding", "none"), "ct": upstream.headers.get("content-type", "")}
    async def stream_iter():
        buf = b""
        try:
            async for chunk in upstream.aiter_raw():
                diag["chunks"] += 1
                diag["bytes"] += len(chunk)
                if is_sse:
                    buf += chunk
                    while b"\n\n" in buf:
                        evt, buf = buf.split(b"\n\n", 1)
                        if b"usage" in evt:
                            for line in evt.split(b"\n"):
                                if line.startswith(b"data: "):
                                    try:
                                        d = json.loads(line[6:])
                                        u = d.get("usage") or (d.get("message") or {}).get("usage") or {}
                                        if u:
                                            for k_src, k_dst in [("input_tokens","in"),("cache_read_input_tokens","cache_read"),("cache_creation_input_tokens","cache_create"),("output_tokens","out")]:
                                                v = u.get(k_src)
                                                if v:
                                                    usage_capture[k_dst] = v
                                    except Exception:
                                        pass
                yield chunk
        finally:
            await upstream.aclose()
            _log({
                "event": "turn",
                "path": path,
                "status": upstream.status_code,
                "latency_ms": int((time.time() - t0) * 1000),
                "req": req_stats,
                "usage": usage_capture,
                "diag": diag,
                "is_sse": is_sse,
            })

    if is_sse:
        return StreamingResponse(stream_iter(), status_code=upstream.status_code, headers=dict(resp_headers), media_type="text/event-stream")

    # Non-SSE: read fully, log, return
    content = b""
    async for chunk in upstream.aiter_raw():
        content += chunk
    await upstream.aclose()
    _log({
        "event": "turn",
        "path": path,
        "status": upstream.status_code,
        "latency_ms": int((time.time() - t0) * 1000),
        "req": req_stats,
        "resp_chars": len(content),
    })
    return Response(content=content, status_code=upstream.status_code, headers=dict(resp_headers))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787, log_level="warning")
