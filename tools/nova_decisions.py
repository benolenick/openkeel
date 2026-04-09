#!/usr/bin/env python3
"""
Nova Decision Engine — Generates contextual decisions from templates.
500+ unique combinations from parameterized templates.
Picks a relevant decision every ~20 seconds based on game state.
"""

import random
import time

# ── Agent and System pools for parameterization ──────────────────────
AGENTS = {
    "route": {"name": "Agent 1", "color": "#4488FF", "specialty": "network"},
    "scout": {"name": "Agent 2", "color": "#CC66FF", "specialty": "external"},
    "ping":  {"name": "Agent 3",  "color": "#FFD700", "specialty": "servers"},
    "vault": {"name": "Agent 4", "color": "#00FF88", "specialty": "data"},
}

SYSTEM_GROUPS = {
    "servers": ["web_server", "app_server"],
    "data": ["database", "cache", "message_queue"],
    "network": ["load_balancer", "cdn", "dns"],
    "external": ["payment_api", "auth_service"],
}

SYSTEM_NAMES = {
    "web_server": "Web Server", "app_server": "App Server",
    "database": "Database", "cache": "Cache",
    "load_balancer": "Load Balancer", "cdn": "CDN",
    "payment_api": "Payment API", "auth_service": "Auth Service",
    "message_queue": "Message Queue", "dns": "DNS",
}

# ── Decision Templates ──────────────────────────────────────────────
# Each template generates many unique decisions via parameterization.
# Categories: agent_mgmt, triage, system_fix, tactical, recovery, prevention, curveball

