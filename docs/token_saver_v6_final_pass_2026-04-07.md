# Token Saver v6 Final Pass — Easy Wins Left

Date: 2026-04-07
Audience: Claude / Ben
Scope: final high-signal critique before pausing work for a week

## Bottom line

v6 found real savings, but the local-LLM layer is still aimed too low. It is doing some routing and some summarization. The next easy wins are mostly about using the local model to decide what **never enters Claude's prompt at all**, especially on long interactive sessions where `cache_read` dominates.

Current measured state:

- `python3 -m openkeel.token_saver.one_metric` now reports `-13.4%` week-over-week pool-units.
- Proxy last 24h still shows only `9` traced turns and `0` Haiku routings, so routing upside remains mostly unproven in live traffic.
- The strongest directly measured win so far is still the `-p` session-start strip, not the router.

## Major easy wins still left out

### 1. Wire Anthropic context editing into the proxy

This is the cleanest omitted lever for long sessions.

Why it matters:

- v6 already concluded that long interactive cost is dominated by accumulated cached context, not one-shot `cache_creation`.
- Anthropic now exposes server-side context editing / tool-result clearing before the prompt reaches Claude.
- That is more aligned with the real remaining denominator than another round of output summarization.

What to do:

- Add `context_management` / context-editing controls in the proxy for long sessions.
- Start conservative: keep recent tool results, clear older bulky tool outputs, exclude critical tools.
- Measure before/after on `cache_read_input_tokens`, not just request chars.

Sources:

- Anthropic context editing docs: <https://platform.claude.com/docs/en/build-with-claude/context-editing>
- Anthropic context management blog: <https://claude.com/blog/context-management>

### 2. Use the local LLM as a goal-conditioned code skimmer

Right now the local model is mostly acting as a summarizer/router. That is leaving value on the table.

Better use:

- For `Read`: select relevant spans, symbols, and line ranges for the current task.
- For `Grep`: cluster hits and keep only goal-relevant groups.
- For old tool outputs: extract durable state, not prose summary.

Why this is an easy win:

- You already have the interception points.
- You already have a local model online.
- This attacks the bytes before they become part of cached history.

Research support:

- SWE-Pruner reports `23–54%` token reduction on coding-agent tasks with lightweight task-aware pruning.
- Their key move is selection, not generic summarization.

Source:

- <https://arxiv.org/abs/2601.16746>

### 3. Build a hierarchical code-context view instead of flat compression

The local LLM currently has to reason over flat file/text chunks. That is weaker than it needs to be.

What is missing:

- file -> symbol -> span tree
- reusable compact repo sketch
- path-aware and symbol-aware retrieval before full text is sent

Why it matters:

- Hierarchical compression is a better fit for large codebases than linearly summarizing files.
- It gives the local LLM a safer job: pick nodes, not narrate code.

Research support:

- Stingy Context reports `18:1` hierarchical code compression.

Source:

- <https://arxiv.org/abs/2601.19929>

### 4. Add a real confidence gate to the qwen router

The router is still too binary.

Problem:

- `_qwen_classify()` returns only a model guess.
- No confidence, no abstain path beyond timeout/failure.
- No accuracy feedback loop.

Easy improvement:

- Return `{model, confidence, reason}`.
- Require high confidence for Haiku.
- On low confidence, fall back to Sonnet.
- Log routed turns and sample some for later Opus grading.

This is especially important because live traffic has not yet shown meaningful Haiku usage, so the router's projected upside is still mostly hypothetical.

### 5. Use token counting as a control loop, not just a postmortem

The current measurement story still relies too much on after-the-fact billing inference.

Easy win:

- For each major rewrite path, run a token-count preview before and after.
- Reject rewrites that reduce bytes but break cacheability.
- Use this to tune cache breakpoints and context-edit settings.

Source:

- Anthropic prompt caching docs: <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>
- Don't Break the Cache: <https://arxiv.org/abs/2601.06007>

### 6. Tighten memory externalization before context clearing

If you clear context without externalizing durable state, the agent gets dumber.

Easy version:

- Write a small structured state object after major turns:
  - current objective
  - active files
  - constraints
  - confirmed findings
  - open questions
- Then clear raw tool sludge more aggressively.

This pairs naturally with Hyphae and makes context editing safer.

## Critique of current v6 direction

The current stack still spends too much effort on **compressing outputs already produced** and not enough on **selecting what should never enter Claude's prompt**.

The local LLM should mainly do:

- selection
- ranking
- pruning
- span extraction
- route confidence / abstention

It should do less:

- free-form summarization
- unsupported confidence-free routing claims
- brittle regex cleanup as the main line of defense

## Recommended final work pass

If there is one more round of work before pausing:

1. Add context editing for long sessions in the proxy.
2. Add local-LLM span selection for `Read` and `Grep`.
3. Add confidence-gated routing, especially for Haiku.
4. Add token-count measurement for each rewrite mode.
5. Persist one immutable benchmark artifact per run.

That is the highest-signal remaining move set.

## Final estimate: plan-extension multiplier

Grounded estimate from current evidence:

- Measured now: `-13.4%` pool-units week over week.
- If routing starts actually biting and the remaining easy wins above are landed, the most realistic medium-term outcome is around `-25%` to `-35%`.
- Stretch case with successful context editing plus competent routing plus stronger local pruning: around `-40%` to `-50%`.

If vanilla is `X20`, that implies:

- current measured state: about `X23`
- realistic after the final pass fully lands: about `X27` to `X31`
- strong stretch case: about `X33` to `X40`

I would **not** forecast `X50+` from the current evidence. That requires assumptions the live traffic does not support yet.
