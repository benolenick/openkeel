#!/usr/bin/env python3
"""
Nova Incident Response — Scenario Engine v4
Rich agent management: agents get stuck, lost, timeout, collide, crash.
Nova delegates everything. Human guides the agents through her.
"""

import asyncio
import time
import random
from nova_decisions import DecisionEngine

# ── Systems ─────────────────────────────────────────────────────────
SYSTEMS = {
    "web_server":    {"name": "Web Server",    "short": "WEB"},
    "app_server":    {"name": "App Server",    "short": "APP"},
    "database":      {"name": "Database",      "short": "DB"},
    "cache":         {"name": "Cache",         "short": "CACHE"},
    "load_balancer": {"name": "Load Balancer", "short": "LB"},
    "cdn":           {"name": "CDN",           "short": "CDN"},
    "payment_api":   {"name": "Payment API",   "short": "PAY"},
    "auth_service":  {"name": "Auth Service",  "short": "AUTH"},
    "message_queue": {"name": "Msg Queue",     "short": "MQ"},
    "dns":           {"name": "DNS",           "short": "DNS"},
}

INITIAL_METRICS = {
    "web_server": {"CPU": "12%", "MEM": "45%", "HTTP": "99.8%", "RPS": 342},
    "app_server": {"Threads": "24/24", "Queue": 3, "Resp": "89ms"},
    "database": {"Conn": "23/100", "Lag": "12ms", "SlowQ": 0},
    "cache": {"Hit%": "94.2", "Mem": "512MB", "Evict": 0},
    "load_balancer": {"Up": "4/4", "RPS": 680},
    "cdn": {"Hit%": "97.1", "BW": "45MB/s"},
    "payment_api": {"Lat": "120ms", "OK%": "99.9", "Timeout": 0},
    "auth_service": {"Login/m": 48, "Fail%": "0.2", "Sessions": 1240},
    "message_queue": {"Pending": 12, "Lag": 0, "Dead": 0},
    "dns": {"Resolve": "8ms", "Status": "OK"},
}

CRISIS_METRICS = {
    "database": {"Conn": "100/100", "Lag": "8.4s", "SlowQ": 1},
    "app_server": {"Threads": "24/24", "Queue": 89, "Resp": "3200ms"},
    "web_server": {"CPU": "34%", "MEM": "97%", "HTTP": "38.4%", "RPS": 342},
    "cache": {"Hit%": "31.0", "Mem": "510MB", "Evict": 84},
    "message_queue": {"Pending": 4200, "Lag": 340, "Dead": 23},
    "payment_api": {"Lat": "4800ms", "OK%": "72.1", "Timeout": 84},
    "load_balancer": {"Up": "1/4", "RPS": 170},
}

CRISIS_CASCADE = [
    (0,  "database",      "critical"),
    (2,  "app_server",    "warning"),
    (4,  "web_server",    "warning"),
    (6,  "cache",         "warning"),
    (8,  "message_queue", "warning"),
    (9,  "payment_api",   "degraded"),
    (11, "load_balancer", "degraded"),
]

RECOVERY_ORDER = [
    (0,  "database",      "recovering"),
    (2,  "database",      "healthy"),
    (3,  "app_server",    "recovering"),
    (4,  "cache",         "recovering"),
    (5,  "message_queue", "recovering"),
    (6,  "web_server",    "recovering"),
    (7,  "payment_api",   "healthy"),
    (8,  "load_balancer", "healthy"),
    (9,  "app_server",    "healthy"),
    (10, "web_server",    "healthy"),
    (11, "cache",         "healthy"),
    (12, "message_queue", "healthy"),
]

# ── Workers ─────────────────────────────────────────────────────────
WORKERS = {
    "route": {"name": "Agent 1", "color": "#4488FF"},
    "scout": {"name": "Agent 2", "color": "#CC66FF"},
    "ping":  {"name": "Agent 3",  "color": "#FFD700"},
    "vault": {"name": "Agent 4", "color": "#00FF88"},
}

# ── Scenario Script ─────────────────────────────────────────────────
# Each event is processed in order. Workers run in parallel groups.
# Types: dispatch, terminal, clear, finding, stuck, timeout_check,
#        lost, collision, needs_web, report, nova_speak, pause, decision

SCRIPT = [
    # ── Phase: Deploy agents ──
    {"type": "nova_speak", "text": "I'm sending my agents out to investigate in parallel. Agent 1 takes network, Agent 2 takes external services services, Agent 3 takes servers, Agent 4 takes the data layer. Watch the terminals.", "emotion": "concerned"},
    {"type": "pause", "duration": 3},

    # ── ROUTE: Fast, clean sweep ──
    {"type": "parallel_start", "groups": ["route_sweep", "scout_sweep", "ping_sweep", "vault_sweep"]},
]

ROUTE_SWEEP = [
    # ── DNS ──
    {"type": "dispatch", "worker": "route", "system": "dns"},
    {"type": "terminal", "worker": "route", "system": "dns", "lines": [
        ("nova@dns:~$ dig meridian-commerce.com +short", 0.78),
        ("Resolving...", 1.3),
        ("104.21.46.182", 0.78),
        ("nova@dns:~$ dig @8.8.8.8 meridian-commerce.com", 0.78),
        (";; ANSWER SECTION:", 1.35),
        ("meridian-commerce.com. 300 IN A 104.21.46.182", 0.78),
        (";; Query time: 12 msec", 0.78),
        ("nova@dns:~$ dig @1.1.1.1 meridian-commerce.com +short", 0.78),
        ("104.21.46.182", 1.35),
        ("nova@dns:~$ dig @208.67.222.222 meridian-commerce.com +short", 0.78),
        ("104.21.46.182", 1.35),
        ("nova@dns:~$ # Testing CNAME chain...", 0.78),
        ("nova@dns:~$ dig meridian-commerce.com CNAME", 0.78),
        ("No CNAME, direct A record", 0.78),
        ("nova@dns:~$ dig meridian-commerce.com MX +short", 0.78),
        ("10 mail.meridian-commerce.com.", 0.78),
        ("nova@dns:~$ # Checking TTL consistency...", 0.78),
        ("TTL: 300s across all resolvers", 1.35),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("DNS: 4 resolvers checked, all consistent", 0.52),
        ("Resolution time: 8-12ms, no anomalies", 0.52),
        ("[OK] DNS healthy", 0.78),
    ]},
    {"type": "monitor", "worker": "route", "system": "dns", "duration": 18, "interval": 0.4,
     "templates": [
        "  ;; reply from 8.8.8.8: A 104.21.46.182 ttl={rand3}",
        "  ;; query time: {rand2}ms server: {randip}",
        "  dig +trace meridian-commerce.com [{ts}]",
        "  NOERROR  flags: qr rd ra;  ANSWER: 1",
        "  ;; WHEN: {ts}  MSG SIZE rcvd: {rand3}",
        "  checking resolver {randip}... OK",
        "  propagation check {rand2}/50 resolvers consistent",
    ]},
    {"type": "clear", "worker": "route", "system": "dns", "text": "DNS resolving normally (4 resolvers checked)"},
    {"type": "return", "worker": "route"},

    # ── CDN ──
    {"type": "dispatch", "worker": "route", "system": "cdn"},
    {"type": "terminal", "worker": "route", "system": "cdn", "lines": [
        ("nova@cdn:~$ curl -sI cdn.meridian-commerce.com/app.js", 0.78),
        ("HTTP/2 200", 0.52),
        ("x-cache: HIT", 0.52),
        ("age: 1204", 0.52),
        ("content-length: 284621", 0.52),
        ("cf-cache-status: HIT", 0.52),
        ("cf-ray: 8a2f3b...", 0.52),
        ("nova@cdn:~$ curl -w 'total:%{time_total}s' -so /dev/null cdn.meridian-commerce.com/hero.webp", 0.78),
        ("total:0.038s", 1.35),
        ("nova@cdn:~$ curl -w 'total:%{time_total}s' -so /dev/null cdn.meridian-commerce.com/bundle.js", 0.78),
        ("total:0.041s", 1.35),
        ("nova@cdn:~$ curl -w 'total:%{time_total}s' -so /dev/null cdn.meridian-commerce.com/fonts.woff2", 0.78),
        ("total:0.029s", 0.78),
        ("nova@cdn:~$ # Testing edge locations...", 0.78),
        ("nova@cdn:~$ for i in {1..5}; do curl -sI cdn.meridian-commerce.com | grep cf-ray; done", 0.78),
        ("cf-ray: 8a2f3b-YYZ", 0.52),
        ("cf-ray: 8a2f3c-YYZ", 0.52),
        ("cf-ray: 8a2f3d-YYZ", 0.52),
        ("cf-ray: 8a2f3e-YYZ", 0.52),
        ("cf-ray: 8a2f3f-YYZ", 0.52),
        ("All hitting YYZ edge. Cache ratio: 97.1%", 0.78),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("CDN: all assets cached, <50ms delivery", 0.52),
        ("Edge: YYZ, cache ratio 97.1%", 0.52),
        ("[OK] CDN nominal", 0.78),
    ]},
    {"type": "monitor", "worker": "route", "system": "cdn", "duration": 18, "interval": 0.4,
     "templates": [
        "  GET /assets/img_{rand4}.webp  HIT  {rand2}ms  {rand3}KB",
        "  GET /js/chunk_{rand3}.js  HIT  {rand2}ms  {rand2}KB",
        "  GET /css/main.css  HIT  {rand2}ms  age:{rand4}s",
        "  cf-cache-status: HIT  cf-ray: 8a{rand4}-YYZ",
        "  bandwidth: {randf}MB/s  requests: {rand3}/s",
        "  edge-pop: YYZ  cache-ratio: 97.{rand2}%",
        "  GET /fonts/inter-{rand3}.woff2  HIT  {rand2}ms",
    ]},
    {"type": "clear", "worker": "route", "system": "cdn", "text": "CDN serving normally — 97% cache hit"},
    {"type": "return", "worker": "route"},

    # ── Load Balancer ──
    {"type": "dispatch", "worker": "route", "system": "load_balancer"},
    {"type": "terminal", "worker": "route", "system": "load_balancer", "lines": [
        ("nova@lb:~$ netstat -an | grep :443 | wc -l", 0.78),
        ("247", 1.35),
        ("nova@lb:~$ ss -s", 0.78),
        ("TCP: 247 (estab 231, closed 8, orphaned 4, timewait 4)", 0.78),
        ("nova@lb:~$ # Normal range 100-400, checking rate...", 0.78),
        ("nova@lb:~$ sar -n DEV 1 5 | grep eth0 | tail -5", 0.78),
        ("14:27:01 eth0  rxpck/s:1204  txpck/s:1198  rxkB/s:842", 1.56),
        ("14:27:02 eth0  rxpck/s:1189  txpck/s:1201  rxkB/s:831", 1.56),
        ("14:27:03 eth0  rxpck/s:1211  txpck/s:1195  rxkB/s:847", 1.56),
        ("14:27:04 eth0  rxpck/s:1198  txpck/s:1204  rxkB/s:838", 1.56),
        ("14:27:05 eth0  rxpck/s:1207  txpck/s:1199  rxkB/s:844", 1.56),
        ("nova@lb:~$ # Traffic steady ~1200 pps, no spike pattern", 0.78),
        ("nova@lb:~$ iptables -L -n --line-numbers | grep DROP | wc -l", 0.78),
        ("0 dropped packets", 0.78),
        ("nova@lb:~$ cat /var/log/nginx/rate_limit.log | tail -3", 0.78),
        ("(empty — no rate limiting triggered)", 0.78),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("LB: 247 connections, traffic steady, no DDoS", 0.52),
        ("No drops, no rate limiting triggered", 0.52),
        ("[OK] Traffic normal", 0.78),
    ]},
    {"type": "monitor", "worker": "route", "system": "load_balancer", "duration": 24, "interval": 0.4,
     "templates": [
        "  {ts} eth0 rx:{rand4}pkt tx:{rand4}pkt drop:0",
        "  {ts} conn: {rand3} active  new: {rand2}/s",
        "  {ts} upstream node-1: 503  node-2: 503  node-3: timeout  node-4: 200",
        "  {ts} rate-limit: 0 triggered  blacklist: 0 matched",
        "  {ts} SYN flood check: 0 suspicious  0 blocked",
        "  {ts} geo: CA:{rand2}% US:{rand2}% EU:{rand2}%",
        "  {ts} bandwidth in:{randf}MB/s out:{randf}MB/s",
        "  {ts} latency p50:{rand2}ms p99:{rand3}ms",
    ]},
    {"type": "clear", "worker": "route", "system": "load_balancer", "text": "Traffic normal — not a DDoS (5s sample)"},
    {"type": "return", "worker": "route"},

    {"type": "report", "worker": "route", "text": "Network layer clean. DNS, CDN, traffic all normal. Problem is internal."},
]