TEMPLATES = [
    # ═══ AGENT MANAGEMENT (agents × systems = many combos) ═══
    {
        "id": "agent_timeout",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} has been on {system} for 30 seconds with no new findings. It might be stuck in a loop.",
            [
                {"label": "Pull Back, Reassign", "keywords": ["pull", "back", "reassign", "redirect"], "score": 12,
                 "result": "Pulled {agent} back. Reassigning to a different approach."},
                {"label": "Give More Time", "keywords": ["time", "wait", "more", "patience", "let"], "score": 5,
                 "result": "Giving {agent} more time. Sometimes patience pays off."},
                {"label": "Send a Helper Agent", "keywords": ["help", "another", "second", "backup", "assist"], "score": 15,
                 "result": "Sent a second agent to assist. Two perspectives on {system} working in parallel."},
                {"label": "Change Strategy", "keywords": ["strategy", "different", "approach", "try", "new"], "score": 10,
                 "result": "Told {agent} to try a different diagnostic approach on {system}."},
            ]),
    },
    {
        "id": "agent_wrong_system",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_wrong_system(state,
            "{agent} wandered to {wrong_system} but it should be looking at {right_system}. It's checking {wrong_thing}.",
            [
                {"label": "Redirect to {right_system}", "keywords": ["redirect", "focus", "right", "correct", "back"], "score": 15,
                 "result": "Redirected {agent} to {right_system}. Back on track."},
                {"label": "Let It Explore", "keywords": ["explore", "let", "continue", "maybe", "check"], "score": 0,
                 "result": "{agent} found nothing useful on {wrong_system}. Wasted 8 seconds."},
            ]),
    },
    {
        "id": "agent_permission",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} needs elevated access on {system}. It can't read the logs without root permissions.",
            [
                {"label": "Grant Sudo", "keywords": ["sudo", "root", "grant", "elevate", "permission"], "score": 5,
                 "result": "Elevated access granted. {agent} can now read the restricted logs."},
                {"label": "Use Alternative Path", "keywords": ["alternative", "other", "different", "debug", "api", "endpoint"], "score": 15,
                 "result": "Good thinking. {agent} found a debug endpoint that doesn't need root."},
                {"label": "Check Metrics Instead", "keywords": ["metric", "prometheus", "grafana", "dashboard", "monitor"], "score": 12,
                 "result": "Pulled metrics from the monitoring dashboard instead. Got what we needed without log access."},
                {"label": "Skip This System", "keywords": ["skip", "move", "next", "don't need"], "score": 3,
                 "result": "Skipped {system}. Might miss something but saved time."},
            ]),
    },
    {
        "id": "agent_overwhelmed",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} is drowning in data on {system}. Too many log entries to parse manually.",
            [
                {"label": "Filter by Severity", "keywords": ["filter", "severity", "error", "critical", "important"], "score": 15,
                 "result": "Filtered to errors only. {agent} found the important entries immediately."},
                {"label": "Sort by Time", "keywords": ["sort", "time", "recent", "latest", "newest", "chronological"], "score": 10,
                 "result": "Sorted chronologically. The spike started at 14:23. Narrowing down."},
                {"label": "Grep for Keywords", "keywords": ["grep", "search", "keyword", "pattern", "find"], "score": 12,
                 "result": "Searching for error, timeout, failed. Found 847 matches since 14:23."},
                {"label": "Sample Random Entries", "keywords": ["sample", "random", "spot", "check"], "score": 5,
                 "result": "Randomly sampled 20 entries. Got a sense of the pattern but missed some details."},
            ]),
    },
    {
        "id": "agent_conflicting_data",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _two_agents(state,
            "{agent1} says {system1} is the problem, but {agent2} thinks it's {system2}. They're reporting conflicting data.",
            [
                {"label": "Trust {agent1}", "keywords": ["{a1_kw}", "trust", "first", "agree"], "score": lambda ctx: 15 if ctx.get("a1_right") else 0,
                 "result": "{verdict1}"},
                {"label": "Trust {agent2}", "keywords": ["{a2_kw}", "second", "other"], "score": lambda ctx: 15 if not ctx.get("a1_right") else 0,
                 "result": "{verdict2}"},
                {"label": "Send Both to Verify", "keywords": ["both", "verify", "double", "check", "confirm"], "score": 10,
                 "result": "Both agents cross-checking. Takes longer but we'll know for sure."},
            ]),
    },
    {
        "id": "agent_needs_web",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} found an error code on {system} it doesn't recognize. Wants to search the docs.",
            [
                {"label": "Search the Docs", "keywords": ["search", "docs", "look", "web", "documentation", "google"], "score": 10,
                 "result": "{agent} found the answer in the Postgres docs. Error means connection pool exhaustion."},
                {"label": "Skip It, Move On", "keywords": ["skip", "move", "ignore", "next", "doesn't matter"], "score": 5,
                 "result": "Skipped. Might have been useful context but we're saving time."},
            ]),
    },
    {
        "id": "agent_crashed",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} lost connection to {system}. The agent process appears to have crashed.",
            [
                {"label": "Redeploy {agent}", "keywords": ["redeploy", "restart", "again", "retry", "send"], "score": 10,
                 "result": "Redeployed {agent}. It's reconnecting to {system} now."},
                {"label": "Redistribute Work", "keywords": ["redistribute", "other", "split", "different agent", "reassign"], "score": 15,
                 "result": "Splitting {agent}'s remaining work across the other agents."},
            ]),
    },

    # ═══ TRIAGE DECISIONS ═══
    {
        "id": "which_system_first",
        "category": "triage",
        "phases": ["investigating", "recovering"],
        "gen": lambda state: _triage_two_systems(state,
            "Two systems just went critical: {system1} and {system2}. We only have one free agent. Which one first?",
            [
                {"label": "{system1} First", "keywords": ["{s1_kw}"], "score": lambda ctx: 15 if ctx.get("s1_priority") else 5,
                 "result": "Sending agent to {system1}. {reason1}"},
                {"label": "{system2} First", "keywords": ["{s2_kw}"], "score": lambda ctx: 15 if not ctx.get("s1_priority") else 5,
                 "result": "Sending agent to {system2}. {reason2}"},
            ]),
    },
    {
        "id": "revenue_vs_stability",
        "category": "triage",
        "phases": ["investigating", "deciding"],
        "gen": lambda state: {
            "nova_text": f"We can bring payment processing back online immediately with a risky shortcut, or wait for a stable fix. We're losing ${int(state.get('loss', 0)):,} so far.",
            "options": [
                {"id": "fast_risky", "label": "Fast Risky Fix", "keywords": ["fast", "quick", "risky", "now", "payment"], "score": 5,
                 "result": "Payments are back but we might see errors. Risky."},
                {"id": "stable_wait", "label": "Wait for Stable Fix", "keywords": ["wait", "stable", "safe", "proper", "correct"], "score": 15,
                 "result": "Good call. Patience here saves us from a second incident."},
            ],
        },
    },

    # ═══ SYSTEM-SPECIFIC FIXES ═══
    {
        "id": "db_pool_size",
        "category": "system_fix",
        "phases": ["recovering"],
        "gen": lambda state: {
            "nova_text": "Database is recovering. How should we restore the connection pool?",
            "options": [
                {"id": "restore_full", "label": "Full Restore (100)", "keywords": ["100", "full", "restore", "normal", "all"], "score": 3,
                 "result": "Pool at 100. Risk of another spike but fastest recovery."},
                {"id": "keep_throttled", "label": "Stay at 50", "keywords": ["50", "throttle", "keep", "safe", "slow", "careful"], "score": 15,
                 "result": "Smart. Pool at 50 until backlog clears. No stampede risk."},
                {"id": "gradual", "label": "Gradual: 30, 60, 100", "keywords": ["gradual", "step", "slowly", "ramp", "increment"], "score": 12,
                 "result": "Ramping up gradually. 30 then 60 then 100 over 2 minutes. Controlled and safe."},
                {"id": "dynamic", "label": "Auto-Scale Based on Load", "keywords": ["auto", "scale", "dynamic", "smart", "adaptive"], "score": 10,
                 "result": "Set pool to auto-scale based on CPU and queue depth. Adaptive approach."},
            ],
        },
    },
    {
        "id": "cache_strategy",
        "category": "system_fix",
        "phases": ["recovering"],
        "gen": lambda state: {
            "nova_text": "Cache is stale after the incident. Flush everything and rebuild, or let it warm up naturally?",
            "options": [
                {"id": "flush_all", "label": "Flush and Rebuild", "keywords": ["flush", "clear", "rebuild", "reset", "wipe"], "score": 10,
                 "result": "Cache flushed. Temporary performance hit but clean slate."},
                {"id": "warm_natural", "label": "Let It Warm Naturally", "keywords": ["warm", "natural", "gradual", "leave", "organic"], "score": 12,
                 "result": "Cache warming on its own. Slower but no performance cliff."},
            ],
        },
    },
    {
        "id": "queue_drain_rate",
        "category": "system_fix",
        "phases": ["recovering"],
        "gen": lambda state: {
            "nova_text": "Message queue has 4200 messages backed up. How fast should we drain it?",
            "options": [
                {"id": "slow_safe", "label": "50/s — Safe", "keywords": ["50", "slow", "safe", "careful", "steady"], "score": 15,
                 "result": "Draining at 50/s. Steady and controlled. No risk to DB."},
                {"id": "medium", "label": "100/s — Balanced", "keywords": ["100", "medium", "balanced", "moderate"], "score": 10,
                 "result": "100/s is a good middle ground. Clearing in about 40 seconds."},
                {"id": "fast_risky", "label": "200/s — Fast", "keywords": ["200", "fast", "quick", "clear", "rapid"], "score": 3,
                 "result": "Draining at 200/s. Fast but the DB groaned under the load."},
                {"id": "priority", "label": "Priority Queue — Orders First", "keywords": ["priority", "order", "important", "critical first"], "score": 12,
                 "result": "Smart. Processing order confirmations first, then emails. Customers see recovery fastest."},
            ],
        },
    },
    {
        "id": "node_restoration",
        "category": "system_fix",
        "phases": ["recovering"],
        "gen": lambda state: {
            "nova_text": "Load balancer has 3 nodes still marked unhealthy. Bring them all back at once, or one at a time with health checks?",
            "options": [
                {"id": "all_at_once", "label": "All at Once", "keywords": ["all", "once", "together", "fast", "everything"], "score": 5,
                 "result": "All nodes back. Traffic spike incoming — hope they hold."},
                {"id": "one_by_one", "label": "One at a Time", "keywords": ["one", "time", "gradual", "careful", "slowly", "each"], "score": 15,
                 "result": "Bringing them back one by one. Each one health-checked before the next."},
            ],
        },
    },

    # ═══ CURVEBALL EVENTS ═══
    {
        "id": "new_alert",
        "category": "curveball",
        "phases": ["investigating", "recovering"],
        "gen": lambda state: _random_new_alert(state),
    },
    {
        "id": "customer_complaint",
        "category": "curveball",
        "phases": ["investigating", "deciding"],
        "gen": lambda state: {
            "nova_text": "Customer support is flooding in. CEO is asking for a status update. Do we stop to write one, or keep working?",
            "options": [
                {"id": "keep_working", "label": "Keep Working", "keywords": ["keep", "work", "focus", "ignore", "later", "not now"], "score": 15,
                 "result": "Right call. Fix first, explain later. CEO can wait 2 minutes."},
                {"id": "send_update", "label": "Send Quick Update", "keywords": ["update", "send", "ceo", "status", "tell"], "score": 8,
                 "result": "Sent a quick status. Good communication but cost us 10 seconds."},
            ],
        },
    },
    {
        "id": "second_incident",
        "category": "curveball",
        "phases": ["investigating"],
        "gen": lambda state: _random_second_incident(state),
    },
    {
        "id": "agent_found_unrelated",
        "category": "curveball",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} found a separate issue on {system} — an expiring SSL cert in 3 days. Not related to the current incident.",
            [
                {"label": "Log It, Stay Focused", "keywords": ["log", "later", "focus", "note", "ignore", "not now"], "score": 15,
                 "result": "Noted for later. Staying focused on the current incident."},
                {"label": "Fix It Now", "keywords": ["fix", "now", "while we're here", "might as well"], "score": -5,
                 "result": "That pulled {agent} off task for 15 seconds. We're in a crisis — not the time."},
            ]),
    },
    {
        "id": "monitoring_gap",
        "category": "curveball",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} discovered there's no monitoring on {system}. We've been flying blind on this component.",
            [
                {"label": "Note It, Continue", "keywords": ["note", "continue", "later", "after", "focus"], "score": 12,
                 "result": "Good find. We'll add monitoring after the incident is resolved."},
                {"label": "Quick Alert Setup", "keywords": ["quick", "alert", "setup", "add", "monitor"], "score": 8,
                 "result": "Added a basic health check. Better than nothing but took 5 seconds."},
            ]),
    },

    # ═══ PREVENTION (post-recovery) ═══
    {
        "id": "add_query_timeout",
        "category": "prevention",
        "phases": ["resolved"],
        "gen": lambda state: {
            "nova_text": "Should we add a 60-second query timeout to prevent runaway queries in the future?",
            "options": [
                {"id": "yes_timeout", "label": "Yes, Add Timeout", "keywords": ["yes", "add", "timeout", "prevent", "absolutely"], "score": 15,
                 "result": "Query timeout set to 60s. This can never happen again."},
                {"id": "no_timeout", "label": "Skip It", "keywords": ["skip", "no", "not now", "later"], "score": -5,
                 "result": "No timeout added. We'll be back here next time someone runs a bad query."},
            ],
        },
    },
    {
        "id": "add_memory_alert",
        "category": "prevention",
        "phases": ["resolved"],
        "gen": lambda state: {
            "nova_text": "Add a memory usage alert at 80%? Would have caught this 20 minutes earlier.",
            "options": [
                {"id": "yes_alert", "label": "Yes, Alert at 80%", "keywords": ["yes", "alert", "add", "80", "monitor"], "score": 15,
                 "result": "Alert configured. Next time memory hits 80% we'll know immediately."},
                {"id": "skip", "label": "Skip", "keywords": ["skip", "no"], "score": -5,
                 "result": "No alert added."},
            ],
        },
    },
    {
        "id": "add_connection_pool_alert",
        "category": "prevention",
        "phases": ["resolved"],
        "gen": lambda state: {
            "nova_text": "Add an alert when connection pool hits 80 out of 100? Early warning before it maxes out.",
            "options": [
                {"id": "yes", "label": "Yes, Alert at 80%", "keywords": ["yes", "add", "alert", "pool"], "score": 15,
                 "result": "Connection pool alert at 80%. Early warning system in place."},
                {"id": "no", "label": "Skip", "keywords": ["skip", "no"], "score": -5,
                 "result": "Skipped."},
            ],
        },
    },
    {
        "id": "incident_report",
        "category": "prevention",
        "phases": ["resolved"],
        "gen": lambda state: {
            "nova_text": "Send an incident report to the team? Documents what happened, root cause, and what we did.",
            "options": [
                {"id": "send", "label": "Send Report", "keywords": ["send", "yes", "report", "document", "team"], "score": 10,
                 "result": "Incident report sent. Team will learn from this."},
                {"id": "skip", "label": "Skip", "keywords": ["skip", "no", "later"], "score": 0,
                 "result": "No report sent. Knowledge lost."},
            ],
        },
    },
    {
        "id": "runbook_update",
        "category": "prevention",
        "phases": ["resolved"],
        "gen": lambda state: {
            "nova_text": "Update the incident runbook with this scenario? Next time someone can follow the playbook.",
            "options": [
                {"id": "update", "label": "Update Runbook", "keywords": ["update", "runbook", "yes", "playbook", "document"], "score": 10,
                 "result": "Runbook updated with the thundering herd mitigation steps."},
                {"id": "skip", "label": "Skip", "keywords": ["skip", "no"], "score": 0,
                 "result": "Runbook not updated."},
            ],
        },
    },

    # ═══ MORE AGENT MANAGEMENT ═══
    {
        "id": "agent_slow_progress",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} is making slow progress on {system}. The responses are coming back but it is taking twice as long as expected.",
            [
                {"label": "Send a Faster Query Strategy", "keywords": ["fast", "strategy", "optimize", "quick", "speed"], "score": 12,
                 "result": "Gave {agent} a more targeted approach. Speed improved."},
                {"label": "Let It Work", "keywords": ["let", "work", "fine", "ok", "patience"], "score": 5,
                 "result": "{agent} finished eventually. Slower but thorough."},
            ]),
    },
    {
        "id": "agent_found_anomaly",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} found an anomaly on {system} that does not match any known error pattern. Could be a new type of failure.",
            [
                {"label": "Flag It, Keep Going", "keywords": ["flag", "note", "keep", "continue", "log"], "score": 12,
                 "result": "Anomaly flagged for post-incident review. Staying focused."},
                {"label": "Deep Dive Now", "keywords": ["dive", "investigate", "now", "look", "explore"], "score": 3,
                 "result": "{agent} spent 12 seconds on it. Turned out to be cosmetic. Wasted time."},
            ]),
    },
    {
        "id": "agent_conflicting_logs",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} is seeing conflicting timestamps in {system} logs. The clock might be skewed, which means the log order could be wrong.",
            [
                {"label": "Use NTP to Verify Clock", "keywords": ["ntp", "clock", "time", "verify", "sync"], "score": 15,
                 "result": "Clock was 3 seconds off. {agent} adjusted and the log timeline makes sense now."},
                {"label": "Ignore, Trust the Logs", "keywords": ["ignore", "trust", "fine", "skip", "doesn't matter"], "score": 5,
                 "result": "Proceeded with potentially wrong timestamps. Risky but faster."},
            ]),
    },
    {
        "id": "agent_resource_limit",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} is hitting memory limits on {system}. The log file it is trying to parse is too large to load at once.",
            [
                {"label": "Stream Instead of Load", "keywords": ["stream", "chunk", "piece", "part", "tail", "line"], "score": 15,
                 "result": "{agent} switched to streaming. Processing the log line by line now."},
                {"label": "Skip to End of Log", "keywords": ["skip", "end", "tail", "last", "recent"], "score": 10,
                 "result": "Jumped to the last 1000 lines. Found what we needed quickly."},
            ]),
    },
    {
        "id": "agent_retry_strategy",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} got a connection refused from {system}. It wants to retry but how aggressively?",
            [
                {"label": "Exponential Backoff", "keywords": ["backoff", "wait", "gradual", "gentle", "exponential"], "score": 15,
                 "result": "Smart. Backed off 1s, 2s, 4s. Connected on third try without overloading {system}."},
                {"label": "Retry Immediately", "keywords": ["retry", "now", "again", "immediate", "fast", "hammer"], "score": 3,
                 "result": "Hammered {system} with retries. Got in but added load to an already stressed system."},
            ]),
    },
    {
        "id": "agent_parallel_check",
        "category": "agent_mgmt",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} wants to run 5 diagnostic checks on {system} simultaneously. That will be fast but might add load to an already stressed system.",
            [
                {"label": "Run Sequentially", "keywords": ["sequential", "one", "time", "careful", "gentle", "slow"], "score": 12,
                 "result": "Running checks one at a time. Slower but no extra stress on {system}."},
                {"label": "Run in Parallel", "keywords": ["parallel", "fast", "all", "same time", "go"], "score": 5,
                 "result": "All 5 checks at once. Fast results but {system} groaned under the load."},
            ]),
    },

    # ═══ MORE TACTICAL ═══
    {
        "id": "escalation_decision",
        "category": "tactical",
        "phases": ["investigating", "deciding"],
        "gen": lambda state: {
            "nova_text": f"We have been at this for {int(time.time() - state.get('alarm_start', time.time()))} seconds. Should we escalate to the on-call engineer, or can we handle this ourselves?",
            "options": [
                {"id": "handle_it", "label": "We Got This", "keywords": ["we", "got", "handle", "ourselves", "no", "fine"], "score": 15,
                 "result": "Staying in control. No escalation needed."},
                {"id": "escalate", "label": "Escalate", "keywords": ["escalate", "call", "help", "engineer", "backup"], "score": 8,
                 "result": "Escalation sent. On-call engineer notified. Good to have backup."},
            ],
        },
    },
    {
        "id": "rollback_option",
        "category": "tactical",
        "phases": ["investigating"],
        "gen": lambda state: {
            "nova_text": "There was a deployment 2 hours ago. We could roll it back as a precaution, even though it might not be related.",
            "options": [
                {"id": "rollback", "label": "Roll Back Deploy", "keywords": ["roll", "back", "revert", "undo", "deploy"], "score": 5,
                 "result": "Rolled back. The deployment was not the cause, but at least we eliminated it."},
                {"id": "keep", "label": "Keep Current Version", "keywords": ["keep", "no", "current", "stay", "fine"], "score": 12,
                 "result": "Good instinct. The deploy was unrelated. No rollback needed."},
            ],
        },
    },
    {
        "id": "communication_strategy",
        "category": "tactical",
        "phases": ["investigating"],
        "gen": lambda state: {
            "nova_text": "Status page is still showing green even though we are down. Update it manually, or let it auto-detect?",
            "options": [
                {"id": "manual_update", "label": "Update Status Page", "keywords": ["update", "manual", "status", "page", "tell", "honest"], "score": 12,
                 "result": "Status page updated. Customers see we are aware. Trust maintained."},
                {"id": "auto_detect", "label": "Let Auto-Detection Handle It", "keywords": ["auto", "detect", "wait", "automatic", "let"], "score": 5,
                 "result": "Auto-detection kicked in 30 seconds later. Customers saw green during a real outage."},
            ],
        },
    },
    {
        "id": "resource_allocation",
        "category": "tactical",
        "phases": ["investigating"],
        "gen": lambda state: _two_agents(state,
            "{agent1} and {agent2} are both free. We have two systems to check: {system1} and {system2}. Which agent goes where?",
            [
                {"label": "{agent1} to {system1}", "keywords": ["{a1_kw}"], "score": 10,
                 "result": "Assigned. Both agents working their targets."},
                {"label": "Swap: {agent1} to {system2}", "keywords": ["swap", "switch", "other"], "score": 10,
                 "result": "Swapped assignments. Different perspective might help."},
            ]),
    },

    # ═══ MORE CURVEBALLS ═══
    {
        "id": "false_positive",
        "category": "curveball",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} is reporting a critical error on {system}, but the metrics look fine. Could be a false positive from stale monitoring data.",
            [
                {"label": "Verify Manually", "keywords": ["verify", "manual", "check", "confirm", "double"], "score": 15,
                 "result": "Verified. It was a false positive from cached data. Good catch not acting on bad info."},
                {"label": "Trust the Alert", "keywords": ["trust", "alert", "act", "real", "respond"], "score": 3,
                 "result": "Sent an agent to fix a non-problem. Wasted resources."},
            ]),
    },
    {
        "id": "config_drift",
        "category": "curveball",
        "phases": ["investigating"],
        "gen": lambda state: _agent_on_system(state,
            "{agent} noticed {system} config does not match the documented baseline. Someone changed it manually without updating docs.",
            [
                {"label": "Note It for Later", "keywords": ["note", "later", "after", "document", "log"], "score": 12,
                 "result": "Logged the config drift. Will fix documentation after the incident."},
                {"label": "Revert Config Now", "keywords": ["revert", "fix", "now", "change", "reset"], "score": 3,
                 "result": "Reverted config mid-incident. Risky move that could have made things worse."},
            ]),
    },
    {
        "id": "network_blip",
        "category": "curveball",
        "phases": ["investigating"],
        "gen": lambda state: {
            "nova_text": "A brief network blip just caused two agents to lose connection for 3 seconds. They reconnected automatically. Check on them or trust the reconnection?",
            "options": [
                {"id": "trust", "label": "Trust Auto-Reconnect", "keywords": ["trust", "fine", "ok", "auto", "reconnect"], "score": 12,
                 "result": "Agents resumed right where they left off. Auto-reconnect worked perfectly."},
                {"id": "verify", "label": "Verify Both Agents", "keywords": ["verify", "check", "confirm", "status"], "score": 8,
                 "result": "Both agents verified healthy. Cautious but cost a few seconds."},
            ],
        },
    },
]


