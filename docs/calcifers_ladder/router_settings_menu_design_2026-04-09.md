# Calcifer's Ladder: Router Settings Menu Design

**Date:** 2026-04-09  
**Status:** Design proposal  
**Scope:** Add a user-facing settings menu that changes how Calcifer routes chat, planning, execution, and escalation across local models and Claude tiers.

## Why this should exist

Calcifer currently has a strong routing idea, but most of the policy is embedded directly in code:

- band classification in `openkeel/calcifer/band_classifier.py`
- planner selection in `openkeel/calcifer/broker_gui_adapter.py`
- execution mode selection in the planning agents
- UI messaging in `openkeel/calcifer/ladder_chat.py`

That hard-coding is acceptable while the system is young, but it creates three problems:

1. The user cannot express preferences about cost, intelligence, or local-vs-cloud behavior.
2. The system cannot cleanly support different plan sizes or budget profiles.
3. The UI concept of "the Ladder" becomes hard to trust if the routing behavior cannot be inspected or altered.

The missing piece is not another router heuristic. The missing piece is a first-class routing policy layer.

## Product goal

The settings menu should let a user shape Calcifer's intelligence stack without editing code.

Examples:

- "Use Sonnet for almost everything, only escalate to Opus when truly needed."
- "I'm on a tiny plan. Prefer local models and only touch cloud when I explicitly force it."
- "Use Gemma 4 26B instead of Haiku and Sonnet where possible."
- "Let Opus think and judge, but keep normal execution on cheaper models."
- "Never use Opus unless I tag `@opus`."

This is not just a convenience setting. It changes the economic identity of Calcifer.

## Core design principle

The settings menu should be based on **roles and constraints**, not just a flat list of model names.

Bad design:

- "Band A model"
- "Band B model"
- "Band C model"
- six unchecked dropdowns with no explanation

Better design:

- `chat model`
- `planner model`
- `executor model`
- `judge model`
- `max cloud tier`
- `allow Opus automatically`
- `local-first aggressiveness`

Why this matters:

- models change over time
- the same model may serve different roles well or badly
- users think in terms of budget and trust, not in terms of internal class names

The policy layer should still support band-specific overrides, but those should sit on top of the role model rather than replace it.

## What the user should be able to control

### 1. Budget preset

This is the top-level setting. It should set a whole routing posture with one click.

Suggested presets:

- `Tiny plan`
  - direct and local first
  - avoid cloud unless forced or repeated failure
  - Opus disabled except explicit override
- `Balanced`
  - current intended default
  - Sonnet for standard work
  - Opus for high-judgment planning and recovery
- `Quality`
  - Sonnet and Opus used more aggressively
  - fewer local substitutions
  - lower tolerance for cheap-model drift
- `Local-max`
  - use strongest local model available for most work
  - reserve cloud for judgment or explicit escalation

These presets should be real config templates, not cosmetic labels.

### 2. Role mapping

The user should be able to assign a model or mode to each routing role.

Required roles:

- `chat_model`
- `planner_model`
- `executor_model`
- `judge_model`

Possible values:

- `direct`
- `gemma4_small`
- `qwen25`
- `gemma4_large`
- `haiku`
- `sonnet`
- `opus`

Notes:

- `direct` is valid only for certain roles
- `judge_model` should probably allow only `sonnet` or `opus`
- `planner_model` can be local if local planning is intentionally supported

### 3. Escalation controls

These settings determine how much freedom the system has to spend upward.

Required controls:

- `allow_auto_escalation`
- `allow_opus_planning`
- `allow_opus_judgment`
- `max_model_tier`
- `local_failures_before_cloud`
- `cloud_failures_before_opus`

This is the heart of the economic policy.

For a tiny-plan user, `allow_opus_planning` might be false and `max_model_tier` might be `haiku` or even `gemma4_large`.

For a quality-first user, `allow_auto_escalation` might be true and `local_failures_before_cloud` might be zero.

### 4. Band overrides

Even with role-based routing, users may want direct control over the existing band system.

For each band A-E, allow an optional override:

- planner override
- executor override
- judge override
- disable planner skip

This preserves the current architecture while making it configurable.

Examples:

- force Band A chat to `gemma4_small`
- force Band C planning to `sonnet`
- force Band D planning to `opus`
- force Band B to stay `direct`

