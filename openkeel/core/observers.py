"""Observer Orchestrator — runs the Cartographer and Pilgrim as background workers.

The Cartographer processes new log entries → updates the map.
The Pilgrim walks the map on a slower cycle → produces findings.
The Consensus Gate decides when to inject findings into the executor.

This module provides the threading/async layer that connects everything.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from openkeel.core.cartographer import (
    ProblemMap,
    create_map,
    load_map,
    save_map,
    apply_delta,
    build_cartographer_prompt,
    CARTOGRAPHER_SYSTEM_PROMPT,
)
from openkeel.core.pilgrim import (
    PilgrimReport,
    walk_map,
    build_pilgrim_prompt,
    apply_pilgrim_findings,
    report_to_dict,
    PILGRIM_SYSTEM_PROMPT,
)
from openkeel.core.retrieval_bridge import (
    enrich_new_nodes,
    fill_pilgrim_gaps,
    proactive_retrieve,
    format_enrichment_nudge,
)
from openkeel.core.consensus import (
    ConsensusConfig,
    ConsensusState,
    process_cartographer_alerts,
    process_pilgrim_report,
    format_injection,
    format_nudge,
    consensus_status_line,
)
from openkeel.core.distilled_log import DistilledLog
from openkeel.integrations.local_llm import (
    LLMEndpoint,
    complete,
    parse_json_response,
    check_health,
    CARTOGRAPHER_ENDPOINT,
    PILGRIM_ENDPOINT,
    OVERWATCH_ENDPOINT,
)
from openkeel.core.oracle import (
    OverwatchOracle,
    OracleConfig,
    OracleVerdict,
    build_oracle_context,
)


# ---------------------------------------------------------------------------
# Observer worker
# ---------------------------------------------------------------------------

@dataclass
class ObserverConfig:
    """Configuration for the observer system."""
    cartographer_endpoint: LLMEndpoint = field(default_factory=lambda: CARTOGRAPHER_ENDPOINT)
    pilgrim_endpoint: LLMEndpoint = field(default_factory=lambda: PILGRIM_ENDPOINT)
    consensus: ConsensusConfig = field(default_factory=ConsensusConfig)
    cartographer_poll_seconds: float = 30.0    # check for new log entries (was 15 — too aggressive for mining risers)
    pilgrim_walk_seconds: float = 120.0        # full map walk interval
    pilgrim_walk_on_contradiction: bool = True  # immediate walk when contradiction found
    use_llm_cartographer: bool = True           # False = only structural delta (no LLM)
    use_llm_pilgrim: bool = True                # False = only graph algorithms (no LLM)
    # Overwatch Oracle (CPU-only, sees everything)
    oracle: OracleConfig = field(default_factory=OracleConfig)
    enable_oracle: bool = True
    # GPU safety: max consecutive LLM failures before cooldown
    max_consecutive_llm_fails: int = 3
    llm_cooldown_seconds: float = 300.0        # 5 min cooldown after max failures
    gpu_util_max_pct: int = 90                 # skip LLM call if GPU > this %


class ObserverOrchestrator:
    """Runs both observers in background threads."""

    def __init__(
        self,
        mission_dir: Path,
        config: ObserverConfig | None = None,
        on_nudge: Callable[[str], None] | None = None,
        on_interrupt: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ):
        self._mission_dir = mission_dir
        self._config = config or ObserverConfig()
        self._on_nudge = on_nudge or (lambda s: None)
        self._on_interrupt = on_interrupt or (lambda s: None)
        self._on_status = on_status or (lambda s: None)

        self._log = DistilledLog(mission_dir)
        self._pmap = load_map(mission_dir) or create_map(mission_dir.name)
        self._consensus = ConsensusState()
        self._last_log_count = 0  # Start from beginning on fresh daemon launch

        self._running = False
        self._cart_thread: threading.Thread | None = None
        self._pilgrim_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # Pilgrim trigger event (for immediate walks on contradiction)
        self._pilgrim_trigger = threading.Event()


        # Memory integration for Pilgrim
        self._hyphae_url = "http://127.0.0.1:8100"
        self._memoria_url = "http://127.0.0.1:8000"
        self._last_technique_change_time = __import__("time").monotonic()
        self._last_technique_count = 0
        self._drift_warnings = 0

        # GPU safety: backoff state
        self._cart_consecutive_fails = 0
        self._pilgrim_consecutive_fails = 0
        self._cart_cooldown_until = 0.0
        self._pilgrim_cooldown_until = 0.0

        # Overwatch Oracle
        self._oracle: OverwatchOracle | None = None
        if self._config.enable_oracle:
            self._oracle = OverwatchOracle(
                config=self._config.oracle,
                on_verdict=self._on_oracle_verdict,
                on_inject=self._on_interrupt,
                on_status=self._on_status,
                context_builder=self._build_oracle_context,
            )

        # Mission context (set externally for Oracle)
        self._mission_objective: str = ""
        self._mission_plan: str = ""
        self._credentials: list[dict] = []
        self._executor_action: str = ""

    @property
    def problem_map(self) -> ProblemMap:
        return self._pmap

    @property
    def consensus_state(self) -> ConsensusState:
        return self._consensus

    @property
    def distilled_log(self) -> DistilledLog:
        return self._log

    @property
    def oracle(self) -> OverwatchOracle | None:
        return self._oracle

    def set_mission_context(
        self,
        objective: str = "",
        plan: str = "",
        credentials: list[dict] | None = None,
    ) -> None:
        """Set mission context so the Oracle can see it."""
        self._mission_objective = objective
        self._mission_plan = plan
        self._credentials = credentials or []

    def set_executor_action(self, action: str) -> None:
        """Update what the executor is currently doing (for Oracle context)."""
        self._executor_action = action

    def start(self) -> None:
        """Start both observer threads."""
        if self._running:
            return

        self._running = True

        self._cart_thread = threading.Thread(
            target=self._cartographer_loop,
            name="weary-cartographer",
            daemon=True,
        )
        self._pilgrim_thread = threading.Thread(
            target=self._pilgrim_loop,
            name="vigilant-pilgrim",
            daemon=True,
        )

        self._cart_thread.start()
        self._pilgrim_thread.start()

        # Start Oracle if enabled
        if self._oracle:
            self._oracle.start()

        self._on_status("Observers started")

    def stop(self) -> None:
        """Stop all observer threads and Oracle."""
        self._running = False
        self._pilgrim_trigger.set()  # unblock pilgrim wait
        if self._oracle:
            self._oracle.stop()
        if self._cart_thread:
            self._cart_thread.join(timeout=5)
        if self._pilgrim_thread:
            self._pilgrim_thread.join(timeout=5)
        # Save final state
        with self._lock:
            save_map(self._mission_dir, self._pmap)
        self._on_status("Observers stopped")

    def health_check(self) -> dict:
        """Check all LLM endpoints including Oracle."""
        cart = check_health(self._config.cartographer_endpoint)
        pilgrim = check_health(self._config.pilgrim_endpoint)
        result = {
            "cartographer": cart,
            "pilgrim": pilgrim,
            "map_nodes": len(self._pmap.nodes),
            "map_edges": len(self._pmap.edges),
            "log_entries": self._log.count(),
            "contradictions": self._pmap.unresolved_contradictions,
        }
        if self._oracle:
            result["oracle"] = self._oracle.health_check()
        return result

    # ---- Cartographer loop ----

    def _check_gpu_safe(self, gpu_index: int = 0) -> bool:
        """Check if GPU utilization is below safety threshold."""
        try:
            import subprocess
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits", f"--id={gpu_index}"],
                capture_output=True, text=True, timeout=5,
            )
            util = int(result.stdout.strip())
            return util < self._config.gpu_util_max_pct
        except Exception:
            return True  # fail-open: if we can't check, allow the call

    def _in_cooldown(self, who: str) -> bool:
        """Check if a component is in failure cooldown."""
        now = time.monotonic()
        if who == "cart":
            return now < self._cart_cooldown_until
        return now < self._pilgrim_cooldown_until

    def _record_llm_fail(self, who: str) -> None:
        """Record an LLM failure, enter cooldown if too many."""
        if who == "cart":
            self._cart_consecutive_fails += 1
            if self._cart_consecutive_fails >= self._config.max_consecutive_llm_fails:
                self._cart_cooldown_until = time.monotonic() + self._config.llm_cooldown_seconds
                self._on_status(f"Cartographer LLM: {self._cart_consecutive_fails} consecutive failures, cooling down {self._config.llm_cooldown_seconds}s")
                self._cart_consecutive_fails = 0
        else:
            self._pilgrim_consecutive_fails += 1
            if self._pilgrim_consecutive_fails >= self._config.max_consecutive_llm_fails:
                self._pilgrim_cooldown_until = time.monotonic() + self._config.llm_cooldown_seconds
                self._on_status(f"Pilgrim LLM: {self._pilgrim_consecutive_fails} consecutive failures, cooling down {self._config.llm_cooldown_seconds}s")
                self._pilgrim_consecutive_fails = 0

    def _record_llm_success(self, who: str) -> None:
        """Reset failure counter on success."""
        if who == "cart":
            self._cart_consecutive_fails = 0
        else:
            self._pilgrim_consecutive_fails = 0

    def _query_memory_for_pilgrim(self) -> str:
        """Query Hyphae and Memoria for context relevant to current attack."""
        import urllib.request
        import json
        context_parts = []

        # Extract recent techniques from the map for query building
        recent_nodes = sorted(
            self._pmap.nodes.values(),
            key=lambda n: n.timestamp or "",
            reverse=True,
        )[:5]
        query_terms = " ".join(n.text[:50] for n in recent_nodes if n.text)[:200]

        if not query_terms.strip():
            return ""

        # Query Hyphae for past session knowledge
        try:
            payload = json.dumps({"query": query_terms, "top_k": 3}).encode()
            req = urllib.request.Request(
                self._hyphae_url + "/recall",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                results = json.loads(resp.read()).get("results", [])
                if results:
                    context_parts.append("=== PAST SESSION KNOWLEDGE (Hyphae) ===")
                    for r in results[:3]:
                        score = r.get("score", 0)
                        text = r.get("text", "")[:200]
                        if score > 0.5:
                            context_parts.append(f"  [{score:.2f}] {text}")
        except Exception:
            pass

        # Query Memoria for attack patterns
        try:
            payload = json.dumps({"query": query_terms, "top_k": 3}).encode()
            req = urllib.request.Request(
                self._memoria_url + "/search",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                results = json.loads(resp.read()).get("results", [])
                if results:
                    context_parts.append("=== ATTACK KNOWLEDGE (Memoria) ===")
                    for r in results[:3]:
                        rel = r.get("relevance", 0)
                        text = r.get("text", "")[:200]
                        if rel > 0.6:
                            context_parts.append(f"  [{rel:.2f}] {text}")
        except Exception:
            pass

        return "\n".join(context_parts)

    def _check_drift(self) -> str | None:
        """Detect if operator is stuck on same techniques for >10 minutes."""
        import time
        now = time.monotonic()

        # Count unique technique types in last 20 log entries
        recent = self._log.read_since(max(0, self._log.count() - 20))
        technique_set = set()
        for entry in recent:
            msg = entry.message[:80] if hasattr(entry, "message") else str(entry)[:80]
            # Extract the tool name
            tool = msg.split(":")[0].strip() if ":" in msg else msg[:20]
            technique_set.add(tool)

        current_count = len(technique_set)

        if current_count == self._last_technique_count:
            elapsed = now - self._last_technique_change_time
            if elapsed > 600:  # 10 minutes same techniques
                self._drift_warnings += 1
                return f"DRIFT ALERT: Same {current_count} techniques repeated for {int(elapsed)}s. The operator may be stuck in a loop."
        else:
            self._last_technique_count = current_count
            self._last_technique_change_time = now
            self._drift_warnings = 0

        return None

    def _build_attack_state_prompt(self) -> str:
        """Build attack-state-aware prompt from RECENT distilled log only."""
        # Read last 20 log entries directly — no stale map data
        recent = self._log.read_since(max(0, self._log.count() - 20))

        lines = []
        for e in recent:
            msg = e.message[:150] if hasattr(e, "message") else str(e)[:150]
            cat = e.category if hasattr(e, "category") else "?"
            lines.append(f"[{cat}] {msg}")

        if not lines:
            return "No recent activity. Waiting for operator to start."

        prompt = "RECENT ATTACK ACTIVITY (last 20 actions):\n"
        prompt += "\n".join(lines)
        prompt += "\n\nExtract software names, version numbers, and error patterns from the above."
        return prompt

    def _cartographer_loop(self) -> None:
        """Poll for new log entries and update the map."""
        while self._running:
            try:
                new_entries = self._log.read_since(self._last_log_count)
                if new_entries:
                    for entry in new_entries:
                        self._process_log_entry(entry)
                    self._last_log_count = self._log.count()

                    # Save map after processing
                    with self._lock:
                        save_map(self._mission_dir, self._pmap)

            except Exception as e:
                self._on_status(f"Cartographer error: {e}")

            time.sleep(self._config.cartographer_poll_seconds)

    def _process_log_entry(self, entry) -> None:
        """Process a single log entry through the Cartographer."""
        formatted = self._log.format_entry(entry)
        alerts = []

        if self._config.use_llm_cartographer:
            import logging
            _logger = logging.getLogger("openkeel.cartographer")

            # GPU safety: skip LLM if in cooldown or GPU too hot
            if self._in_cooldown("cart"):
                _logger.info("Cartographer LLM in cooldown, using structural fallback")
                self._structural_cartographer(entry, alerts)
                return
            if not self._check_gpu_safe(gpu_index=0):
                _logger.warning("GPU 0 utilization too high, skipping LLM call")
                self._structural_cartographer(entry, alerts)
                return

            # Send to Cartographer LLM -- retry once on JSON parse failure
            prompt = build_cartographer_prompt(self._pmap, formatted)
            delta = None
            for _retry in range(2):
                response = complete(
                    self._config.cartographer_endpoint,
                    CARTOGRAPHER_SYSTEM_PROMPT,
                    prompt,
                )
                delta = parse_json_response(response)
                if "error" not in delta:
                    self._record_llm_success("cart")
                    break
                if _retry == 0:
                    _logger.warning(
                        "Cartographer JSON parse failed (attempt 1), retrying: %s",
                        delta.get("error", "unknown"),
                    )
                else:
                    _logger.warning(
                        "Cartographer JSON parse failed twice, falling back to structural: %s",
                        delta.get("error", "unknown"),
                    )
                    self._record_llm_fail("cart")
                    delta = None

            if delta is not None and "error" not in delta:
                with self._lock:
                    alerts = apply_delta(self._pmap, delta)

                # --- Retrieval Bridge: enrich new nodes ---
                try:
                    new_texts = [n.get("text", "") for n in delta.get("add_nodes", [])]
                    if new_texts:
                        existing = {n.text for n in self._pmap.nodes.values()}
                        latent = enrich_new_nodes(new_texts, existing)
                        for ln in latent:
                            from openkeel.core.cartographer import add_node
                            add_node(self._pmap, node_type=ln["node_type"], text=ln["text"],
                                    confidence=ln["confidence"], discovered=ln["discovered"],
                                    tags=["memoria_latent"])
                except Exception as e:
                    import logging
                    logging.getLogger("openkeel.retrieval_bridge").debug(f"Enrichment failed: {e}")

                # ALSO run structural cartographer for edge creation
                # LLM creates nodes, structural creates edges between them
                self._structural_cartographer(entry, alerts)
            else:
                # Fall back to structural mode for this entry
                _logger.warning(
                    "LLM cartographer failed, using structural fallback for: %s",
                    entry.message[:60],
                )
                self._structural_cartographer(entry, alerts)
        else:
            self._structural_cartographer(entry, alerts)

        # Process alerts through consensus
        if alerts:
            actions = process_cartographer_alerts(
                self._consensus, alerts, self._config.consensus
            )
            self._handle_actions(actions)

            # Trigger pilgrim walk if contradiction found
            if any("CONTRADICTION" in a.upper() for a in alerts):
                if self._config.pilgrim_walk_on_contradiction:
                    self._pilgrim_trigger.set()


        # --- Retrieval Bridge: proactive retrieve ---
        try:
            nudges = proactive_retrieve(formatted)
            for nudge in nudges:
                if self._on_nudge:
                    self._on_nudge(nudge, level="proactive_retrieval")
        except Exception:
            pass

    def _structural_cartographer(self, entry, alerts) -> None:
        """Structural-only cartographer: add nodes/edges by category, no LLM needed."""
        from openkeel.core.cartographer import add_node, add_edge
        type_map = {
            "GOAL": "goal",
            "HYPOTHESIS": "assumption",
            "ATTEMPT": "technique",
            "RESULT": "technique",
            "DISCOVERY": "fact",
            "ENV": "environment",
            "CRED": "credential",
            "PIVOT": "goal",
            "CIRCUIT": "fact",
        }
        ntype = type_map.get(entry.category, "fact")
        with self._lock:
            node = add_node(self._pmap, ntype, entry.message)
            if entry.category == "RESULT" and "FAIL" in entry.message.upper():
                node.tried = True
                node.result = "fail"

            # --- Create edges between related nodes ---
            def _latest_of_type(ntypes):
                """Find the most recently added node of given type(s)."""
                best = None
                for nid, n in self._pmap.nodes.items():
                    if nid == node.id:
                        continue
                    if n.node_type in ntypes and (best is None or n.timestamp >= best.timestamp):
                        best = n
                return best

            if entry.category == "HYPOTHESIS":
                goal = _latest_of_type({"goal"})
                if goal:
                    add_edge(self._pmap, goal.id, node.id, "DEPENDS_ON",
                             reason="goal depends on hypothesis")

            elif entry.category == "ATTEMPT":
                hyp = _latest_of_type({"assumption"})
                if hyp:
                    add_edge(self._pmap, node.id, hyp.id, "TRIED_FOR",
                             reason="technique attempts hypothesis")

            elif entry.category == "RESULT":
                # FIXED: Link to most recent ATTEMPT that has no result edge yet
                # (not just any "technique" node, since RESULT also maps to technique)
                best_attempt = None
                for nid, n in self._pmap.nodes.items():
                    if nid == node.id:
                        continue
                    if n.node_type != "technique":
                        continue
                    # Skip nodes that already have a result edge pointing to them
                    has_result = any(
                        e.target == nid and e.edge_type in ("SUPPORTS", "FAILED_TO_RESOLVE")
                        for e in self._pmap.edges
                    )
                    if has_result:
                        continue
                    if best_attempt is None or n.timestamp >= best_attempt.timestamp:
                        best_attempt = n
                if best_attempt:
                    edge_type = "SUPPORTS" if "SUCCESS" in entry.message.upper() else "FAILED_TO_RESOLVE"
                    add_edge(self._pmap, node.id, best_attempt.id, edge_type,
                             reason="result of attempt")

            elif entry.category == "DISCOVERY":
                target = _latest_of_type({"goal", "assumption"})
                if target:
                    add_edge(self._pmap, node.id, target.id, "SUPPORTS",
                             reason="discovery supports goal/hypothesis")

            elif entry.category == "CRED":
                disc = _latest_of_type({"fact"})
                if disc:
                    add_edge(self._pmap, node.id, disc.id, "DISCOVERED_BY",
                             reason="credential found via discovery")

            elif entry.category == "ENV":
                goal = _latest_of_type({"goal"})
                if goal:
                    add_edge(self._pmap, node.id, goal.id, "SAME_CONTEXT",
                             reason="environment context for goal")

            # Log node creation
            try:
                with open("/tmp/cartographer.log", "a") as _cf:
                    import time as _t
                    _cf.write(_t.strftime("%H:%M:%S") + f" NODE [{ntype}]: {entry.message[:100]}\n")
            except Exception:
                pass
            alerts.append(f"CARTOGRAPHER: {ntype} \u2014 {entry.message[:60]}")

    # ---- Pilgrim loop ----

    def _pilgrim_loop(self) -> None:
        """Periodically walk the map looking for blind spots."""
        while self._running:
            # Wait for either timer or trigger
            triggered = self._pilgrim_trigger.wait(
                timeout=self._config.pilgrim_walk_seconds
            )
            self._pilgrim_trigger.clear()

            if not self._running:
                break

            try:
                self._pilgrim_walk()
            except Exception as e:
                self._on_status(f"Pilgrim error: {e}")

    def _pilgrim_walk(self) -> None:
        """Perform one walk of the map."""
        with self._lock:
            # Local graph analysis first (always runs, no LLM needed)
            report = walk_map(self._pmap)

        # LLM-enhanced analysis (with retry on JSON parse failure)
        if self._config.use_llm_pilgrim and len(self._pmap.nodes) >= 3:
            import logging
            _plogger = logging.getLogger("openkeel.pilgrim")

            # GPU safety: skip LLM if in cooldown or GPU too hot
            if self._in_cooldown("pilgrim"):
                _plogger.info("Pilgrim LLM in cooldown, skipping LLM enhancement")
                findings = None
            elif not self._check_gpu_safe(gpu_index=1):
                _plogger.warning("GPU 1 utilization too high, skipping LLM call")
                findings = None
            else:
                # Gather cross-session memory context
                memory_context = self._query_memory_for_pilgrim()

                # Check for drift
                drift = self._check_drift()
                if drift:
                    self._on_status(f"[DRIFT] {drift}")

                prompt = self._build_attack_state_prompt()
                if memory_context:
                    prompt = prompt + "\n\n" + memory_context
                if drift:
                    prompt = prompt + "\n\nDRIFT DETECTED: " + drift
                findings = None
                for _retry in range(2):
                    response = complete(
                        self._config.pilgrim_endpoint,
                        PILGRIM_SYSTEM_PROMPT,
                        prompt,
                    )
                    try:
                        dbg = open("/tmp/pilgrim_daemon_raw.txt", "w")
                        dbg.write("len=" + str(len(response)) + "\n" + response)
                        dbg.close()
                    except Exception:
                        pass
                    findings = parse_json_response(response)
                    if "error" not in findings:
                        self._record_llm_success("pilgrim")
                        break
                    if _retry == 0:
                        _plogger.warning(
                            "Pilgrim JSON parse failed (attempt 1), retrying: %s",
                            findings.get("error", "unknown"),
                        )
                    else:
                        _plogger.warning(
                            "Pilgrim JSON parse failed twice, skipping LLM enhancement: %s",
                            findings.get("error", "unknown"),
                        )
                        self._record_llm_fail("pilgrim")
                        findings = None

            if findings is not None and "error" not in findings:
                # V2 format: write directly as nudge, bypass consensus gate
                v2_parts = []
                rec = findings.get("top_recommendation", {})
                if isinstance(rec, dict) and rec.get("action"):
                    v2_parts.append("Try: " + rec["action"])
                    if rec.get("reasoning"):
                        v2_parts.append("Why: " + rec["reasoning"])
                alts = findings.get("alternative_paths", [])
                for alt in alts[:2]:
                    if isinstance(alt, dict) and alt.get("action"):
                        v2_parts.append("Alt: " + alt["action"])
                fa = findings.get("false_assumption", "")
                if fa:
                    v2_parts.append("Challenge: " + fa)
                mq = findings.get("memoria_query", "")
                if mq:
                    v2_parts.append("Query Memoria: " + mq)
                overall = findings.get("overall", "")
                if overall:
                    v2_parts.append("Bottom line: " + overall)
                if v2_parts:
                    nudge_text = "PILGRIM V2 INTEL: " + " | ".join(v2_parts)
                    self._on_nudge(nudge_text)
                    # Log to visible file
                    try:
                        with open("/tmp/pilgrim_nudges.log", "a") as _pf:
                            import time as _t
                            _pf.write(_t.strftime("%H:%M:%S") + " NUDGE: " + nudge_text[:300] + "\n")
                    except Exception:
                        pass
                    self._on_status("Pilgrim V2: nudge delivered (" + str(len(v2_parts)) + " parts)")
                # V3: Execute retrieval queries against Memoria and return actual results
                queries = findings.get("queries", [])
                if queries:
                    import urllib.request
                    import json as _json
                    retrieval_parts = []
                    for q in queries[:5]:
                        query_text = q.get("query", "") if isinstance(q, dict) else str(q)
                        priority = q.get("priority", "medium") if isinstance(q, dict) else "medium"
                        if not query_text or len(query_text) < 5:
                            continue
                        # Query Memoria
                        try:
                            payload = _json.dumps({"query": query_text, "top_k": 3}).encode()
                            req = urllib.request.Request(
                                "http://127.0.0.1:8000/search",
                                data=payload,
                                headers={"Content-Type": "application/json"},
                            )
                            with urllib.request.urlopen(req, timeout=8) as resp:
                                results = _json.loads(resp.read()).get("results", [])
                                for r in results[:2]:
                                    rel = r.get("relevance", 0)
                                    text = r.get("text", "")[:250]
                                    if rel > 0.7:
                                        retrieval_parts.append("[" + str(round(rel, 2)) + "|" + priority + "] " + text)
                        except Exception:
                            pass
                    if retrieval_parts:
                        retrieval_nudge = "PILGRIM RETRIEVAL: " + " ||| ".join(retrieval_parts)
                        self._on_nudge(retrieval_nudge)
                        try:
                            with open("/tmp/pilgrim_nudges.log", "a") as _pf:
                                import time as _t
                                _pf.write(_t.strftime("%H:%M:%S") + " RETRIEVAL: " + retrieval_nudge[:300] + "\n")
                        except Exception:
                            pass
                        self._on_status("Pilgrim retrieval: " + str(len(retrieval_parts)) + " facts from Memoria")
                # Loop detection
                if findings.get("loop_detected"):
                    loop_desc = findings.get("loop_description", "Operator may be stuck in a loop")
                    self._on_interrupt("LOOP DETECTED: " + loop_desc + " — STOP and try a different approach.")
                    self._on_status("Pilgrim: LOOP DETECTED")
                # Also try legacy format
                report = apply_pilgrim_findings(report, findings)

        # Bridge findings to treadstone tree

            # --- Retrieval Bridge: fill blind spots ---
            try:
                if report and report.blind_spots:
                    spots = [{"description": s.description, "category": s.category, "severity": s.severity}
                             for s in report.blind_spots]
                    known = [n.text for n in self._pmap.nodes.values() if n.discovered]
                    objective = getattr(self, '_mission_objective', '')
                    enriched = fill_pilgrim_gaps(spots, known, objective)
                    if enriched:
                        nudge_text = format_enrichment_nudge(enriched)
                        if nudge_text and self._on_nudge:
                            self._on_nudge(nudge_text, level="retrieval_bridge")
            except Exception as e:
                import logging
                logging.getLogger("openkeel.retrieval_bridge").debug(f"Gap fill failed: {e}")

        self._bridge_to_treadstone(report)

        # Process through consensus
        actions = process_pilgrim_report(
            self._consensus, report, self._config.consensus
        )
        self._handle_actions(actions)

        # Update status
        status = consensus_status_line(self._consensus)
        self._on_status(status)

    # ---- Action dispatch ----

    def _handle_actions(self, actions: list[dict]) -> None:
        """Route consensus actions to callbacks."""
        interrupts = [a for a in actions if a["type"] == "interrupt"]
        nudges = [a for a in actions if a["type"] == "nudge"]

        if interrupts:
            injection = format_injection(interrupts)
            if injection:
                self._on_interrupt(injection)

        if nudges:
            nudge = format_nudge(nudges)
            if nudge:
                self._on_nudge(nudge)

    # ---- Treadstone bridge ----

    def _bridge_to_treadstone(self, report: PilgrimReport) -> None:
        """Bridge observer blind spots into treadstone hypotheses."""
        try:
            from openkeel.core.treadstone import (
                load_tree, save_tree, add_hypothesis, get_active_stone,
            )

            tree = load_tree(self._mission_dir)
            if not tree:
                return

            stone = get_active_stone(tree)
            if not stone:
                return

            # Only create hypotheses for significant blind spots
            existing = {h.label.lower() for h in stone.hypotheses}
            added = 0

            for spot in report.blind_spots:
                if spot.severity < 6:
                    continue
                if spot.category not in ("unexplored", "killer_question", "contradiction"):
                    continue

                label = (spot.suggested_action or spot.description)[:80]
                if label.lower() in existing:
                    continue

                add_hypothesis(
                    stone,
                    label=label,
                    rationale=f"Observer ({spot.category}, severity {spot.severity}/10): {spot.description}",
                    initial_confidence=0.35,
                    tags=["observer", spot.category],
                )
                added += 1
                existing.add(label.lower())

            if added:
                save_tree(self._mission_dir, tree)
                self._log.append(
                    "OBSERVER",
                    f"Pilgrim created {added} hypotheses from blind spots",
                )
                self._on_status(f"Bridge: {added} new hypotheses from observer findings")

        except Exception as e:
            self._on_status(f"Bridge error: {e}")

    # ---- Oracle integration ----

    def _build_oracle_context(self) -> str:
        """Build the Oracle's full context from all available sources."""
        from openkeel.core.cartographer import map_to_prompt_context

        # Tree summary
        tree_summary = ""
        try:
            from openkeel.core.treadstone import load_tree, tree_status_line
            tree = load_tree(self._mission_dir)
            if tree:
                tree_summary = tree_status_line(tree)
        except Exception:
            pass

        # Problem map summary
        map_summary = ""
        with self._lock:
            if self._pmap.nodes:
                map_summary = map_to_prompt_context(self._pmap, max_nodes=30)

        # Recent log entries
        log_window = self._log.format_window(n=20)

        # Pilgrim findings
        pilgrim_findings = ""
        if self._consensus.pilgrim_reports:
            latest = self._consensus.pilgrim_reports[-1]
            spots = latest.blind_spots[:5]
            if spots:
                pilgrim_findings = "\n".join(
                    f"  [{s.severity}/10] {s.description}" for s in spots
                )

        # Cartographer alerts
        cart_alerts = ""
        if self._consensus.cartographer_alerts:
            recent = self._consensus.cartographer_alerts[-10:]
            cart_alerts = "\n".join(f"  - {a}" for a in recent)

        return build_oracle_context(
            mission_objective=self._mission_objective,
            mission_plan=self._mission_plan,
            credentials=self._credentials,
            tree_summary=tree_summary,
            map_summary=map_summary,
            log_window=log_window,
            pilgrim_findings=pilgrim_findings,
            cartographer_alerts=cart_alerts,
            executor_action=self._executor_action,
        )

    def _on_oracle_verdict(self, verdict: OracleVerdict) -> None:
        """Handle an Oracle verdict — show as nudge if actionable."""
        if verdict.is_actionable:
            self._on_nudge(
                f"[ORACLE {verdict.inference_seconds:.0f}s] {verdict.answer}"
            )