# ── Helper generators ───────────────────────────────────────────────
def _agent_on_system(state, text_tmpl, options_tmpl):
    """Pick a random agent and a system it could be on."""
    agents_active = state.get("agents_active", list(AGENTS.keys()))
    agent_id = random.choice(agents_active) if agents_active else random.choice(list(AGENTS.keys()))
    agent = AGENTS[agent_id]

    # Pick a system from this agent's specialty group
    group = SYSTEM_GROUPS.get(agent["specialty"], ["web_server"])
    system_id = random.choice(group)
    system_name = SYSTEM_NAMES[system_id]

    text = text_tmpl.replace("{agent}", agent["name"]).replace("{system}", system_name)
    options = []
    for opt in options_tmpl:
        o = dict(opt)
        o["label"] = o["label"].replace("{agent}", agent["name"]).replace("{system}", system_name)
        o["result"] = o["result"].replace("{agent}", agent["name"]).replace("{system}", system_name)
        o["id"] = o.get("id", o["label"][:20].lower().replace(" ", "_"))
        options.append(o)

    return {"nova_text": text, "options": options, "agent": agent_id, "system": system_id, "color": agent["color"]}


def _agent_wrong_system(state, text_tmpl, options_tmpl):
    agent_id = random.choice(list(AGENTS.keys()))
    agent = AGENTS[agent_id]
    right_group = SYSTEM_GROUPS.get(agent["specialty"], ["web_server"])
    wrong_groups = [g for k, g in SYSTEM_GROUPS.items() if k != agent["specialty"]]
    wrong_group = random.choice(wrong_groups) if wrong_groups else right_group

    right_sys = random.choice(right_group)
    wrong_sys = random.choice(wrong_group)

    wrong_things = ["checking auth tokens", "scanning DNS records", "reading CDN logs",
                    "testing payment webhooks", "monitoring queue depths", "analyzing cache hit rates"]

    text = text_tmpl.replace("{agent}", agent["name"]).replace("{wrong_system}", SYSTEM_NAMES[wrong_sys])\
        .replace("{right_system}", SYSTEM_NAMES[right_sys]).replace("{wrong_thing}", random.choice(wrong_things))

    options = []
    for opt in options_tmpl:
        o = dict(opt)
        o["label"] = o["label"].replace("{right_system}", SYSTEM_NAMES[right_sys])
        o["result"] = o["result"].replace("{agent}", agent["name"])\
            .replace("{wrong_system}", SYSTEM_NAMES[wrong_sys]).replace("{right_system}", SYSTEM_NAMES[right_sys])
        o["id"] = o.get("id", o["label"][:20].lower().replace(" ", "_"))
        options.append(o)

    return {"nova_text": text, "options": options, "agent": agent_id, "system": wrong_sys, "color": agent["color"]}