### 5. Explicit override behavior

The system already has the idea of user-forced routing (`@opus`, etc.). The settings menu should define how strong those overrides are.

Controls:

- `force_tags_always_win`
- `allow_force_tags_above_max_tier`
- `confirm_opus_when_tiny_plan`

This matters because some users will want hard safety rails, while others will want manual escape hatches.

## Recommended policy object

Add a single config object as the source of truth.

Suggested file:

- `openkeel/calcifer/routing_policy.py`

Suggested structure:

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RoleMap:
    chat_model: str = "sonnet"
    planner_model: str = "sonnet"
    executor_model: str = "sonnet"
    judge_model: str = "opus"


@dataclass
class EscalationPolicy:
    allow_auto_escalation: bool = True
    allow_opus_planning: bool = True
    allow_opus_judgment: bool = True
    max_model_tier: str = "opus"
    local_failures_before_cloud: int = 1
    cloud_failures_before_opus: int = 1


@dataclass
class BandOverride:
    planner_model: Optional[str] = None
    executor_model: Optional[str] = None
    judge_model: Optional[str] = None
    disable_planner_skip: bool = False


@dataclass
class RoutingPolicy:
    preset_name: str = "balanced"
    roles: RoleMap = field(default_factory=RoleMap)
    escalation: EscalationPolicy = field(default_factory=EscalationPolicy)
    band_overrides: dict[str, BandOverride] = field(default_factory=dict)
    force_tags_always_win: bool = True
    allow_force_tags_above_max_tier: bool = True