SCOUT_SWEEP = [
    # ── Payment API ──
    {"type": "dispatch", "worker": "scout", "system": "payment_api"},
    {"type": "terminal", "worker": "scout", "system": "payment_api", "lines": [
        ("nova@ext:~$ curl -s api.stripe.com/v1/health", 0.78),
        ("Connecting to 104.18.7.52:443...", 1.35),
        ("SSL handshake OK (TLS 1.3)", 0.78),
        ('{"status":"ok","version":"2026-03","latency_ms":45}', 0.78),
        ("nova@ext:~$ curl -w '%{time_connect} %{time_total}' -so /dev/null api.stripe.com/v1/charges", 0.78),
        ("connect:0.012s  total:0.048s", 0.78),
        ("nova@ext:~$ # Stripe responds in 48ms", 0.52),
        ("nova@ext:~$ # Our payment timeouts show 4800ms", 0.52),
        ("nova@ext:~$ # Bottleneck is between app and Stripe", 0.78),
        ("nova@ext:~$ traceroute -m 5 api.stripe.com", 0.78),
        (" 1  gateway (10.0.0.1)  1.2ms", 0.52),
        (" 2  isp-gw (72.14.204.1)  3.4ms", 0.52),
        (" 3  104.18.7.52  8.1ms", 0.78),
        ("nova@ext:~$ # Route is clean, 3 hops, 8ms", 0.78),
        ("nova@ext:~$ curl -s localhost:8080/metrics | grep stripe", 0.78),
        ("stripe_timeout_total: 84 (last 10min)", 0.78),
        ("stripe_avg_latency: 4800ms", 0.78),
        ("nova@ext:~$ # 84 timeouts, but Stripe is fast", 0.52),
        ("nova@ext:~$ # Our app is too slow to complete the call", 0.78),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("Stripe: healthy (48ms). Timeouts are on our side.", 0.52),
        ("App server too slow to complete payment calls.", 0.52),
        ("[OK] Stripe healthy — problem is internal", 0.78),
    ]},
    {"type": "monitor", "worker": "scout", "system": "payment_api", "duration": 21, "interval": 0.4,
     "templates": [
        "  {ts} POST /v1/charges -> Stripe 200 {rand2}ms",
        "  {ts} POST /v1/charges -> Stripe 200 {rand2}ms",
        "  {ts} POST /v1/charges -> timeout (our side) {rand4}ms",
        "  {ts} webhook received: charge.succeeded {rand4}",
        "  {ts} GET /v1/balance -> 200 {rand2}ms",
        "  {ts} outbound proxy latency: {rand2}ms",
        "  {ts} TLS handshake: {rand2}ms  cert: valid",
        "  {ts} traceroute hop {rand2}: {randf}ms",
    ]},
    {"type": "clear", "worker": "scout", "system": "payment_api", "text": "Stripe healthy (48ms) — our app is the bottleneck"},
    {"type": "return", "worker": "scout"},

    # ── Auth Service ──
    {"type": "dispatch", "worker": "scout", "system": "auth_service"},
    {"type": "terminal", "worker": "scout", "system": "auth_service", "lines": [
        ("nova@auth:~$ curl -s localhost:9090/health", 0.78),
        ('{"status":"healthy","uptime":"47d 3h","sessions":1240}', 0.78),
        ("nova@auth:~$ curl -s localhost:9090/metrics", 0.78),
        ("auth_requests_total: 48294", 0.52),
        ("auth_failures_total: 12 (24h)", 0.52),
        ("auth_failure_rate: 0.02%", 0.52),
        ("active_sessions: 1240", 0.52),
        ("token_gen_avg_ms: 3", 0.52),
        ("nova@auth:~$ tail -5 /var/log/auth/access.log", 0.78),
        ("14:22:58 LOGIN user=jsmith src=10.0.0.44 OK 3ms", 0.52),
        ("14:23:01 LOGIN user=mchen src=10.0.0.51 OK 2ms", 0.52),
        ("14:23:04 LOGIN user=agarcia src=10.0.0.37 OK 4ms", 0.52),
        ("14:23:08 LOGIN user=tkumar src=10.0.0.62 OK 2ms", 0.52),
        ("14:23:11 REFRESH user=jsmith token=ok 1ms", 0.52),
        ("nova@auth:~$ # All logins fast, no anomalies", 0.78),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("Auth: healthy, 0.02% fail rate, 3ms avg", 0.52),
        ("[OK] Auth service nominal", 0.78),
    ]},
    {"type": "monitor", "worker": "scout", "system": "auth_service", "duration": 15, "interval": 0.4,
     "templates": [
        "  {ts} LOGIN user={rand3}@corp OK {rand2}ms",
        "  {ts} TOKEN refresh uid:{rand4} OK 2ms",
        "  {ts} SESSION check sid:{rand4} valid",
        "  {ts} LDAP bind: {rand2}ms  pool: 8/20",
        "  {ts} JWT verify: RS256 {rand2}ms",
        "  {ts} rate-limit check: {randip} -> pass",
        "  {ts} MFA verify uid:{rand4} -> success",
    ]},
    {"type": "clear", "worker": "scout", "system": "auth_service", "text": "Auth service healthy — 0.02% fail rate"},
    {"type": "return", "worker": "scout"},

    # ── STUCK: AWS status timeout ──
    {"type": "dispatch", "worker": "scout", "system": "auth_service"},
    {"type": "terminal", "worker": "scout", "system": "auth_service", "lines": [
        ("nova@ext:~$ # Checking AWS status...", 0.78),
        ("nova@ext:~$ curl -s https://status.aws.amazon.com/data.json", 0.78),
        ("Connecting to status.aws.amazon.com...", 1.3),
        ("...", 1.95),
        ("...", 1.95),
        ("...", 1.95),
        ("curl: (28) Connection timed out after 10001ms", 0.78),
        ("nova@ext:~$ # Retry with shorter timeout", 0.78),
        ("nova@ext:~$ curl -s --connect-timeout 5 status.aws.amazon.com/data.json", 0.78),
        ("...", 1.95),
        ("...", 1.95),
        ("curl: (28) Connection timed out after 5001ms", 0.78),
        ("nova@ext:~$ # Something blocking outbound to AWS", 0.78),
        ("[STUCK] Cannot reach AWS status page", 0.78),
    ]},
    {"type": "stuck", "worker": "scout", "system": "auth_service",
     "scenario": "timeout",
     "nova_text": "Scout's been trying to reach AWS for 20 seconds — it's stuck. Redirect it to check Downdetector instead, or just skip this and move on?",
     "options": [
        {"id": "redirect", "label": "Check Downdetector", "keywords": ["downdetector", "redirect", "different", "another", "alternative", "try"],
         "terminal": [
            ("nova@ext:~$ curl -s downdetector.com/status/aws/", 0.78),
            ("Parsing response...", 1.35),
            ("No outages reported in last 2h", 0.78),
            ("nova@ext:~$ curl -s isitdown.site/api/aws.amazon.com", 0.78),
            ('{"isDown":false,"lastChecked":"14:27"}', 0.78),
            ("[OK] AWS confirmed clean via 2 sources", 0.78),
         ], "nova_reply": "Good thinking — routed around the block. AWS is clean, confirmed from two sources.", "score": 15},
        {"id": "skip", "label": "Skip, Move On", "keywords": ["skip", "pull", "back", "forget", "move on", "fine", "assume"],
         "terminal": [
            ("[OK] Skipping AWS check — assuming healthy", 0.78),
         ], "nova_reply": "Pulling it back. Everything else external passed. Safe to assume AWS is fine.", "score": 8},
     ], "timeout": 60, "timeout_choice": "skip"},
    {"type": "return", "worker": "scout"},

    {"type": "report", "worker": "scout", "text": "External deps all clear. Stripe healthy, auth nominal, AWS clean. Entirely internal problem."},
]