def _two_agents(state, text_tmpl, options_tmpl):
    agent_ids = random.sample(list(AGENTS.keys()), 2)
    a1, a2 = AGENTS[agent_ids[0]], AGENTS[agent_ids[1]]
    s1 = random.choice(SYSTEM_GROUPS.get(a1["specialty"], ["web_server"]))
    s2 = random.choice(SYSTEM_GROUPS.get(a2["specialty"], ["database"]))
    a1_right = random.choice([True, False])

    text = text_tmpl.replace("{agent1}", a1["name"]).replace("{agent2}", a2["name"])\
        .replace("{system1}", SYSTEM_NAMES[s1]).replace("{system2}", SYSTEM_NAMES[s2])

    ctx = {"a1_right": a1_right}
    verdicts = {
        True: (f"{a1['name']} was right. {SYSTEM_NAMES[s1]} is the real problem.",
               f"Turns out {a2['name']} had the right lead. Should have listened."),
        False: (f"{a1['name']} was wrong. Wasted time on {SYSTEM_NAMES[s1]}.",
                f"{a2['name']} was right all along. {SYSTEM_NAMES[s2]} is the issue."),
    }

    options = []
    for opt in options_tmpl:
        o = dict(opt)
        o["label"] = o["label"].replace("{agent1}", a1["name"]).replace("{agent2}", a2["name"])
        o["result"] = o.get("result", "").replace("{verdict1}", verdicts[True][0] if a1_right else verdicts[False][0])\
            .replace("{verdict2}", verdicts[True][1] if a1_right else verdicts[False][1])
        o["keywords"] = [k.replace("{a1_kw}", a1["name"].lower()).replace("{a2_kw}", a2["name"].lower()) for k in o["keywords"]]
        score = o["score"]
        o["score"] = score(ctx) if callable(score) else score
        o["id"] = o.get("id", o["label"][:20].lower().replace(" ", "_"))
        options.append(o)

    return {"nova_text": text, "options": options}


