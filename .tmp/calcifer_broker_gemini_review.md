I will now analyze the "Calcifer Broker Sketch" and provide an adversarial systems review, focusing on structural weaknesses, failure modes, and potential for production collapse.

### TL;DR
The "Calcifer Broker" design is a classic **over-engineered hierarchy** that risks **cascading summarization loss** and **infinite "Cheap-Loop-Hell."** While it attempts to constrain costs, it creates a "Verifier Paradox" where the supervisor is often less competent than the laborer. The strict separation of Rungs will likely lead to "State Drift," where Opus makes strategic decisions based on a game of "telephone" played by lower-tier summarizers.

---

### Critical Findings

#### 1. The Verifier Paradox (Authority vs. Competence)
The design requires a verifier to gate all upward trust. However, if a Rung 3 Specialist Worker (e.g., a coding agent) performs a complex task, a Rung 2 Cheap Model or Rung 1 Script is fundamentally incapable of verifying the *semantic correctness* of that task. 
- **Risk:** You will either end up with "Security Theater" (verifiers checking for file existence but missing logic bugs) or you will be forced to use Opus (Rung 5) to verify Rung 3, destroying your cost-savings logic.

#### 2. Context Thinning & Summarization Decay
The mandate that "Opus consumes summaries, not raw exhaust" is a recipe for **strategic hallucination**. In long-horizon tasks, the delta between "what actually happened" and "what the Broker told the StatusPacket" grows with every turn. 
- **Risk:** Opus will issue "Directives" based on a sanitized, low-resolution map of the workspace. When the worker encounters a ground-truth reality that wasn't summarized (e.g., a specific library version conflict), the system will deadlock because the strategic layer can't see the obstacle.

#### 3. The "Cheap-Loop-Hell" Incentive
The "Cheapest-first" rule combined with "Retry_same_rung" creates an incentive for the system to churn at Rungs 1 and 2. 
- **Risk:** The system may spend $0.50 in "cheap" tokens failing 10 times at a task that requires a single $0.10 Rung 3 call, but the Broker's local logic won't realize it because each individual failure looks "low risk." There is no explicit "Global Budget Burn Rate" monitor mentioned to prevent death by a thousand small tool calls.

#### 4. The "Resume" Fallacy
"Resume is first-class" assumes that `StatusPacket` and `ExecutionReport` contain all state. They don't. The actual state is in the file system, the environment, and the hidden context of the models. 
- **Risk:** If the Broker is interrupted and resumes from a `StatusPacket`, but a Rung 1 script left the environment in a "dirty" state (e.g., a half-applied migration), the `StatusPacket` won't capture it. The system lacks a **Global Rollback or State-Revert** protocol.

---

### Secondary Findings

*   **Schema Rigidity:** The `Directive` and `ExecutionReport` schemas are highly structured but don't account for **inter-task dependencies**. If Task A changes the requirements for Task B mid-execution, the "StatusPacket" mechanism for propagating that change is undefined.
*   **Memory Pollution:** "Store durable knowledge" is subjective. Without a "Forget" or "Prune" mechanism, Hyphae will eventually become a swamp of conflicting "Facts" from different versions of the project.
*   **Latency Explosion:** The state machine has 9 steps per loop. If each step involves a model call or a complex verification, the "Wall Clock Time" for even simple tasks will be unacceptable for interactive use.

---

### What To Change First

1.  **Implement "Peek-Through" Debugging:** Opus must have the ability to request "Raw Exhaust" for specific `goal_id`s. Strategic reasoning cannot be strictly isolated from ground truth when confidence drops.
2.  **Define "Evidence Standards" by Task Type:** Instead of a generic `Verifier`, create a registry of **Domain Verifiers**. A "Code Change" task *must* require a passing test suite, not just a model's "confidence" score.
3.  **Add a "Velocity" Metric to Escalation:** The Broker should escalate not just on "Risk" or "Ambiguity," but on **Lack of Progress**. If 3 turns pass without a change in `recent_evidence`, force an upward escalation regardless of "Budget State."
4.  **Invert the Memory Policy:** Instead of "Remember on completion," use **"Remember on Surprise."** Only store facts that contradict the previous `StatusPacket`. This reduces noise and highlights what the system actually learned.
5.  **State Checkpointing:** Add a mandatory "Environment Hash" to the `StatusPacket`. If the hash changes unexpectedly between directives, the Broker must assume state drift and trigger a "Rung 4 Planner" audit.