PING_SWEEP = [
    # ── Web Server ──
    {"type": "dispatch", "worker": "ping", "system": "web_server"},
    {"type": "terminal", "worker": "ping", "system": "web_server", "lines": [
        ("nova@web:~$ curl -s localhost/health", 0.78),
        ('{"status":"degraded","code":503,"uptime":"47d"}', 0.78),
        ("nova@web:~$ top -bn1 | head -6", 0.78),
        ("top - 14:27:03 up 47 days", 0.52),
        ("load average: 18.42, 12.31, 8.67", 0.52),
        ("Tasks: 284 total, 3 running", 0.52),
        ("%Cpu: 34.2us, 8.1sy, 57.7id", 0.52),
        ("MiB Mem: 16384 total, 476 free, 15907 used", 0.52),
        ("MiB Swap: 8192 total, 1474 free, 6717 used", 0.78),
        ("nova@web:~$ ps aux --sort=-%mem | head -5", 0.78),
        ("PID   %MEM  COMMAND", 0.52),
        ("4821  96.8  java -Xmx12g -jar app.jar", 0.52),
        ("1204   0.3  nginx: worker", 0.52),
        ("1205   0.3  nginx: worker", 0.52),
        ("nova@web:~$ # Java process eating 96.8% memory", 0.78),
        ("nova@web:~$ curl -s localhost/health?verbose=true", 0.78),
        ('{"http_200_pct":38.4,"http_503_pct":61.6}', 0.78),
        ("nova@web:~$ # 61.6% of requests returning 503", 0.78),
    ]},
    # STUCK: permission denied
    {"type": "terminal", "worker": "ping", "system": "web_server", "lines": [
        ("nova@web:~$ cat /var/log/app/error.log", 0.78),
        ("cat: Permission denied", 0.78),
        ("nova@web:~$ ls -la /var/log/app/error.log", 0.78),
        ("-rw------- 1 root root 48M error.log", 0.78),
        ("nova@web:~$ # File owned by root, agent is nova", 0.78),
        ("[STUCK] No read access to error logs", 0.78),
    ]},
    {"type": "monitor", "worker": "ping", "system": "web_server", "duration": 24, "interval": 0.35,
     "templates": [
        "  {ts} GET /api/products -> 503 {rand4}ms",
        "  {ts} GET /api/cart -> 503 {rand4}ms",
        "  {ts} GET / -> 200 {rand3}ms (static cached)",
        "  {ts} GET /api/orders -> 503 {rand4}ms",
        "  {ts} POST /api/checkout -> 503 timeout",
        "  {ts} mem: {rand2}% cpu: {rand2}% swap: {rand2}%",
        "  {ts} java.lang.OutOfMemoryError: Java heap space",
        "  {ts} GC pause: {rand3}ms (full GC, stop-the-world)",
        "  {ts} GET /health -> 503 degraded",
        "  {ts} nginx worker pid:{rand4} connections: {rand3}",
    ]},
    {"type": "stuck", "worker": "ping", "system": "web_server",
     "scenario": "blocked",
     "nova_text": "Agent 3 can't read the error logs. No permissions. Try the app's debug endpoint, or grant it elevated access?",
     "options": [
        {"id": "endpoint", "label": "Use Debug Endpoint", "keywords": ["debug", "endpoint", "api", "app", "http"],
         "terminal": [
            ("nova@web:~$ curl -s localhost:8080/debug/logs?last=20", 0.78),
            ("Fetching application logs...", 1.35),
            ("14:23:01 ERROR java.lang.OutOfMemoryError: Java heap", 0.52),
            ("14:23:01 ERROR java.lang.OutOfMemoryError: Java heap", 0.39),
            ("14:23:01 ERROR java.lang.OutOfMemoryError: Java heap", 0.39),
            ("14:23:02 ERROR java.lang.OutOfMemoryError: Java heap", 0.39),
            ("14:23:02 ERROR java.lang.OutOfMemoryError: Java heap", 0.39),
            ("14:23:03 ERROR GC overhead limit exceeded", 0.39),
            ("14:23:03 ERROR java.lang.OutOfMemoryError: Java heap", 0.39),
            ("nova@web:~$ curl -s localhost:8080/debug/logs?last=20 | grep -c OutOfMemory", 0.78),
            ("847 occurrences since 14:23:01", 0.78),
            ("--- SUMMARY FOR NOVA ---", 0.78),
            ("Web: OOM errors x847 since 14:23, MEM 97%", 0.52),
            ("Java heap exhausted. Something consuming all memory.", 0.52),
         ], "nova_reply": "Smart move using the debug endpoint. 847 out-of-memory errors since 14:23. Something is eating all the RAM.", "score": 15},
        {"id": "sudo", "label": "Grant Sudo", "keywords": ["sudo", "root", "elevat", "grant", "admin"],
         "terminal": [
            ("[WARN] Granting elevated access to agent...", 1.35),
            ("nova@web:~$ sudo tail -20 /var/log/app/error.log", 0.78),
            ("14:23:01 OutOfMemoryError: Java heap", 0.52),
            ("14:23:01 OutOfMemoryError: Java heap", 0.39),
            ("14:23:02 OutOfMemoryError: Java heap", 0.39),
            ("14:23:02 OutOfMemoryError: Java heap", 0.39),
            ("--- SUMMARY FOR NOVA ---", 0.78),
            ("Web: OOM errors confirmed. Memory exhausted.", 0.52),
         ], "nova_reply": "Got in with elevated access. OOM errors confirmed since 14:23.", "score": 5},
     ], "timeout": 75, "timeout_choice": "endpoint"},
    {"type": "finding", "worker": "ping", "system": "web_server",
     "severity": "critical", "text": "Web server OOM — 847 errors since 14:23"},
    {"type": "return", "worker": "ping"},

    # ── App Server ──
    {"type": "dispatch", "worker": "ping", "system": "app_server"},
    {"type": "terminal", "worker": "ping", "system": "app_server", "lines": [
        ("nova@app:~$ curl -s localhost:8080/metrics", 0.78),
        ("thread_pool_active: 24", 0.52),
        ("thread_pool_max: 24", 0.52),
        ("thread_pool_queued: 89", 0.52),
        ("avg_response_time_ms: 3204", 0.52),
        ("p99_response_time_ms: 12800", 0.52),
        ("gc_pause_ms: 450", 0.52),
        ("heap_used_mb: 11904", 0.52),
        ("heap_max_mb: 12288", 0.52),
        ("nova@app:~$ curl -s localhost:8080/debug/threads | head -12", 0.78),
        ("Thread-01: BLOCKED waiting for db conn (38s)", 0.52),
        ("Thread-02: BLOCKED waiting for db conn (35s)", 0.39),
        ("Thread-03: BLOCKED waiting for db conn (34s)", 0.39),
        ("Thread-04: BLOCKED waiting for db conn (33s)", 0.39),
        ("Thread-05: BLOCKED waiting for db conn (31s)", 0.39),
        ("Thread-06: BLOCKED waiting for db conn (30s)", 0.39),
        ("Thread-07: BLOCKED waiting for db conn (28s)", 0.39),
        ("Thread-08: BLOCKED waiting for db conn (27s)", 0.39),
        ("...(16 more, all BLOCKED on db)", 0.52),
        ("nova@app:~$ # All 24 threads waiting for DB connections", 0.78),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("App: 24/24 threads BLOCKED on DB connection pool", 0.52),
        ("Queue: 89 requests waiting. Heap: 97% full.", 0.52),
    ]},
    {"type": "monitor", "worker": "ping", "system": "app_server", "duration": 18, "interval": 0.35,
     "templates": [
        "  Thread-{rand2}: BLOCKED on db.getConnection() ({rand2}s)",
        "  Thread-{rand2}: BLOCKED on db.getConnection() ({rand2}s)",
        "  Thread-{rand2}: BLOCKED on db.getConnection() ({rand2}s)",
        "  queue +1 (depth:{rand3})  waiting: {rand2}s",
        "  queue +1 (depth:{rand3})  waiting: {rand2}s",
        "  {ts} request timeout: POST /api/checkout uid:{rand4}",
        "  {ts} request timeout: GET /api/products?cat={rand2}",
        "  {ts} heap: {rand4}MB/{rand4}MB  gc-pauses: {rand2}",
    ]},
    {"type": "finding", "worker": "ping", "system": "app_server",
     "severity": "warning", "text": "All 24 threads blocked on DB connections"},
    {"type": "return", "worker": "ping"},

    # ── LOST: Ping wanders to auth ──
    {"type": "dispatch", "worker": "ping", "system": "auth_service"},
    {"type": "terminal", "worker": "ping", "system": "auth_service", "lines": [
        ("nova@auth:~$ grep 'failed_login' /var/log/auth.log | wc -l", 0.78),
        ("3", 0.78),
        ("nova@auth:~$ grep 'brute_force' /var/log/auth.log", 0.78),
        ("(no results)", 1.35),
        ("nova@auth:~$ cat /etc/auth/token_rotation.conf", 0.78),
        ("rotation_interval: 3600", 0.52),
        ("last_rotation: 14:01:23", 0.52),
        ("nova@auth:~$ openssl x509 -in /etc/auth/jwt.pem -noout -dates", 0.78),
        ("notAfter=Jun 15 2026", 0.78),
        ("nova@auth:~$ # Checking if token expiry related...", 0.78),
        ("nova@auth:~$ grep -c 'token_expired' /var/log/auth.log", 0.78),
        ("0", 0.78),
        ("nova@auth:~$ # Investigating session anomalies...", 1.35),
    ]},
    {"type": "stuck", "worker": "ping", "system": "auth_service",
     "scenario": "lost",
     "nova_text": "Agent 3 is going off-script. It's digging through auth logs looking for token issues. That's not our problem — auth was already cleared. Pull it back and refocus, or let it finish?",
     "options": [
        {"id": "refocus", "label": "Pull Back, Refocus", "keywords": ["pull", "back", "refocus", "stop", "redirect", "focus", "server"],
         "terminal": [
            ("[REDIRECT] Pulling agent back to core investigation", 0.78),
         ], "nova_reply": "Good call. No point chasing ghosts. Brought it back on task.", "score": 15},
        {"id": "let_finish", "label": "Let It Finish", "keywords": ["finish", "let", "continue", "keep", "explore"],
         "terminal": [
            ("nova@auth:~$ ...scanning more logs...", 1.3),
            ("nova@auth:~$ ...checking LDAP integration...", 1.3),
            ("nova@auth:~$ ...testing SSO endpoints...", 1.3),
            ("nova@auth:~$ ...nothing relevant found...", 1.3),
            ("[OK] Auth clean. (Wasted 15 seconds)", 0.78),
         ], "nova_reply": "Auth is clean. But we burned time checking something Scout already cleared.", "score": 0},
     ], "timeout": 45, "timeout_choice": "refocus"},
    {"type": "return", "worker": "ping"},

    {"type": "report", "worker": "ping", "text": "Servers choking. Web: OOM since 14:23. App: all threads blocked on DB. Something upstream is consuming all database connections."},
]