def _triage_two_systems(state, text_tmpl, options_tmpl):
    all_sys = list(SYSTEM_NAMES.keys())
    s1, s2 = random.sample(all_sys, 2)
    s1_priority = random.choice([True, False])

    reasons = {
        True: (f"{SYSTEM_NAMES[s1]} is upstream — fixing it might fix {SYSTEM_NAMES[s2]} too.",
               f"{SYSTEM_NAMES[s2]} is important but it's a symptom, not the cause."),
        False: (f"{SYSTEM_NAMES[s1]} is a symptom. We should focus on the root cause.",
                f"{SYSTEM_NAMES[s2]} is the root — fixing it cascades to everything else."),
    }

    text = text_tmpl.replace("{system1}", SYSTEM_NAMES[s1]).replace("{system2}", SYSTEM_NAMES[s2])
    ctx = {"s1_priority": s1_priority}

    options = []
    for opt in options_tmpl:
        o = dict(opt)
        o["label"] = o["label"].replace("{system1}", SYSTEM_NAMES[s1]).replace("{system2}", SYSTEM_NAMES[s2])
        o["result"] = o.get("result", "").replace("{system1}", SYSTEM_NAMES[s1]).replace("{system2}", SYSTEM_NAMES[s2])\
            .replace("{reason1}", reasons[s1_priority][0]).replace("{reason2}", reasons[s1_priority][1])
        o["keywords"] = [k.replace("{s1_kw}", SYSTEM_NAMES[s1].lower().split()[0]).replace("{s2_kw}", SYSTEM_NAMES[s2].lower().split()[0]) for k in o["keywords"]]
        score = o["score"]
        o["score"] = score(ctx) if callable(score) else score
        o["id"] = o.get("id", o["label"][:20].lower().replace(" ", "_"))
        options.append(o)

    return {"nova_text": text, "options": options}


