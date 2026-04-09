# Calcifer's Ladder

Companion architecture notes for the Ladder work.

Files:

**Central Reference:**
- `CHANGES_ROADMAP_2026-04-09.md` ⭐ — Complete summary of recent changes: rate limit fixes, Ladder architecture, Claude CLI integration, what's documented and what's next

**Architecture & Design:**
- `architecture_scaffold_2026-04-08.md` — proposed architecture scaffold for an adaptive CLI agent with an outer supervisory loop and inner agent loops
- `intention_packet_2026-04-08.md` — proposed `IntentionPacket` runtime object for representing invariant user intent above `StatusPacket` and `Directive`
- `conversation_shapes_and_escalation_2026-04-08.md` — patterns for when/how to escalate between rungs

**Build & Implementation:**
- `agent_build_scaffold_2026-04-09.md` — fractal agent swarm design for complex multi-agent tasks
- `implementation_blueprint_2026-04-08.md` — detailed implementation steps for Ladder components

**Critique & Investigation:**
- `critique_2026-04-08.md` — critique of `../calcifer_ladder_design_2026-04-08.md` using the Claude CLI loop as reference
- `claude_rate_limit_investigation_2026-04-09.md` — investigation context for the rate limit problem (now FIXED)

**Related top-level docs:**
- `../calcifer_ladder_design_2026-04-08.md` — Full Ladder design with routing rules
- `../calcifers_ladder_handoff_2026-04-07.md` — Original handoff context
- `../token_saver_v6_final_pass_2026-04-07.md` — Token Saver v6 overview