VAULT_SWEEP = [
    # ── Database (first pass) ──
    {"type": "dispatch", "worker": "vault", "system": "database"},
    {"type": "terminal", "worker": "vault", "system": "database", "lines": [
        ("nova@db:~$ psql -c 'SELECT count(*) FROM pg_stat_activity'", 0.78),
        ("Connecting to postgresql://localhost:5432/meridian...", 1.35),
        ("  count", 0.52),
        ("  -----", 0.39),
        ("    100", 0.52),
        ("(1 row)", 0.52),
        ("nova@db:~$ psql -c 'SHOW max_connections'", 0.78),
        ("  100", 0.78),
        ("nova@db:~$ # 100/100 — COMPLETELY FULL", 0.78),
        ("nova@db:~$ psql -c 'SELECT state,count(*) FROM pg_stat_activity GROUP BY state'", 0.78),
        ("  active     | 99", 0.52),
        ("  idle       |  1", 0.52),
        ("nova@db:~$ # 99 active queries. Only 1 idle slot.", 0.78),
        ("nova@db:~$ psql -c 'SELECT wait_event_type,count(*) FROM pg_stat_activity WHERE state=\'active\' GROUP BY 1'", 0.78),
        ("  Client     | 12", 0.52),
        ("  Lock       | 34", 0.52),
        ("  IO         | 48", 0.52),
        ("  (null)     |  5", 0.52),
        ("nova@db:~$ # 48 connections doing IO, 34 waiting on locks", 0.78),
    ]},
    {"type": "monitor", "worker": "vault", "system": "database", "duration": 24, "interval": 0.35,
     "templates": [
        "  pid:{rand4} state:active duration:00:00:{rand2} SELECT * FROM products WHERE...",
        "  pid:{rand4} state:active duration:00:00:{rand2} INSERT INTO cart_items...",
        "  pid:{rand4} state:active duration:00:00:{rand2} UPDATE orders SET status...",
        "  pid:28431  state:active duration:00:47:{rand2} SELECT * FROM orders JOIN...",
        "  pg_stat: active:99 idle:1 idle_in_tx:0 locks:34",
        "  {ts} connection refused: pool exhausted (100/100)",
        "  {ts} connection refused: pool exhausted (100/100)",
        "  {ts} lock wait: pid:{rand4} waiting on pid:28431",
        "  {ts} IO: read {rand4} blks/s  write 0 blks/s",
    ]},
    {"type": "finding", "worker": "vault", "system": "database",
     "severity": "critical", "text": "DB pool FULL — 100/100 connections, 99 active"},
    {"type": "return", "worker": "vault"},

    # ── Database (dig deeper — STUCK) ──
    {"type": "dispatch", "worker": "vault", "system": "database"},
    {"type": "terminal", "worker": "vault", "system": "database", "lines": [
        ("nova@db:~$ psql -c 'SELECT * FROM pg_stat_activity'", 0.78),
        ("Fetching all 100 rows...", 1.35),
        ("  pid  | state  | duration | query", 0.52),
        ("  ---- | ------ | -------- | -----", 0.39),
        ("  3841 | active | 00:00:04 | SELECT * FROM products...", 0.39),
        ("  3842 | active | 00:00:03 | INSERT INTO cart_items...", 0.39),
        ("  3843 | active | 00:00:12 | SELECT p.*, i.* FROM...", 0.39),
        ("  3844 | active | 00:00:08 | UPDATE orders SET...", 0.39),
        ("  3845 | active | 00:00:02 | SELECT count(*) FROM...", 0.39),
        ("  ... (95 more rows)", 0.78),
        ("nova@db:~$ # Too many connections to analyze manually", 0.78),
        ("nova@db:~$ # Need a strategy to find the culprit", 0.78),
        ("[STUCK] 100 connections — can't identify the problem", 0.78),
    ]},
    {"type": "stuck", "worker": "vault", "system": "database",
     "scenario": "overwhelmed",
     "nova_text": "Agent 4 is drowning in data. 100 connections and it can't tell which one is the problem. Should it sort by how long each has been running, or filter to just the heaviest?",
     "options": [
        {"id": "sort_time", "label": "Sort by Duration", "keywords": ["sort", "duration", "longest", "time", "running", "oldest", "order"],
         "terminal": [
            ("nova@db:~$ psql -c 'SELECT pid,now()-query_start AS duration,left(query,80) FROM pg_stat_activity WHERE state=\'active\' ORDER BY duration DESC LIMIT 5'", 0.78),
            ("  pid   | duration | query", 0.52),
            ("  ----- | -------- | -----", 0.39),
            ("  28431 | 00:47:23 | SELECT * FROM orders JOIN products ON p.id=o.product_id JOIN inventory ON...", 0.78),
            ("  28502 | 00:00:38 | SELECT * FROM products WHERE category_id = 14...", 0.52),
            ("  28510 | 00:00:35 | INSERT INTO cart_items (user_id, product_id...", 0.52),
            ("  28515 | 00:00:34 | SELECT p.*, r.avg_rating FROM products p JOIN...", 0.52),
            ("  28521 | 00:00:33 | UPDATE orders SET status='processing' WHERE...", 0.52),
            ("nova@db:~$ # PID 28431 — running for 47 MINUTES", 0.78),
            ("nova@db:~$ # Next longest is only 38 seconds", 0.78),
            ("nova@db:~$ psql -c 'SELECT query FROM pg_stat_activity WHERE pid=28431'", 0.78),
            ("  SELECT * FROM orders", 0.52),
            ("    JOIN products ON products.id = orders.product_id", 0.52),
            ("    JOIN inventory ON inventory.product_id = products.id", 0.52),
            ("    JOIN shipments ON shipments.order_id = orders.id", 0.52),
            ("    WHERE orders.created_at > '2026-01-01'", 0.52),
            ("    ORDER BY orders.created_at DESC", 0.78),
            ("nova@db:~$ psql -c 'SELECT pg_size_pretty(pg_total_relation_size(\'orders\'))'", 0.78),
            ("  24 GB", 0.78),
            ("nova@db:~$ psql -c 'SELECT indexrelname FROM pg_stat_user_indexes WHERE relname=\'orders\''", 0.78),
            ("  orders_pkey (id only)", 0.52),
            ("  NO INDEX on created_at", 0.78),
            ("--- SUMMARY FOR NOVA ---", 0.78),
            ("ROOT CAUSE: pid 28431 running 47min", 0.52),
            ("Full table scan on 24GB 'orders' table", 0.52),
            ("4-table JOIN with no index on filter column", 0.52),
            ("This one query is holding all connections hostage", 0.52),
         ], "nova_reply": "That's it. One query, 47 minutes, scanning 24 gigs with no index. Every other connection is stuck behind it. Root cause found.", "score": 20},
        {"id": "filter", "label": "Filter Heavy Queries", "keywords": ["filter", "heavy", "resource", "big", "expensive", "cpu"],
         "terminal": [
            ("nova@db:~$ psql -c 'SELECT pid,duration,left(query,60) FROM pg_stat_activity WHERE state!=\'idle\' ORDER BY duration DESC LIMIT 3'", 0.78),
            ("  28431 | 47min | SELECT * FROM orders JOIN products...", 0.78),
            ("  28502 | 38s   | SELECT * FROM products WHERE...", 0.52),
            ("  28510 | 35s   | INSERT INTO cart_items...", 0.52),
            ("nova@db:~$ # Top offender: pid 28431 at 47 minutes", 0.78),
            ("--- SUMMARY FOR NOVA ---", 0.78),
            ("ROOT CAUSE: 47-minute query, 24GB scan, no index", 0.52),
         ], "nova_reply": "Filtered the noise. One massive query — 47 minutes, no index, 24 gig table scan. Root cause found.", "score": 15},
     ], "timeout": 90, "timeout_choice": "sort_time"},
    {"type": "finding", "worker": "vault", "system": "database",
     "severity": "critical", "text": "ROOT CAUSE: Runaway query pid 28431 — 47min, 24GB scan, no index"},
    {"type": "return", "worker": "vault"},

    # ── Vault needs kill command — STUCK ──
    {"type": "dispatch", "worker": "vault", "system": "database"},
    {"type": "terminal", "worker": "vault", "system": "database", "lines": [
        ("nova@db:~$ # Preparing termination command for pid 28431", 0.78),
        ("nova@db:~$ psql --version", 0.78),
        ("psql (PostgreSQL) 15.4", 0.78),
        ("nova@db:~$ # Checking pg_terminate_backend compatibility...", 0.78),
        ("nova@db:~$ # Need to verify syntax for v15...", 1.35),
    ]},
    {"type": "stuck", "worker": "vault", "system": "database",
     "scenario": "needs_research",
     "nova_text": "Agent 4 found the root cause but wants to verify the kill command syntax for Postgres 15. Give it the standard pg_terminate_backend, or have it check the docs first?",
     "options": [
        {"id": "give_cmd", "label": "Just Use pg_terminate_backend", "keywords": ["give", "pg_terminate", "terminate", "command", "standard", "just", "use"],
         "terminal": [
            ("nova@db:~$ # Received command: pg_terminate_backend(28431)", 0.78),
            ("nova@db:~$ psql -c 'SELECT pg_terminate_backend(28431)' --dry-run", 0.78),
            ("  Syntax valid. Command staged.", 0.78),
            ("--- SUMMARY FOR NOVA ---", 0.78),
            ("Kill command staged for pid 28431.", 0.52),
            ("Ready to execute on your order.", 0.52),
         ], "nova_reply": "Loaded the command. Vault is ready to terminate the query on your signal.", "score": 15},
        {"id": "research", "label": "Check Docs First", "keywords": ["look", "search", "docs", "web", "research", "check", "verify"],
         "terminal": [
            ("nova@db:~$ curl -s postgresql.org/docs/15/functions-admin.html | grep terminate", 1.35),
            ("pg_terminate_backend(pid int) -> boolean", 0.78),
            ("Terminates the backend with the specified PID", 0.52),
            ("Available since PostgreSQL 8.4", 0.52),
            ("nova@db:~$ # Confirmed for v15. Staging command.", 0.78),
            ("--- SUMMARY FOR NOVA ---", 0.78),
            ("Verified syntax. Kill command ready.", 0.52),
         ], "nova_reply": "Verified against the docs. Command is correct for our version. Ready to go.", "score": 10},
     ], "timeout": 60, "timeout_choice": "give_cmd"},
    {"type": "return", "worker": "vault"},

    # ── Cache ──
    {"type": "dispatch", "worker": "vault", "system": "cache"},
    {"type": "terminal", "worker": "vault", "system": "cache", "lines": [
        ("nova@redis:~$ redis-cli INFO stats | grep -E 'hit|miss|evict'", 0.78),
        ("keyspace_hits:1204", 0.52),
        ("keyspace_misses:2847", 0.52),
        ("evicted_keys:340", 0.52),
        ("nova@redis:~$ python3 -c 'print(1204/(1204+2847)*100)'", 0.78),
        ("29.7%  (normally 94%)", 0.78),
        ("nova@redis:~$ redis-cli INFO memory | grep used_memory_human", 0.78),
        ("used_memory_human:508.42M", 0.52),
        ("nova@redis:~$ redis-cli INFO stats | grep evicted_keys_per_sec", 0.78),
        ("84/sec", 0.78),
        ("nova@redis:~$ # App can't write back to cache — DB blocked", 0.78),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("Cache: 29.7% hit rate (was 94%). Evicting 84 keys/sec.", 0.52),
        ("App can't refresh cache because DB is blocked.", 0.52),
    ]},
    {"type": "monitor", "worker": "vault", "system": "cache", "duration": 15, "interval": 0.4,
     "templates": [
        "  {ts} GET product:{rand4} MISS (backend timeout)",
        "  {ts} GET session:{rand4} HIT",
        "  {ts} GET cart:{rand4} MISS (backend timeout)",
        "  {ts} EVICT product:{rand4} (LRU, mem pressure)",
        "  {ts} GET product:{rand4} MISS",
        "  {ts} SET product:{rand4} FAIL (write timeout)",
        "  {ts} keys: {rand4}  mem: 508MB/{rand3}MB  evict/s: {rand2}",
    ]},
    {"type": "finding", "worker": "vault", "system": "cache",
     "severity": "warning", "text": "Cache collapsed — 29.7% hit rate, 84 evictions/sec"},
    {"type": "return", "worker": "vault"},

    # ── Message Queue ──
    {"type": "dispatch", "worker": "vault", "system": "message_queue"},
    {"type": "terminal", "worker": "vault", "system": "message_queue", "lines": [
        ("nova@mq:~$ rabbitmqctl list_queues name messages consumers", 0.78),
        ("order_processing     4200    0", 0.52),
        ("email_notifications   340    0", 0.52),
        ("inventory_sync        127    0", 0.52),
        ("analytics_events      891    0", 0.52),
        ("nova@mq:~$ rabbitmqctl list_connections | wc -l", 0.78),
        ("0 active connections", 0.78),
        ("nova@mq:~$ # All consumers disconnected", 0.52),
        ("nova@mq:~$ tail -3 /var/log/rabbitmq/rabbit.log", 0.78),
        ("14:23:14 connection_closed: consumer lost db connection", 0.52),
        ("14:23:15 connection_closed: consumer lost db connection", 0.52),
        ("14:23:15 connection_closed: consumer lost db connection", 0.52),
        ("nova@mq:~$ # Consumers crashed because they can't reach DB", 0.78),
        ("--- SUMMARY FOR NOVA ---", 0.78),
        ("Queue: 5558 msgs pending, 0 consumers.", 0.52),
        ("All consumers died when DB connections ran out.", 0.52),
    ]},
    {"type": "monitor", "worker": "vault", "system": "message_queue", "duration": 15, "interval": 0.4,
     "templates": [
        "  {ts} order_processing +1 (pending: {rand4})",
        "  {ts} order_processing +1 (pending: {rand4})",
        "  {ts} email_notifications +1 (pending: {rand3})",
        "  {ts} consumer reconnect attempt... FAILED (db unavailable)",
        "  {ts} dead letter: order:{rand4} reason: consumer_timeout",
        "  {ts} inventory_sync +1 (pending: {rand3})",
        "  {ts} consumer reconnect attempt... FAILED",
    ]},
    {"type": "finding", "worker": "vault", "system": "message_queue",
     "severity": "warning", "text": "Queue: 5558 msgs pending, 0 consumers (DB killed them)"},
    {"type": "return", "worker": "vault"},

    {"type": "report", "worker": "vault", "text": "Root cause confirmed: runaway query pid 28431 consuming all DB connections. Cache and queue are downstream casualties. Kill command staged and ready."},
]

