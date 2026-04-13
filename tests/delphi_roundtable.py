"""Delphi Round Table — Independent assessment of A/B results by 3 AI models.

Sends the raw experimental data to Claude (Anthropic), GPT-4o (OpenAI), and Gemini (Google)
for independent analysis. Each model assesses whether the token savings are real, meaningful,
and whether the approach is scientifically sound.

Usage:
  python3 tests/delphi_roundtable.py --results tests/ab_results.json --output tests/delphi_report.md

Requires env vars:
  ANTHROPIC_API_KEY — for Claude
  OPENAI_API_KEY — for GPT-4o / Codex
  GEMINI_API_KEY — for Gemini
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path


ASSESSMENT_PROMPT = """You are an independent technical reviewer assessing an experiment.

## Context
OpenKeel is a tool that wraps Claude Code (Anthropic's CLI agent). It implements "bubble delegation" — instead of Sonnet (the expensive model) doing all the work, it uses Sonnet only for planning and synthesis, delegating sub-task execution to cheaper models (Haiku API or local LLMs via Ollama).

## Experiment Design
The same set of codebase analysis tasks were run under two configurations:
- **Vanilla**: Sonnet CLI handles everything (plan + execute sub-tasks + synthesize)
- **Flat (bubble)**: Sonnet CLI plans + synthesizes. A Haiku API classifier routes each sub-task to either a local LLM (for simple lookups) or Haiku API with tool use (for moderate analysis).

Each task was run once per config on the same codebase (OpenKeel v2, ~3300 lines across 19 Python files).

## Raw Results
{results_json}

## Your Assessment
Please provide a rigorous, honest assessment addressing:

1. **Are the token savings real?** Analyze the Sonnet call counts. Is the reduction genuine?
2. **Is the methodology sound?** Are there confounding variables? Selection bias? What would make this more rigorous?
3. **Quality trade-off**: The flat config delegates to weaker models. Is there evidence the output quality suffers? (Compare output_len as a rough proxy.)
4. **Cost analysis**: Calculate the actual API cost difference. Haiku costs ~$0.80/M input + $4/M output. Sonnet CLI calls are free (subscription) but quota-limited. Is the trade-off worth it?
5. **Scalability**: Would these savings hold for harder tasks? Larger codebases? What are the limits?
6. **Verdict**: On a scale of 1-10, how confident are you that this approach provides genuine, reproducible token savings? Explain your rating.

Be critical and honest. If the experiment has flaws, say so. If the results are genuinely good, say that too.
"""


def call_claude(prompt, api_key):
    """Call Claude API (Anthropic)."""
    data = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["content"][0]["text"]


def call_openai(prompt, api_key):
    """Call GPT-4o (OpenAI)."""
    data = json.dumps({
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["choices"][0]["message"]["content"]


def call_gemini(prompt, api_key):
    """Call Gemini (Google)."""
    data = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 2000},
    }).encode()
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    return result["candidates"][0]["content"]["parts"][0]["text"]


def main():
    parser = argparse.ArgumentParser(description="Delphi round table assessment")
    parser.add_argument("--results", required=True, help="Path to ab_results.json")
    parser.add_argument("--output", default="tests/delphi_report.md")
    args = parser.parse_args()

    results = json.loads(Path(args.results).read_text())
    results_json = json.dumps(results, indent=2)
    prompt = ASSESSMENT_PROMPT.format(results_json=results_json)

    report_parts = ["# Delphi Round Table: OpenKeel Bubble Token Savings\n"]
    report_parts.append(f"**Experiment**: {results.get('tasks_run', '?')} tasks, vanilla vs flat delegation\n")
    report_parts.append(f"**Headline**: {results.get('sonnet_reduction_pct', '?')}% Sonnet call reduction, "
                       f"{results.get('oeq_saved', '?'):,} OEQ saved\n")
    report_parts.append("---\n")

    # Raw data summary
    report_parts.append("## Raw Data Summary\n")
    report_parts.append(f"| Metric | Vanilla | Flat (Bubble) |")
    report_parts.append(f"|--------|---------|---------------|")
    report_parts.append(f"| Sonnet CLI calls | {results['vanilla_sonnet_calls']} | {results['flat_sonnet_calls']} |")
    report_parts.append(f"| Haiku API calls | 0 | {results['flat_haiku_calls']} |")
    report_parts.append(f"| Haiku tokens | 0 | {results['flat_haiku_tokens']:,} |")
    report_parts.append(f"| Haiku cost | $0 | ${results['flat_haiku_cost']:.4f} |")
    report_parts.append(f"| Local LLM calls | 0 | {results['flat_local_calls']} |")
    report_parts.append(f"| OEQ burn | {results['vanilla_oeq']:,} | {results['flat_oeq']:,} |")
    report_parts.append(f"| **Sonnet reduction** | — | **{results['sonnet_reduction_pct']}%** |")
    report_parts.append(f"| **OEQ saved** | — | **{results['oeq_saved']:,}** |\n")

    # Per-task breakdown
    report_parts.append("## Per-Task Breakdown\n")
    report_parts.append("| Task | Difficulty | Vanilla Sonnet | Flat Sonnet | Reduction |")
    report_parts.append("|------|-----------|---------------|------------|-----------|")
    for r in results.get("results", []):
        v_s = r["vanilla"]["sonnet_calls"]
        f_s = r["flat"]["sonnet_calls"]
        red = r.get("sonnet_reduction_pct", 0)
        report_parts.append(f"| {r['name']} | {r['difficulty']} | {v_s} | {f_s} | {red}% |")
    report_parts.append("")

    # Assessments
    models = []

    # Claude — try env var first, then OAuth token from Claude CLI
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        creds_path = Path.home() / ".claude" / ".credentials.json"
        if creds_path.exists():
            try:
                creds = json.loads(creds_path.read_text())
                anthropic_key = creds.get("claudeAiOauth", {}).get("accessToken")
            except Exception:
                pass
    if anthropic_key:
        print("Calling Claude (Anthropic)...", file=sys.stderr)
        try:
            claude_resp = call_claude(prompt, anthropic_key)
            models.append(("Claude (Anthropic Sonnet 4)", claude_resp))
            print("  Done.", file=sys.stderr)
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
    else:
        print("ANTHROPIC_API_KEY not set, skipping Claude", file=sys.stderr)

    # GPT-4o
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        print("Calling GPT-4o (OpenAI)...", file=sys.stderr)
        try:
            gpt_resp = call_openai(prompt, openai_key)
            models.append(("GPT-4o (OpenAI)", gpt_resp))
            print("  Done.", file=sys.stderr)
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
    else:
        print("OPENAI_API_KEY not set, skipping GPT-4o", file=sys.stderr)

    # Gemini
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        print("Calling Gemini (Google)...", file=sys.stderr)
        try:
            gemini_resp = call_gemini(prompt, gemini_key)
            models.append(("Gemini 2.0 Flash (Google)", gemini_resp))
            print("  Done.", file=sys.stderr)
        except Exception as e:
            print(f"  Failed: {e}", file=sys.stderr)
    else:
        print("GEMINI_API_KEY not set, skipping Gemini", file=sys.stderr)

    # Write assessments
    for name, resp in models:
        report_parts.append(f"\n## Assessment: {name}\n")
        report_parts.append(resp)
        report_parts.append("")

    # Consensus
    if len(models) >= 2:
        report_parts.append("\n## Consensus\n")
        report_parts.append(f"{len(models)} independent AI models reviewed the experimental data above. "
                          f"Their individual assessments and confidence scores are presented as-is, unedited.\n")

    report = "\n".join(report_parts)
    Path(args.output).write_text(report)
    print(f"\nReport saved to: {args.output}", file=sys.stderr)
    print(report)


if __name__ == "__main__":
    main()
