# Token Saver Proxy Audit — 2026-04-07

This note is for the agent currently working on the new proxy/context-saving system.

## Executive Summary

The proxy stress test did run, but it has **not yet shown meaningful context reduction**.
During the live run, request payloads were still very large, and the proxy trace's
`usage` extraction was broken even though the streamed Anthropic response clearly
contained real usage fields.

## What Was Observed

Live process seen during the run:

- `python3 /home/om/openkeel/tools/token_saver_proxy.py`

Live trace target:

- `/home/om/.openkeel/proxy_trace.jsonl`

Stress script observed:

- `/tmp/stress.sh`

The stress script used:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
timeout 45 claude -p "..."
```

## Live Measurements Seen During The Run

From the live proxy trace while the test was active:

- `3` logged `v1/messages` turns were observed before the file was truncated/reset
- average request body: about `122,064` chars
- average system block: about `30,994` chars
- average tools block: about `70,074` chars
- average latency: about `1,771 ms`

Observed request sizes:

- `122,825` chars
- `121,372` chars
- `121,994` chars

This suggests the new system is **not yet shrinking the base request materially**.

## Critical Finding: Usage Parsing Is Broken In The Trace

While the stress run was active, `proxy_trace.jsonl` entries showed:

```json
"usage": {"in": 0, "cache_read": 0, "cache_create": 0, "out": 0}
```

But the proxy debug log proves the streamed Anthropic response did include real usage:

File:

- `/tmp/tsproxy_dbg.log`

Observed example:

```text
usage":{"input_tokens":6,"cache_creation_input_tokens":15496,"cache_read_input_tokens":29165,...,"output_tokens":1}
...
usage":{"input_tokens":6,"cache_creation_input_tokens":15496,"cache_read_input_tokens":29165,"output_tokens":6}
```

## Interpretation

For a trivial `say pong` turn, the proxy saw:

- fresh input: `6` tokens
- cache creation: `15,496`
- cache read: `29,165`
- final output: `6`

So the current run strongly supports the earlier conclusion:

- **cache/context is still the dominant cost**
- **the proxy has not yet reduced that cost**
- **the trace accounting layer is currently hiding real usage**

## Most Likely Immediate Bug

`tools/token_saver_proxy.py` is almost certainly failing to:

1. accumulate streamed usage correctly across `message_start` and `message_delta`
2. write the final merged usage object into `proxy_trace.jsonl`

The streamed response clearly contains the fields, so the problem appears to be in
the proxy's event parsing / finalization logic, not in Anthropic's response.

## Secondary Testing Problem

The stress harness reuses and truncates:

- `/home/om/.openkeel/proxy_trace.jsonl`

After the run, the file was empty again, which makes outside observation unreliable.

Recommended fix:

1. write each run to a unique artifact
2. preserve a final immutable summary JSON or Markdown file
3. include per-turn request chars and parsed usage totals

## Recommended Next Steps

1. Fix usage extraction in `tools/token_saver_proxy.py`
2. Preserve a final per-run artifact instead of truncating the shared trace
3. Re-run the same 10-prompt stress test
4. Compare:
   - request body chars per turn
   - `cache_read_input_tokens`
   - `cache_creation_input_tokens`
   - total billed input per turn

## Bottom Line

The proxy is alive and seeing real Anthropic usage metadata.
The accounting/logging is not.
The observed request sizes are still huge.
So this run does **not** yet demonstrate successful context reduction.