# Register the sweep scripts
SWEEP_SCRIPTS = {
    "route_sweep": ROUTE_SWEEP,
    "scout_sweep": SCOUT_SWEEP,
    "ping_sweep": PING_SWEEP,
    "vault_sweep": VAULT_SWEEP,
}


class ScenarioEngine:
    def __init__(self):
        self.systems = {}
        self.phase = "idle"
        self.alarm_time = None
        self.revenue_loss = 0
        self.loss_rate = 9.60
        self.waiting_for_human = None
        self.score = 0
        self.decisions_made = []
        self.callbacks = []
        self.workers_done = set()
        self.reset()

    def reset(self):
        self.systems = {sid: {"name": s["name"], "short": s["short"], "status": "healthy",
                              "metrics": dict(INITIAL_METRICS.get(sid, {}))}
                        for sid, s in SYSTEMS.items()}
        self.phase = "idle"
        self.alarm_time = None
        self.revenue_loss = 0
        self.waiting_for_human = None
        self.score = 0
        self.decisions_made = []
        self.workers_done = set()
        self.last_spoke = time.time()
        self.decision_engine = DecisionEngine()
        self.last_dynamic_decision = time.time()  # Prevent immediate dynamic decisions
        self.pending_dynamic_decision = None
        self.scripted_busy = False
        self.decision_lock = asyncio.Lock()
        self.stuck_queue = asyncio.Lock()  # Only one stuck moment at a time
        self.game_log = []

    def log_event(self, event_type, detail="", score_change=0):
        elapsed = time.time() - self.alarm_time if self.alarm_time else 0
        entry = {
            "time": round(elapsed, 1),
            "type": event_type,
            "detail": detail,
            "score_change": score_change,
            "total_score": self.score,
            "revenue_loss": round(self.revenue_loss, 2),
        }
        self.game_log.append(entry)
        # Fire and forget notify — can't await in sync method
        asyncio.ensure_future(self.notify("log_entry", entry))

    def get_state(self):
        elapsed = time.time() - self.alarm_time if self.alarm_time else 0
        return {"phase": self.phase, "systems": self.systems,
                "revenue_loss": round(self.revenue_loss, 2),
                "elapsed": round(elapsed, 1), "score": self.score}

    async def notify(self, etype, data=None):
        if etype in ("nova_speak", "nova_subtitle"):
            self.last_spoke = time.time()
        for cb in self.callbacks:
            try: await cb(etype, data or {})
            except: pass

    async def start_calm(self):
        self.reset()
        self.phase = "calm"
        await self.notify("phase_change", {"phase": "calm"})
        await self.notify("state_update", self.get_state())

        # Show orbs from the start
        worker_info = {wid: {"name": w["name"], "color": w["color"]} for wid, w in WORKERS.items()}
        await self.notify("workers_idle", {"workers": worker_info})

        # Don't speak on load — wait for drill button
        # Nova's intro plays when trigger_alarm is called

    async def trigger_alarm(self):
        # Nova intro + announcement
        await self.notify("nova_subtitle", {
            "text": "Hey, I'm Nova. I manage infrastructure for Oracle Lens. Ten systems, four agents on standby."
        })
        await self.notify("nova_speak", {
            "text": "Hey, I'm Nova. I manage infrastructure for Oracle Lens. Ten systems, four agents on standby.",
            "emotion": "amused"
        })

        await asyncio.sleep(4)

        await self.notify("nova_subtitle", {
            "text": "Let's see what happens when things go wrong."
        })
        await self.notify("nova_speak", {
            "text": "Let's see what happens when things go wrong.",
            "emotion": "neutral"
        })

        await asyncio.sleep(3)

        await self.notify("nova_subtitle", {
            "text": "Anomaly detected in database. Let's investigate."
        })
        await self.notify("nova_speak", {
            "text": "Anomaly detected in database. Let's investigate.",
            "emotion": "concerned"
        })

        await asyncio.sleep(3)

        self.phase = "alarm"
        self.alarm_time = time.time()
        await self.notify("phase_change", {"phase": "alarm"})

        await self.notify("nova_speak", {
            "text": "Multiple systems going red. This is spreading fast. I need to deploy agents now.",
            "emotion": "concerned"
        })

        # Cascade — database goes first, Nova narrates as it spreads
        cascade_comments = {
            "database": "Database just went critical. Connection pool is maxed out.",
            "app_server": "App server is struggling now. It's cascading.",
            "web_server": "Web server going down. Customers are seeing errors.",
            "cache": "Cache is failing. Hit rate is dropping fast.",
            "message_queue": "Message queue is backing up. Jobs are piling up.",
            "payment_api": "Payments are timing out. We're losing revenue.",
            "load_balancer": "Load balancer is pulling nodes offline. This is bad.",
        }

        prev_t = 0
        for delay, sys_id, status in CRISIS_CASCADE:
            wait = delay - prev_t
            if wait > 0: await asyncio.sleep(wait)
            prev_t = delay
            self.systems[sys_id]["status"] = status
            self.systems[sys_id]["metrics"] = CRISIS_METRICS.get(sys_id, self.systems[sys_id]["metrics"])
            self.revenue_loss = (time.time() - self.alarm_time) * self.loss_rate
            await self.notify("system_update", {"system": sys_id, "status": status,
                "metrics": self.systems[sys_id]["metrics"]})
            # Nova narrates key moments
            if sys_id in cascade_comments:
                await self.notify("nova_subtitle", {"text": cascade_comments[sys_id]})

        self.phase = "investigating"

    async def deploy_workers(self):
        """Run all four sweeps in parallel. Route and Scout finish faster."""

        # Track which workers finish
        finished = set()
        wait_decision_shown = False

        async def run_and_track(name, script):
            await self._run_script(script)
            finished.add(name)

            # When Route + Scout done but Vault still working — ask human
            nonlocal wait_decision_shown
            if not wait_decision_shown and "route_sweep" in finished and "scout_sweep" in finished:
                if "vault_sweep" not in finished:
                    wait_decision_shown = True
                    # Wait for any active stuck moment to finish first
                    async with self.stuck_queue:
                        await self.notify("nova_speak", {
                            "text": "Agent 1 and Agent 2 are back. Network and external are clean. But Agent 4 is still digging through the database. Wait for the full picture, or act now?",
                            "emotion": "thinking"
                        })
                        self.waiting_for_human = ("_wait_decision", {
                            "options": [
                                {"id": "wait", "label": "Wait for Vault", "keywords": ["wait", "full", "picture", "let", "finish", "vault", "patience"],
                                 "terminal": [], "nova_reply": "Smart. Let's get the full picture before we act.", "score": 15},
                                {"id": "act_now", "label": "Act on What We Have", "keywords": ["act", "now", "go", "enough", "move", "don't wait"],
                                 "terminal": [], "nova_reply": "We know enough to start narrowing it down. But we might miss the root cause.", "score": 5},
                            ],
                            "timeout": 60, "timeout_choice": "wait",
                            "nova_text": "", "system": "database",
                        })
                        await self.notify("show_stuck_options", {"worker": "_wait",
                            "options": [
                                {"id": "wait", "label": "Wait for Full Picture"},
                                {"id": "act_now", "label": "Act Now"},
                            ]})
                        start = time.time()
                        while self.waiting_for_human and self.waiting_for_human[0] == "_wait_decision":
                            await asyncio.sleep(0.5)
                            if time.time() - start > 20:
                                self.waiting_for_human = None
                                await self.notify("nova_speak", {"text": "Going to wait. Agent 4 is close.", "emotion": "neutral"})
                                await self.notify("hide_stuck_options", {})
                                break

        tasks = []
        for name, script in SWEEP_SCRIPTS.items():
            tasks.append(asyncio.create_task(run_and_track(name, script)))
        await asyncio.gather(*tasks)

        # All done — final decision
        await asyncio.sleep(2.5)
        self.phase = "deciding"
        self.scripted_busy = True
        await self.notify("nova_speak", {
            "text": "All agents back. Root cause: a runaway query eating every database connection. Agent 4 has the kill command ready. Your call — send Agent 4 to kill it, or restart the server first?",
            "emotion": "neutral"
        })
        await self.notify("show_decisions", {"options": [
            {"id": "kill_query", "label": "Send Agent 4 to Kill Query", "desc": "Instant fix, report data lost"},
            {"id": "restart_server", "label": "Send Agent 3 to Restart Server", "desc": "Buy time, root cause stays"},
        ]})

    async def _run_script(self, script):
        for step in script:
            st = step["type"]

            if st == "dispatch":
                await self.notify("orb_dispatch", {"worker": step["worker"], "system": step["system"],
                    "color": WORKERS[step["worker"]]["color"], "name": WORKERS[step["worker"]]["name"]})
                await asyncio.sleep(2.5)

            elif st == "terminal":
                for text, delay in step["lines"]:
                    await asyncio.sleep(delay)
                    await self.notify("terminal_line", {"system": step["system"],
                        "worker": step["worker"], "color": WORKERS[step["worker"]]["color"], "text": text})

            elif st == "monitor":
                # Continuously stream rolling output for `duration` seconds
                import random
                end_time = asyncio.get_event_loop().time() + step["duration"]
                templates = step["templates"]
                while asyncio.get_event_loop().time() < end_time:
                    line = random.choice(templates)
                    # Replace {rand} placeholders with random values
                    line = line.replace("{rand3}", str(random.randint(100, 999)))
                    line = line.replace("{rand2}", str(random.randint(10, 99)))
                    line = line.replace("{rand4}", str(random.randint(1000, 9999)))
                    line = line.replace("{randf}", f"{random.uniform(0.1, 99.9):.1f}")
                    line = line.replace("{randip}", f"10.0.0.{random.randint(2, 254)}")
                    line = line.replace("{ts}", f"14:{random.randint(23,27)}:{random.randint(10,59):02d}")
                    await asyncio.sleep(step.get("interval", 0.39))
                    await self.notify("terminal_line", {"system": step["system"],
                        "worker": step["worker"], "color": WORKERS[step["worker"]]["color"], "text": line})

            elif st == "clear":
                await self.notify("issue_clear", {"system": step["system"], "text": step["text"],
                    "worker": step["worker"], "color": WORKERS[step["worker"]]["color"]})

            elif st == "finding":
                await self.notify("issue_found", {"system": step["system"], "text": step["text"],
                    "severity": step["severity"], "worker": step["worker"],
                    "color": WORKERS[step["worker"]]["color"]})

            elif st == "return":
                await self.notify("orb_return", {"worker": step["worker"]})
                await asyncio.sleep(2.5)

            elif st == "report":
                self.workers_done.add(step["worker"])
                await self.notify("worker_report", {"worker": step["worker"],
                    "name": WORKERS[step["worker"]]["name"],
                    "color": WORKERS[step["worker"]]["color"], "text": step["text"]})

            elif st == "stuck":
                # Wait for any other stuck moment to finish first
                async with self.stuck_queue:
                    self.scripted_busy = True
                    # Cancel any pending dynamic decision
                    if self.waiting_for_human and self.waiting_for_human[0] == "_dynamic":
                        self.waiting_for_human = None
                        await self.notify("hide_stuck_options", {})
                        await asyncio.sleep(1)

                    self.waiting_for_human = (step["worker"], step)
                    await self.notify("worker_stuck", {"worker": step["worker"],
                        "system": step["system"], "scenario": step.get("scenario", ""),
                        "name": WORKERS[step["worker"]]["name"],
                        "color": WORKERS[step["worker"]]["color"]})
                    await self.notify("nova_speak", {"text": step["nova_text"], "emotion": "thinking"})
                    await self.notify("show_stuck_options", {"worker": step["worker"],
                        "options": [{"id": o["id"], "label": o["label"]} for o in step["options"]]})

                    # Wait for human
                    start = time.time()
                    while self.waiting_for_human and self.waiting_for_human[0] == step["worker"]:
                        await asyncio.sleep(0.5)
                        if self.alarm_time:
                            self.revenue_loss = (time.time() - self.alarm_time) * self.loss_rate
                        if time.time() - start > step.get("timeout", 4.0):
                            await self._resolve_stuck(step["worker"], step["timeout_choice"], step, auto=True)
                            break

                    # Small gap between stuck moments
                    await asyncio.sleep(2)

            elif st == "pause":
                await asyncio.sleep(step.get("duration", 2.5))

    async def _resolve_stuck(self, worker_id, choice_id, step, auto=False):
        # Only clear if this is still the active decision
        if self.waiting_for_human and self.waiting_for_human[0] == worker_id:
            self.waiting_for_human = None
        self.scripted_busy = False
        chosen = next((o for o in step["options"] if o["id"] == choice_id), step["options"][0])

        for text, delay in chosen.get("terminal", []):
            await asyncio.sleep(delay)
            await self.notify("terminal_line", {"system": step["system"],
                "worker": worker_id, "color": WORKERS[worker_id]["color"], "text": text})

        if auto:
            prefix = "Timed out, going with default. "
        else:
            prefix = f"Confirmed: {chosen.get('label', chosen['id'])}. "
        await self.notify("nova_speak", {"text": prefix + chosen["nova_reply"],
            "emotion": "impressed" if not auto else "neutral"})

        bonus = chosen.get("score", 1.3)
        if auto: bonus = max(0, bonus - 10)
        self.score += bonus
        self.log_event("agent_guided", f"{WORKERS[worker_id]['name']}: {chosen.get('id', '?')}", bonus)
        await self.notify("score_update", {"score": self.score, "bonus": bonus,
            "reason": f"{WORKERS[worker_id]['name']} guided"})
        await self.notify("hide_stuck_options", {})

    async def handle_human_input(self, text):
        if not self.waiting_for_human:
            return False
        worker_id, step = self.waiting_for_human
        text_lower = text.lower()

        # Handle dynamic decisions from the decision engine
        if worker_id == "_dynamic":
            for opt in step["options"]:
                kws = opt.get("keywords", [])
                if any(kw in text_lower for kw in kws) or opt.get("label", "").lower() in text_lower:
                    await self._resolve_dynamic_decision(opt, step)
                    return True
            # If no match, pick closest or ask again
            await self._resolve_dynamic_decision(step["options"][0], step)
            return True

        # Handle wait/coordinate decisions
        if worker_id == "_wait_decision" or worker_id == "_coordinate":
            for opt in step["options"]:
                if any(kw in text_lower for kw in opt.get("keywords", [])):
                    self.waiting_for_human = None
                    self.score += opt.get("score", 0)
                    await self.notify("nova_speak", {"text": opt.get("nova_reply", opt.get("result", "OK.")), "emotion": "neutral"})
                    await self.notify("hide_stuck_options", {})
                    await self.notify("score_update", {"score": self.score, "bonus": opt.get("score", 0), "reason": "tactical"})
                    return True
            self.waiting_for_human = None
            await self.notify("hide_stuck_options", {})
            return True

        for opt in step["options"]:
            if any(kw in text_lower for kw in opt["keywords"]):
                await self._resolve_stuck(worker_id, opt["id"], step)
                return True

        labels = [o["label"] for o in step["options"]]
        await self.notify("nova_speak", {
            "text": f"Not sure I follow. The options are: {' or '.join(labels)}.",
            "emotion": "thinking"})
        return True

    async def execute_decision(self, decision):
        self.decisions_made.append(decision)
        self.phase = "recovering"
        self.scripted_busy = True

        if decision == "kill_query":
            # First attempt: just kill it naively
            await self.notify("nova_speak", {"text": "Sending Vault to kill the query.",
                "emotion": "neutral"})
            await self.notify("orb_dispatch", {"worker": "vault", "system": "database",
                "color": WORKERS["vault"]["color"], "name": "Agent 4"})
            await asyncio.sleep(2.5)
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": "nova@db:~$ SELECT pg_terminate_backend(28431);"})
            await asyncio.sleep(1.0)
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": "  pg_terminate_backend: true"})
            await asyncio.sleep(0.5)
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": "  Query killed. Connections releasing..."})
            await asyncio.sleep(1.0)

            # THUNDERING HERD — everything spikes
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": "  connections: 100 -> 42 -> 18..."})
            await asyncio.sleep(0.8)
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": "  connections: 18 -> 54 -> 87 -> 100"})
            await asyncio.sleep(0.5)
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": "[!!] CONNECTIONS SPIKING BACK TO 100"})
            await asyncio.sleep(0.5)
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": "[!!] 99 blocked queries just stampeded in"})

            # App server and MQ also spike
            await self.notify("terminal_line", {"system": "app_server", "worker": "ping",
                "color": WORKERS["ping"]["color"], "text": "[!!] Thread pool flooded: 24/24 + 200 queued"})
            await self.notify("terminal_line", {"system": "message_queue", "worker": "ping",
                "color": WORKERS["ping"]["color"], "text": "[!!] 4200 messages rushing consumers simultaneously"})

            self.systems["database"]["status"] = "critical"
            self.systems["app_server"]["status"] = "critical"
            self.systems["message_queue"]["status"] = "critical"
            await self.notify("system_update", {"system": "database", "status": "critical",
                "metrics": {"Conn": "100/100", "Lag": "12s", "SlowQ": 0}})
            await self.notify("system_update", {"system": "app_server", "status": "critical",
                "metrics": {"Threads": "24/24", "Queue": 224, "Resp": "timeout"}})
            await self.notify("system_update", {"system": "message_queue", "status": "critical",
                "metrics": {"Pending": 4200, "Lag": "flooding", "Dead": 89}})

            await asyncio.sleep(2)
            await self.notify("orb_return", {"worker": "vault"})
            await asyncio.sleep(1)

            await self.notify("nova_speak", {
                "text": "That made it worse. Killing the query freed the connections, but 99 blocked queries and 4200 queued messages all stampeded in at once. We caused a thundering herd. We need to do this in stages — throttle first, then kill. Let me coordinate all four agents.",
                "emotion": "concerned"
            })

            await self.notify("issue_found", {"system": "database", "text": "THUNDERING HERD — stampede after naive kill",
                "severity": "critical", "worker": "vault", "color": WORKERS["vault"]["color"]})

            await asyncio.sleep(3)
            await self._coordinated_recovery()

        elif decision == "restart_server":
            await self.notify("nova_speak", {"text": "Sending Ping to restart the web server.",
                "emotion": "neutral"})
            await self.notify("orb_dispatch", {"worker": "ping", "system": "web_server",
                "color": WORKERS["ping"]["color"], "name": "Agent 3"})
            await asyncio.sleep(2.5)
            await self.notify("terminal_line", {"system": "web_server", "worker": "ping",
                "color": WORKERS["ping"]["color"], "text": "nova@web:~$ systemctl restart app-server"})
            await asyncio.sleep(2.5)
            await self.notify("terminal_line", {"system": "web_server", "worker": "ping",
                "color": WORKERS["ping"]["color"], "text": "Restarting..."})
            await asyncio.sleep(3)
            self.systems["web_server"]["status"] = "recovering"
            await self.notify("system_update", {"system": "web_server", "status": "recovering",
                "metrics": {"CPU": "22%", "MEM": "35%", "HTTP": "94%", "RPS": 280}})
            await asyncio.sleep(4)

            self.systems["web_server"]["status"] = "warning"
            await self.notify("system_update", {"system": "web_server", "status": "warning",
                "metrics": {"CPU": "38%", "MEM": "88%", "HTTP": "52%", "RPS": 342}})
            await self.notify("terminal_line", {"system": "web_server", "worker": "ping",
                "color": WORKERS["ping"]["color"], "text": "[!] Memory climbing again. Root cause still active."})
            await self.notify("orb_return", {"worker": "ping"})

            await asyncio.sleep(2.5)
            await self.notify("nova_speak", {
                "text": "Came back briefly but it's failing again. The query is still running. We need to kill it — but carefully. Send Agent 4?",
                "emotion": "concerned"})
            await self.notify("show_decisions", {"options": [
                {"id": "kill_query", "label": "Send Agent 4 to Kill Query", "desc": "End the runaway query"},
            ]})

    async def _coordinated_recovery(self):
        """The real fix: all 4 agents working in parallel on different systems."""
        await self.notify("nova_speak", {
            "text": "Here's the plan. Agent 4 throttles the connection pool so only 20 can rush in. Agent 3 pauses the message queue. Then Agent 4 kills the query safely. Agent 1 monitors the load balancer to bring nodes back one at a time. All four move at once. Ready?",
            "emotion": "thinking"
        })

        await self.notify("show_decisions", {"options": [
            {"id": "coordinate", "label": "Execute Coordinated Recovery", "desc": "All 4 agents, synchronized"},
        ]})

        # Wait for human to approve
        self.waiting_for_human = ("_coordinate", {
            "options": [
                {"id": "coordinate", "label": "Go", "keywords": ["go", "execute", "yes", "ready", "do it", "coordinate", "send"]},
            ],
            "timeout": 90, "timeout_choice": "coordinate",
            "nova_text": "", "system": "database",
        })
        start = time.time()
        while self.waiting_for_human and self.waiting_for_human[0] == "_coordinate":
            await asyncio.sleep(0.5)
            if time.time() - start > 30:
                self.waiting_for_human = None
                break

        await self.notify("hide_stuck_options", {})
        self.score += 15
        await self.notify("score_update", {"score": self.score, "bonus": 15, "reason": "Coordinated approach"})

        await self.notify("nova_speak", {
            "text": "Deploying all four. Watch the terminals.",
            "emotion": "impressed"
        })

        await asyncio.sleep(2.5)

        # ── STEP 1: Deploy all 4 agents simultaneously ──
        await self.notify("orb_dispatch", {"worker": "vault", "system": "database",
            "color": WORKERS["vault"]["color"], "name": "Agent 4"})
        await self.notify("orb_dispatch", {"worker": "ping", "system": "message_queue",
            "color": WORKERS["ping"]["color"], "name": "Agent 3"})
        await self.notify("orb_dispatch", {"worker": "route", "system": "load_balancer",
            "color": WORKERS["route"]["color"], "name": "Agent 1"})
        await self.notify("orb_dispatch", {"worker": "scout", "system": "web_server",
            "color": WORKERS["scout"]["color"], "name": "Agent 2"})

        await asyncio.sleep(2)

        # ── STEP 2: Agent 4 throttles pool, Agent 3 pauses queue — in parallel ──
        async def vault_throttle():
            W, S = "vault", "database"
            C = WORKERS[W]["color"]
            for text, delay in [
                ("nova@db:~$ psql -c 'ALTER SYSTEM SET max_connections = 20'", 1.35),
                ("ALTER SYSTEM", 0.78),
                ("nova@db:~$ SELECT pg_reload_conf();", 1.35),
                ("  pg_reload_conf: true", 0.78),
                ("nova@db:~$ # Pool throttled: max 20 connections", 0.78),
                ("nova@db:~$ psql -c 'SELECT count(*) FROM pg_stat_activity'", 1.35),
                ("  Waiting for connections to drain...", 1.3),
                ("  active: 100 -> 84 -> 61 -> 42", 1.95),
                ("  active: 42 -> 28 -> 20", 1.95),
                ("  Pool capped at 20. Stable.", 1.35),
                ("[OK] Connection pool throttled to 20", 0.78),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": S, "worker": W, "color": C, "text": text})

        async def ping_pause_queue():
            W, S = "ping", "message_queue"
            C = WORKERS[W]["color"]
            for text, delay in [
                ("nova@mq:~$ rabbitmqctl set_policy pause-all '.*' '{\"max-length\":0}'", 1.35),
                ("Setting policy...", 0.78),
                ("nova@mq:~$ rabbitmqctl list_consumers", 1.35),
                ("  Disconnecting consumers...", 1.3),
                ("  consumer order_proc_1: stopped", 0.78),
                ("  consumer order_proc_2: stopped", 0.78),
                ("  consumer email_worker: stopped", 0.78),
                ("  consumer inventory_sync: stopped", 0.78),
                ("  All consumers paused.", 1.35),
                ("nova@mq:~$ rabbitmqctl list_queues name messages", 1.35),
                ("  order_processing: 4200 (held)", 0.78),
                ("  email_notifications: 340 (held)", 0.78),
                ("[OK] Queue consumers paused. Messages held.", 0.78),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": S, "worker": W, "color": C, "text": text})

        async def route_monitor_lb():
            W, S = "route", "load_balancer"
            C = WORKERS[W]["color"]
            for text, delay in [
                ("nova@lb:~$ # Monitoring traffic during recovery", 0.78),
                ("nova@lb:~$ watch -n1 'nginx -T | grep upstream'", 1.35),
                ("  node-1: DOWN  node-2: DOWN  node-3: DOWN  node-4: UP", 1.3),
                ("  Holding all traffic on node-4 only...", 1.3),
                ("  rps: 170  latency: 2400ms", 1.3),
                ("  rps: 165  latency: 2600ms", 1.3),
                ("  Standing by for recovery signal...", 1.3),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": S, "worker": W, "color": C, "text": text})

        async def scout_monitor_web():
            W, S = "scout", "web_server"
            C = WORKERS[W]["color"]
            for text, delay in [
                ("nova@web:~$ # Monitoring web server during recovery", 0.78),
                ("nova@web:~$ watch -n1 'curl -s localhost/health'", 1.35),
                ("  14:28:01 HTTP 503 — MEM: 97% — still degraded", 1.3),
                ("  14:28:02 HTTP 503 — MEM: 96% — waiting...", 1.3),
                ("  14:28:03 HTTP 503 — MEM: 95% — slow improvement", 1.3),
                ("  Standing by for recovery signal...", 1.3),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": S, "worker": W, "color": C, "text": text})

        # Run all 4 in parallel
        await asyncio.gather(vault_throttle(), ping_pause_queue(), route_monitor_lb(), scout_monitor_web())

        await asyncio.sleep(2.5)
        await self.notify("nova_speak", {
            "text": "Pool throttled, queue paused. Now it's safe. Vault — kill the query.",
            "emotion": "neutral"
        })

        # ── STEP 3: Agent 4 kills the query safely ──
        await asyncio.sleep(2.5)
        for text, delay in [
            ("nova@db:~$ SELECT pg_terminate_backend(28431);", 1.3),
            ("  pg_terminate_backend: true", 1.35),
            ("  Query terminated.", 0.78),
            ("  Connections releasing...", 1.35),
            ("  active: 20 -> 14 -> 8 -> 3", 1.95),
            ("  No stampede. Pool holding at max 20.", 1.3),
            ("[OK] Query killed safely. No thundering herd.", 1.35),
        ]:
            await asyncio.sleep(delay)
            await self.notify("terminal_line", {"system": "database", "worker": "vault",
                "color": WORKERS["vault"]["color"], "text": text})

        self.score += 20
        await self.notify("score_update", {"score": self.score, "bonus": 20, "reason": "Safe kill"})
        await self.notify("issue_found", {"system": "database", "text": "Query killed safely — no stampede",
            "severity": "ok", "worker": "vault", "color": WORKERS["vault"]["color"]})

        await asyncio.sleep(2)

        # ── STEP 4: Restore pool and slowly resume queue ──
        await self.notify("nova_speak", {
            "text": "Query's dead, pool is stable. Now restoring max connections and resuming the queue at a controlled rate. Route — bring nodes back one at a time.",
            "emotion": "neutral"
        })

        await asyncio.sleep(2.5)

        async def vault_restore():
            W, C = "vault", WORKERS["vault"]["color"]
            for text, delay in [
                ("nova@db:~$ ALTER SYSTEM SET max_connections = 100;", 1.35),
                ("nova@db:~$ SELECT pg_reload_conf();", 1.35),
                ("  Pool restored to 100. Current active: 8", 1.3),
                ("  Connections stable. No spike.", 1.3),
                ("[OK] Connection pool restored", 1.35),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "database", "worker": W, "color": C, "text": text})
            self.systems["database"]["status"] = "healthy"
            self.systems["database"]["metrics"] = dict(INITIAL_METRICS.get("database", {}))
            await self.notify("system_update", {"system": "database", "status": "healthy",
                "metrics": self.systems["database"]["metrics"]})

        async def ping_resume():
            W, C = "ping", WORKERS["ping"]["color"]
            for text, delay in [
                ("nova@mq:~$ rabbitmqctl clear_policy pause-all", 1.35),
                ("  Resuming consumers with rate limit: 50 msg/s", 1.3),
                ("  consumer order_proc_1: started (throttled)", 1.35),
                ("  consumer order_proc_2: started (throttled)", 1.35),
                ("  Processing: 50/s... 4200 -> 3800 -> 3400", 1.95),
                ("  Processing: 50/s... 3400 -> 2900 -> 2400", 1.95),
                ("  Backlog draining steadily. No flood.", 1.3),
                ("[OK] Queue resuming at controlled rate", 1.35),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "message_queue", "worker": W, "color": C, "text": text})
            self.systems["message_queue"]["status"] = "recovering"
            await self.notify("system_update", {"system": "message_queue", "status": "recovering",
                "metrics": {"Pending": 2400, "Lag": "draining", "Dead": 23}})

        async def route_restore():
            W, C = "route", WORKERS["route"]["color"]
            for text, delay in [
                ("nova@lb:~$ # Bringing nodes back one at a time", 0.78),
                ("nova@lb:~$ nginx -s reload  # enable node-2", 1.35),
                ("  node-2: checking... HTTP 200. HEALTHY.", 1.95),
                ("  Active nodes: 2/4  rps: 340", 1.3),
                ("nova@lb:~$ nginx -s reload  # enable node-3", 1.35),
                ("  node-3: checking... HTTP 200. HEALTHY.", 1.95),
                ("  Active nodes: 3/4  rps: 510", 1.3),
                ("nova@lb:~$ nginx -s reload  # enable node-4", 1.35),
                ("  node-4: already up. All nodes HEALTHY.", 1.3),
                ("[OK] All 4 nodes back online", 1.35),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "load_balancer", "worker": W, "color": C, "text": text})
            self.systems["load_balancer"]["status"] = "healthy"
            self.systems["load_balancer"]["metrics"] = dict(INITIAL_METRICS.get("load_balancer", {}))
            await self.notify("system_update", {"system": "load_balancer", "status": "healthy",
                "metrics": self.systems["load_balancer"]["metrics"]})

        async def scout_watch():
            W, C = "scout", WORKERS["scout"]["color"]
            for text, delay in [
                ("nova@web:~$ # Monitoring recovery...", 0.78),
                ("  14:29:01 HTTP 200 — MEM: 82% — improving", 1.95),
                ("  14:29:03 HTTP 200 — MEM: 71% — GC reclaiming", 1.95),
                ("  14:29:05 HTTP 200 — MEM: 58% — stabilizing", 1.95),
                ("  14:29:07 HTTP 200 — MEM: 47% — nominal range", 1.95),
                ("[OK] Web server recovered. Memory normal.", 1.35),
            ]:
                await asyncio.sleep(delay)
                await self.notify("terminal_line", {"system": "web_server", "worker": W, "color": C, "text": text})
            self.systems["web_server"]["status"] = "healthy"
            self.systems["web_server"]["metrics"] = dict(INITIAL_METRICS.get("web_server", {}))
            await self.notify("system_update", {"system": "web_server", "status": "healthy",
                "metrics": self.systems["web_server"]["metrics"]})

        await asyncio.gather(vault_restore(), ping_resume(), route_restore(), scout_watch())

        # Return all orbs
        for w in WORKERS:
            await self.notify("orb_return", {"worker": w})
        await asyncio.sleep(2.5)

        # Clean up remaining systems
        for sid in ["app_server", "cache", "payment_api"]:
            self.systems[sid]["status"] = "healthy"
            self.systems[sid]["metrics"] = dict(INITIAL_METRICS.get(sid, {}))
            await self.notify("system_update", {"system": sid, "status": "healthy",
                "metrics": self.systems[sid]["metrics"]})
            await asyncio.sleep(0.5)

        await self._post_recovery()
        await self._finish()

    async def _run_recovery(self):
        prev_t = 0
        for delay, sys_id, status in RECOVERY_ORDER:
            wait = delay - prev_t
            if wait > 0: await asyncio.sleep(wait)
            prev_t = delay
            self.systems[sys_id]["status"] = status
            if status == "healthy":
                self.systems[sys_id]["metrics"] = dict(INITIAL_METRICS.get(sys_id, {}))
            self.revenue_loss = (time.time() - self.alarm_time) * self.loss_rate
            await self.notify("system_update", {"system": sys_id, "status": status,
                "metrics": self.systems[sys_id]["metrics"]})

    async def _post_recovery(self):
        """Send agents to deploy safeguards."""
        await asyncio.sleep(1)
        safeguards = [
            {"worker": "vault", "system": "database", "text": "nova@db:~$ ALTER SYSTEM SET statement_timeout = '60s';", "finding": "Query timeout set to 60s"},
            {"worker": "ping", "system": "web_server", "text": "nova@web:~$ echo 'alert_if memory > 80%' >> /etc/monitoring.conf", "finding": "Memory alert at 80%"},
            {"worker": "scout", "system": "payment_api", "text": "nova@ext:~$ curl -X POST slack.com/api/chat.postMessage -d 'Incident report...'", "finding": "Incident report sent to Slack"},
            {"worker": "route", "system": "load_balancer", "text": "nova@lb:~$ /usr/local/bin/full-health-sweep --all", "finding": "Full health sweep complete"},
        ]
        await self.notify("nova_speak", {"text": "Systems recovering. Deploying safeguards so this doesn't happen again.", "emotion": "neutral"})
        for sg in safeguards:
            await self.notify("orb_dispatch", {"worker": sg["worker"], "system": sg["system"],
                "color": WORKERS[sg["worker"]]["color"], "name": WORKERS[sg["worker"]]["name"]})
            await asyncio.sleep(0.5)
            await self.notify("terminal_line", {"system": sg["system"], "worker": sg["worker"],
                "color": WORKERS[sg["worker"]]["color"], "text": sg["text"]})
            await asyncio.sleep(0.5)
            await self.notify("issue_found", {"system": sg["system"], "text": sg["finding"],
                "severity": "ok", "worker": sg["worker"], "color": WORKERS[sg["worker"]]["color"]})
            await self.notify("orb_return", {"worker": sg["worker"]})
            await asyncio.sleep(0.5)

    async def _finish(self):
        await asyncio.sleep(1)
        self.phase = "resolved"
        self.scripted_busy = False
        elapsed = time.time() - self.alarm_time if self.alarm_time else 0

        if elapsed < 120: self.score += 30; t = "Under 2 minutes — fast."
        elif elapsed < 180: self.score += 15; t = "Under 3 minutes."
        else: t = "Took a while, but you got there."

        if len(self.decisions_made) == 1 and self.decisions_made[0] == "kill_query":
            self.score += 10; d = "Went straight for the root cause."
        else: d = "The restart was a detour — next time aim for root cause first."

        g = "S" if self.score >= 90 else "A" if self.score >= 70 else "B" if self.score >= 50 else "C" if self.score >= 30 else "D"

        await self.notify("nova_speak", {
            "text": f"Incident resolved. Grade: {g}. Score: {self.score}. Lost ${int(self.revenue_loss):,} in {int(elapsed)} seconds. {t} {d}",
            "emotion": "impressed" if g in ("S", "A") else "neutral"
        })
        self.log_event("resolved", f"Grade: {g}, Score: {self.score}", 0)

        await self.notify("phase_change", {"phase": "resolved"})
        await self.notify("final_score", {"score": self.score, "grade": g,
            "loss": int(self.revenue_loss), "time": int(elapsed)})

        # Save game log to file
        import json as _json
        log_path = f"/home/om/openkeel/tools/nova_game_log_{int(time.time())}.json"
        with open(log_path, "w") as f:
            _json.dump({
                "grade": g, "score": self.score, "time": int(elapsed),
                "revenue_loss": int(self.revenue_loss),
                "decisions_count": len(self.game_log),
                "log": self.game_log,
            }, f, indent=2)
        print(f"Game log saved: {log_path}")

        # Send log to frontend too
        await self.notify("game_log", {"path": log_path, "entries": len(self.game_log)})

    async def revenue_ticker(self):
        while True:
            if self.phase in ("alarm", "investigating", "deciding", "recovering") and self.alarm_time:
                self.revenue_loss = (time.time() - self.alarm_time) * self.loss_rate
                await self.notify("revenue_update", {"loss": round(self.revenue_loss, 2)})
            await asyncio.sleep(0.5)

    async def commentary_engine(self):
        """Fill silence with commentary + fire dynamic decisions every ~20s."""
        self.last_spoke = time.time()
        used_comments = set()

        while True:
            await asyncio.sleep(1)
            silence = time.time() - self.last_spoke
            time_since_decision = time.time() - self.last_dynamic_decision

            if silence < 3.5:
                continue

            # Don't comment if waiting for human decision
            if self.waiting_for_human:
                if silence > 10:
                    await self.notify("nova_speak", {
                        "text": "Take your time, but the clock is ticking. What do you want to do?",
                        "emotion": "thinking"
                    })
                    self.last_spoke = time.time()
                continue

            # Don't fire anything if a scripted event is active or any decision is pending
            if self.scripted_busy or self.waiting_for_human or self.phase in ("deciding", "alarm", "resolved", "idle", "calm"):
                continue

            # Every ~20 seconds, fire a dynamic decision (only during investigating/recovering)
            if time_since_decision > 20 and self.phase in ("investigating", "recovering"):
                state = {
                    "phase": self.phase,
                    "loss": self.revenue_loss,
                    "agents_active": list(WORKERS.keys()),
                    "workers_done": list(self.workers_done),
                }
                decision = self.decision_engine.get_decision(state)
                # Double-check nothing grabbed the decision slot while we were generating
                if decision and not self.scripted_busy and not self.waiting_for_human:
                    self.last_dynamic_decision = time.time()
                    self.pending_dynamic_decision = decision

                    await self.notify("nova_speak", {
                        "text": decision["nova_text"],
                        "emotion": "thinking"
                    })

                    # Check AGAIN — a scripted event might have fired during the TTS notify
                    if self.scripted_busy or self.waiting_for_human:
                        self.pending_dynamic_decision = None
                        continue

                    await self.notify("show_stuck_options", {
                        "worker": decision.get("agent", "_dynamic"),
                        "options": [{"id": o.get("id", o["label"][:20]), "label": o["label"]} for o in decision["options"]]
                    })

                    if decision.get("agent") and decision.get("system"):
                        await self.notify("orb_dispatch", {
                            "worker": decision["agent"], "system": decision["system"],
                            "color": decision.get("color", "#fff"),
                            "name": AGENTS.get(decision["agent"], {}).get("name", "")
                        })

                    # Final check before claiming the decision slot
                    if self.scripted_busy or self.waiting_for_human:
                        self.pending_dynamic_decision = None
                        await self.notify("hide_stuck_options", {})
                        continue

                    self.waiting_for_human = ("_dynamic", decision)
                    start = time.time()
                    while self.waiting_for_human and self.waiting_for_human[0] == "_dynamic":
                        await asyncio.sleep(0.5)
                        # Bail if a scripted event took over
                        if self.scripted_busy:
                            self.waiting_for_human = None
                            self.pending_dynamic_decision = None
                            await self.notify("hide_stuck_options", {})
                            break
                        if time.time() - start > 25:
                            await self._resolve_dynamic_decision(decision["options"][0], decision, auto=True)
                            break
                    continue

            # Regular commentary
            if self.phase == "alarm":
                options = [
                    "Systems are cascading. This is spreading fast.",
                    "Revenue loss is climbing. We need to move.",
                    "Multiple systems degrading. Let me get the agents deployed.",
                ]
            elif self.phase == "investigating":
                done = len(self.workers_done)
                loss = int(self.revenue_loss)
                options = [
                    "All four agents are working. Terminals are active.",
                    f"We are losing about ten dollars a second. ${loss:,} so far.",
                    "Route usually reports back first. Network checks are fastest.",
                    "Watch the terminals. When you see a summary, that agent is about to report back.",
                    "Agent 4 is digging through the database. That is usually where the big problems hide.",
                    "Agent 3 is checking the servers. Memory looks bad on the web server.",
                    "Agent 2 is checking if our external services are down or if the problem is internal.",
                    "Narrowing it down. A pattern is forming.",
                    "The database investigation is the one I am most interested in.",
                    f"${loss:,} lost and climbing. Every second counts.",
                ]
            elif self.phase == "deciding":
                options = [
                    f"${int(self.revenue_loss):,} lost and counting. We need to act.",
                    "The longer we wait, the more it costs. What is your call?",
                    "Both options have tradeoffs. There is no perfect answer here.",
                ]
            elif self.phase == "recovering":
                options = [
                    "Recovery in progress. Watching the systems.",
                    "Terminals are showing improvement. Keep watching.",
                    "The agents are handling it. Give it a moment.",
                ]
            else:
                continue

            available = [c for c in options if c not in used_comments]
            if not available:
                used_comments.clear()
                available = options

            if available:
                comment = random.choice(available)
                used_comments.add(comment)
                await self.notify("nova_speak", {
                    "text": comment,
                    "emotion": "thinking" if self.phase == "investigating" else "concerned" if self.phase == "alarm" else "neutral"
                })
                self.last_spoke = time.time()

    async def _resolve_dynamic_decision(self, chosen_option, decision, auto=False):
        """Resolve a dynamic decision from the decision engine."""
        self.waiting_for_human = None
        self.pending_dynamic_decision = None

        score = chosen_option.get("score", 0)
        if auto:
            score = max(0, score - 10)
            prefix = "Timed out. Going with default. "
        else:
            prefix = f"Confirmed: {chosen_option.get('label', 'OK')}. "
        self.score += score

        # Log the decision
        self.log_event("decision", f"{chosen_option.get('label', '?')} ({decision.get('category', '?')})", score)

        await self.notify("nova_speak", {
            "text": prefix + chosen_option.get("result", "Done."),
            "emotion": "impressed" if score >= 12 and not auto else "neutral"
        })
        await self.notify("score_update", {"score": self.score, "bonus": score, "reason": decision.get("category", "decision")})
        await self.notify("hide_stuck_options", {})

        if decision.get("agent"):
            await self.notify("orb_return", {"worker": decision["agent"]})