def _random_new_alert(state):
    alerts = [
        ("SSL certificate on {sys} expires in 48 hours. Unrelated to current incident.",
         "web_server", "Renew After Incident", "Renew Now"),
        ("Disk usage on {sys} hit 90%. Not causing the current issue but it's getting close.",
         "database", "Note It", "Expand Disk Now"),
        ("{sys} is reporting intermittent DNS timeouts. Might be related, might not.",
         "cdn", "Investigate After", "Send Agent Now"),
        ("Automated backup on {sys} just started. It'll use extra IO during recovery.",
         "database", "Pause Backup", "Let It Run"),
        ("A new deployment was queued for {sys} before the incident. It's still pending.",
         "app_server", "Cancel Deployment", "Let It Deploy"),
    ]
    alert = random.choice(alerts)
    sys_name = SYSTEM_NAMES[alert[1]]
    text = alert[0].replace("{sys}", sys_name)

    return {
        "nova_text": text,
        "options": [
            {"id": "defer", "label": alert[2], "keywords": ["note", "after", "later", "pause", "cancel", "defer", "focus"],
             "score": 12, "result": "Good — staying focused on the current incident."},
            {"id": "act_now", "label": alert[3], "keywords": ["now", "fix", "send", "renew", "expand", "deploy", "let"],
             "score": 3, "result": "Handled, but it pulled attention from the main crisis."},
        ],
    }