```

This object should be serializable to JSON so the UI can read and write it without custom glue everywhere.

## Recommended storage

Add one small config module:

- `openkeel/calcifer/router_config.py`

Responsibilities:

- load routing policy from disk
- save routing policy to disk
- provide default presets
- validate that selected models are available
- gracefully fall back if a configured model disappears

Suggested storage path:

- `~/.config/openkeel/calcifer_router.json`

If project-local config is preferred, a repo-relative or session-relative path could also work, but user-level config better matches the meaning of "my plan", "my budget", and "my preferred routing posture".

## Recommended UI

The UI should be simple enough to understand quickly, but expressive enough to matter.

### Entry points

Possible entry points:

- toolbar button in `ladder_chat.py`
- gear icon near the route label
- right-click menu on the runner strip

The cleanest first version is a gear icon in the toolbar.

### Dialog sections

The settings dialog should have four sections:

1. `Preset`
2. `Roles`
3. `Escalation`
4. `Advanced overrides`

Suggested content:

#### Preset

- dropdown or segmented control for preset
- short description text under each preset

#### Roles

- dropdowns for chat/planner/executor/judge
- inline note about what each role means

#### Escalation

- checkboxes for Opus permissions
- slider or spinbox for failure thresholds
- dropdown for max tier

#### Advanced overrides

- optional per-band table
- force-tag behavior toggles

### UX rule

The dialog must explain consequences in plain language.

Examples:

- "Tiny plan: avoids cloud unless necessary"
- "Local-max: prefers local Gemma/Qwen for standard work"
- "Judge model decides what to do when a step fails"

Without these explanations, the menu becomes a developer control panel instead of a user-facing product feature.

## How this should integrate with current code

### `ladder_chat.py`

Responsibilities after change:

- open settings dialog
- load current policy on startup
- display current preset or routing posture in the toolbar
- optionally show the active policy during a turn

Important: the UI should stop promising behavior that is not backed by policy.

### `broker_gui_adapter.py`

This file should become the first consumer of the policy object.

Instead of hard-coding:

- Band A -> Sonnet
- Band C -> Sonnet planner
- Band D/E -> Opus planner

it should ask the policy:

- which planner is allowed for this band
- which executor to use for a direct step
- whether escalation to Opus is allowed

This is the highest-value integration point because it sits at the front door of the live routing path.

### Planning agents

The planning agents should no longer be selected directly by band alone. Instead:

- choose planner model from policy
- instantiate or dispatch to the correct planning backend
- preserve fallback behavior if the preferred planner fails

This may require a thin planner abstraction rather than `if band == C: Sonnet else Opus`.

### Executors

Execution should also honor policy, especially for:

- lightweight reasoning tasks
- edit steps
- retries after partial failure

If a user wants `gemma4_large` to replace Sonnet for executor work, the system should be able to express that directly.

### Judgment / recovery

This is where budget protection matters most.

The policy must decide:

- whether judgment is handled by Sonnet or Opus
- whether repeated failures unlock a stronger tier
- whether a tiny-plan user is allowed automatic recovery escalation

## Presets worth shipping first

These four presets cover most real use cases.

### 1. Tiny plan

- chat: `gemma4_small`
- planner: `gemma4_large`
- executor: `gemma4_large`
- judge: `haiku`
- Opus: off unless explicit override
- escalation: conservative

Who this is for:

- users on small cloud budgets
- users who mostly want local inference

### 2. Balanced

- chat: `sonnet`
- planner: `sonnet`
- executor: `sonnet`
- judge: `opus`
- escalation: moderate

Who this is for:

- most users
- stable day-to-day coding and reasoning

### 3. Local-first with Opus governor

- chat: `gemma4_large`
- planner: `gemma4_large`
- executor: `gemma4_large`
- judge: `opus`
- escalation: cloud only after failure

Who this is for:

- users with good local hardware
- users who want strong local autonomy with strategic cloud backup

### 4. Quality

- chat: `sonnet`
- planner: `opus`
- executor: `sonnet`
- judge: `opus`
- escalation: aggressive

Who this is for:

- architecture work
- sensitive debugging
- high-value sessions where latency and spend matter less

## Design constraints

The menu should not become an unbounded policy engine.

Rules:

1. Keep the first version deterministic and inspectable.
2. Every setting must have a visible runtime effect.
3. Do not introduce policy options that the backend cannot enforce.
4. Prefer a few meaningful presets over many low-signal toggles.
5. Policy should override heuristics, not fight them invisibly.

The easiest failure mode here is overdesign: too many switches, no clear mental model, and unclear actual runtime behavior.

## Observability requirement

This feature should ship with routing transparency.

At minimum, each turn should make it possible to see:

- classified band
- planner model used
- execution model(s) used
- whether escalation occurred
- whether policy blocked a stronger model from being used

Without that, users cannot verify that the menu is doing anything real.

The best place to expose this is:

- token trace log
- optional hover or details panel in the UI
- status text near the route label

## Implementation order

Recommended sequence:

1. Create `RoutingPolicy` dataclasses and JSON storage.
2. Add default presets and config validation.
3. Wire `BrokerGUIAdapter` to read policy instead of hard-coded planner choices.
4. Wire execution and judgment routing to the same policy.
5. Add a minimal settings dialog in `ladder_chat.py`.
6. Surface per-turn routing telemetry in the UI.
7. Add tests for policy enforcement.

This order matters because the policy layer should exist before the UI. Otherwise the menu is just decoration.

## Minimal viable version

If the full feature feels too large, the smallest valuable version is:

- one JSON config file
- four presets
- no per-band UI yet
- a single dropdown in the toolbar
- broker obeys the preset

That version already unlocks:

- tiny-plan mode
- local-first mode
- quality mode
- Sonnet-only or Opus-guarded behavior

## Why this is worth doing

Calcifer is not just a chatbot. It is a budgeted intelligence router.

That means the routing posture is part of the product, not an implementation detail.

A settings menu for routing policy would:

- make the system legible
- make token spend intentional
- let users adapt the stack to their actual subscription constraints
- let strong local hardware materially change the economics
- reduce the pressure to bake every routing preference into code

Most importantly, it would let Calcifer feel like an instrument rather than a fixed appliance.

## Recommended repo placement

This document belongs with the other Ladder architecture notes:

- `docs/calcifers_ladder/router_settings_menu_design_2026-04-09.md`

If implementation begins later, the likely code touchpoints are:

- `openkeel/calcifer/ladder_chat.py`
- `openkeel/calcifer/broker_gui_adapter.py`
- `openkeel/calcifer/band_classifier.py`
- `openkeel/calcifer/opus_planning_agent.py`
- `openkeel/calcifer/sonnet_planning_agent.py`
- `openkeel/calcifer/executors/`
- new files:
  - `openkeel/calcifer/routing_policy.py`
  - `openkeel/calcifer/router_config.py`