def _random_second_incident(state):
    incidents = [
        "A second alert just came in — auth service is showing elevated error rates. Could be related or a coincidence.",
        "Monitoring picked up a CPU spike on a system that was previously green. Cascade spreading?",
        "A team member just force-pushed to production. Unrelated but the timing is terrible.",
        "Load balancer is seeing a traffic spike from a new region. Organic growth or attack?",
    ]
    return {
        "nova_text": random.choice(incidents),
        "options": [
            {"id": "stay_focused", "label": "Stay Focused on Main Incident", "keywords": ["stay", "focus", "main", "ignore", "later", "primary"],
             "score": 15, "result": "Right call. One crisis at a time. We'll investigate after."},
            {"id": "split_attention", "label": "Investigate Both", "keywords": ["both", "investigate", "check", "look", "split"],
             "score": 3, "result": "Splitting attention. Both investigations are now slower."},
        ],
    }


# ── Decision Engine ─────────────────────────────────────────────────
class DecisionEngine:
    """Generates and tracks contextual decisions throughout the game."""

    def __init__(self):
        self.used_template_ids = set()
        self.decisions_served = 0
        self.last_decision_time = 0

    def get_decision(self, state):
        """Get a contextual decision based on current game state. Returns None if nothing fits."""
        phase = state.get("phase", "idle")

        # Filter templates by current phase
        eligible = [t for t in TEMPLATES if phase in t["phases"] and t["id"] not in self.used_template_ids]

        if not eligible:
            # Reset used templates to allow repeats with different params
            self.used_template_ids.clear()
            eligible = [t for t in TEMPLATES if phase in t["phases"]]

        if not eligible:
            return None

        # Pick a random template
        template = random.choice(eligible)
        self.used_template_ids.add(template["id"])

        try:
            decision = template["gen"](state)
            if decision:
                self.decisions_served += 1
                self.last_decision_time = time.time()
                decision["template_id"] = template["id"]
                decision["category"] = template["category"]
                return decision
        except Exception as e:
            print(f"Decision gen error: {e}")

        return None

    def get_total_possible(self):
        """Estimate total unique decisions possible."""
        # Templates × parameter combinations
        agent_sys_combos = len(AGENTS) * 10  # agents × systems
        return len(TEMPLATES) * agent_sys_combos // 3  # rough estimate

    def reset(self):
        self.used_template_ids.clear()
        self.decisions_served = 0
        self.last_decision_time = 0
