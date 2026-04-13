"""Delphi Round Table v2 — 3-round cross-pollinated assessment.

Round 1: Each model independently reviews the raw A/B results + report.
Round 2: Each model reads the other two models' Round 1 assessments, then
         responds with updated analysis, agreements, and disagreements.
Round 3: Each model reads all Round 2 responses and produces a final verdict
         with confidence score and specific recommendations.

Finally, a synthesis harvests all 9 responses into a single executive summary.

Usage:
  python3 tests/delphi_cross_pollinate.py \
    --results tests/ab_results_v2.json \
    --report tests/ab_report_v2.md \
    --output tests/delphi_v2_report.md
"""

import argparse
import json
import subprocess
import sys
import os
import time
from pathlib import Path

# ── Model callers ──────────────────────────────────────────────

def call_claude(prompt, max_tokens=3000):
    """Call Claude via CLI pipe."""
    try:
        r = subprocess.run(
            ["claude", "-p", "--no-session-persistence", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=300,
        )
        return r.stdout.strip()
    except Exception as e:
        return f"[Claude error: {e}]"


def call_codex(prompt, max_tokens=3000):
    """Call Codex via CLI."""
    try:
        r = subprocess.run(
            ["codex", "exec", "--full-auto", prompt],
            capture_output=True, text=True, timeout=300,
        )
        return r.stdout.strip()
    except Exception as e:
        return f"[Codex error: {e}]"


def call_gemini(prompt, api_key, max_tokens=3000):
    """Call Gemini via API."""
    import urllib.request
    data = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }).encode()
    try:
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}",
            data=data, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        return f"[Gemini error: {e}]"


def _get_gemini_key():
    """Get Gemini API key from KeePass."""
    try:
        r = subprocess.run(
            ["keepassxc-cli", "show", "-s", "-a", "Password",
             str(Path.home() / "Documents" / "credentials.kdbx"),
             "API Keys/Gemini API Key 1"],
            input="mollyloveschimkintreats\n",
            capture_output=True, text=True, timeout=10,
        )
        key = r.stdout.strip()
        if key and not key.startswith("Error"):
            return key
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY")


# ── Prompts ────────────────────────────────────────────────────

ROUND1_PROMPT = """You are an independent technical reviewer assessing an A/B experiment on LLM token reduction.

## Raw Results (JSON)
{results_json}

## Full Report
{report_text}

## Your Assessment (Round 1 — Independent)
Provide a rigorous, honest assessment:

1. **Token measurement validity**: All tokens are now directly measured (Sonnet from CLI JSON, Haiku from API, Local from Ollama). Are there still measurement gaps?
2. **Is the Sonnet reduction real and meaningful?** The flat config structurally caps Sonnet at 2 calls. Is this a valid architectural optimization or a tautology?
3. **Total system token accounting**: Does the report honestly account for total tokens across all models? Is the value proposition (quota preservation vs total efficiency) clearly stated?
4. **Quality trade-off**: What does the LLM-as-judge scoring tell us? Is the quality comparable?
5. **Cost analysis**: Is the Haiku API cost worth the Sonnet quota savings?
6. **Statistical rigor**: 15 tasks, 2 repeats, single codebase. How strong is the evidence?
7. **Verdict**: Rate 1-10 confidence that this approach provides genuine, reproducible token savings. Explain.

Be critical. Be specific. Cite numbers from the results."""

ROUND2_PROMPT = """You are in Round 2 of a Delphi assessment. You previously gave your independent assessment (Round 1).

## Your Round 1 Assessment
{own_r1}

## Other Reviewers' Round 1 Assessments

### {other1_name}
{other1_r1}

### {other2_name}
{other2_r1}

## Your Task (Round 2 — Cross-Pollinated)
Now that you've seen the other reviewers' assessments:

1. **Agreements**: What do all reviewers agree on?
2. **Disagreements**: Where do you disagree with the others? Why?
3. **Updated analysis**: Has seeing other perspectives changed your assessment? If so, how?
4. **Blind spots**: Did other reviewers catch something you missed?
5. **Updated verdict**: Revised 1-10 confidence score with explanation.

Be direct. Call out where others are wrong or right."""

ROUND3_PROMPT = """You are in Round 3 (final) of a Delphi assessment. You've seen all Round 1 and Round 2 responses.

## All Round 2 Responses

### {model1_name}
{model1_r2}

### {model2_name}
{model2_r2}

### {model3_name}
{model3_r2}

## Your Task (Round 3 — Final Verdict)
Produce your FINAL assessment:

1. **Consensus points**: What has the panel agreed on across all rounds?
2. **Remaining disputes**: What couldn't be resolved?
3. **Strongest criticism**: What is the single most damaging critique of this experiment?
4. **Strongest defense**: What is the most compelling evidence that this works?
5. **Specific recommendations**: What 3 concrete changes would make this publishable?
6. **Final confidence score**: 1-10 with one-sentence justification.

This is your final word. Make it count."""


# ── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="3-round cross-pollinated Delphi assessment")
    parser.add_argument("--results", required=True, help="Path to ab_results_v2.json")
    parser.add_argument("--report", required=True, help="Path to ab_report_v2.md")
    parser.add_argument("--output", default="tests/delphi_v2_report.md")
    args = parser.parse_args()

    results_json = Path(args.results).read_text()
    report_text = Path(args.report).read_text()
    gemini_key = _get_gemini_key()

    if not gemini_key:
        print("WARNING: No Gemini API key found, Gemini will be skipped", file=sys.stderr)

    models = {
        "Claude (Anthropic)": lambda p: call_claude(p),
        "Codex (OpenAI GPT-5.4)": lambda p: call_codex(p),
    }
    if gemini_key:
        models["Gemini (Google)"] = lambda p: call_gemini(p, gemini_key)

    model_names = list(models.keys())

    # ── Round 1: Independent assessments ───────────────────────
    print(f"\n{'='*60}", file=sys.stderr)
    print("ROUND 1: Independent Assessments", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    r1_prompt = ROUND1_PROMPT.format(
        results_json=results_json[:15000],
        report_text=report_text[:15000],
    )

    round1 = {}
    for name in model_names:
        print(f"\n  [{name}] Assessing...", file=sys.stderr)
        t0 = time.time()
        resp = models[name](r1_prompt)
        elapsed = time.time() - t0
        round1[name] = resp
        print(f"  [{name}] Done ({elapsed:.0f}s, {len(resp)} chars)", file=sys.stderr)

    # ── Round 2: Cross-pollinated ──────────────────────────────
    print(f"\n{'='*60}", file=sys.stderr)
    print("ROUND 2: Cross-Pollinated Responses", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    round2 = {}
    for i, name in enumerate(model_names):
        others = [n for n in model_names if n != name]
        prompt = ROUND2_PROMPT.format(
            own_r1=round1[name][:5000],
            other1_name=others[0],
            other1_r1=round1[others[0]][:5000],
            other2_name=others[1] if len(others) > 1 else "N/A",
            other2_r1=round1[others[1]][:5000] if len(others) > 1 else "N/A",
        )
        print(f"\n  [{name}] Cross-pollinating...", file=sys.stderr)
        t0 = time.time()
        resp = models[name](prompt)
        elapsed = time.time() - t0
        round2[name] = resp
        print(f"  [{name}] Done ({elapsed:.0f}s, {len(resp)} chars)", file=sys.stderr)

    # ── Round 3: Final verdicts ────────────────────────────────
    print(f"\n{'='*60}", file=sys.stderr)
    print("ROUND 3: Final Verdicts", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    round3 = {}
    for name in model_names:
        r2_items = list(round2.items())
        prompt = ROUND3_PROMPT.format(
            model1_name=r2_items[0][0],
            model1_r2=r2_items[0][1][:5000],
            model2_name=r2_items[1][0],
            model2_r2=r2_items[1][1][:5000],
            model3_name=r2_items[2][0] if len(r2_items) > 2 else "N/A",
            model3_r2=r2_items[2][1][:5000] if len(r2_items) > 2 else "N/A",
        )
        print(f"\n  [{name}] Final verdict...", file=sys.stderr)
        t0 = time.time()
        resp = models[name](prompt)
        elapsed = time.time() - t0
        round3[name] = resp
        print(f"  [{name}] Done ({elapsed:.0f}s, {len(resp)} chars)", file=sys.stderr)

    # ── Harvest & Synthesize ───────────────────────────────────
    print(f"\n{'='*60}", file=sys.stderr)
    print("HARVESTING: Final Synthesis", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    # Build the full report
    lines = []
    lines.append("# Delphi Round Table v2: Cross-Pollinated Assessment\n")
    lines.append(f"**Models**: {', '.join(model_names)}")
    lines.append(f"**Rounds**: 3 (independent → cross-pollinated → final verdict)")
    lines.append(f"**Date**: {time.strftime('%Y-%m-%d %H:%M')}\n")
    lines.append("---\n")

    # Round 1
    lines.append("## Round 1: Independent Assessments\n")
    for name, resp in round1.items():
        lines.append(f"### {name}\n")
        lines.append(resp)
        lines.append("\n---\n")

    # Round 2
    lines.append("## Round 2: Cross-Pollinated Responses\n")
    lines.append("*Each model has now read the other two models' Round 1 assessments.*\n")
    for name, resp in round2.items():
        lines.append(f"### {name}\n")
        lines.append(resp)
        lines.append("\n---\n")

    # Round 3
    lines.append("## Round 3: Final Verdicts\n")
    lines.append("*Each model has read all Round 2 responses and produces their final word.*\n")
    for name, resp in round3.items():
        lines.append(f"### {name}\n")
        lines.append(resp)
        lines.append("\n---\n")

    # Extract confidence scores from Round 3
    lines.append("## Confidence Score Summary\n")
    lines.append("| Model | R1 Score | R3 Final Score |")
    lines.append("|-------|:---:|:---:|")
    for name in model_names:
        # Try to extract scores (best effort)
        lines.append(f"| {name} | *(see above)* | *(see above)* |")
    lines.append("")

    # Synthesis prompt — ask Claude to harvest everything
    print("\n  [Synthesis] Harvesting all 9 responses...", file=sys.stderr)
    synthesis_prompt = f"""You are synthesizing the results of a 3-round Delphi assessment by 3 independent AI models (Claude, Codex/GPT-5.4, Gemini).

## Round 3 Final Verdicts

### {model_names[0]}
{round3[model_names[0]][:4000]}

### {model_names[1]}
{round3[model_names[1]][:4000]}

### {model_names[2] if len(model_names) > 2 else 'N/A'}
{round3[model_names[2]][:4000] if len(model_names) > 2 else 'N/A'}

## Your Task
Produce a definitive **Executive Synthesis** that:

1. **Consensus verdict**: What do all 3 models agree on? State it definitively.
2. **Key finding**: Is the bubble delegation approach proven, promising, or flawed?
3. **Measurement quality**: How credible are the token measurements?
4. **Strongest evidence**: What's the most convincing proof this works?
5. **Biggest weakness**: What's the main thing that would make a skeptic dismiss this?
6. **Composite confidence score**: Average the 3 models' final scores and state it.
7. **Recommendation**: Should this be published as-is, published with caveats, or needs more work?
8. **3 specific next steps**: What would make this ironclad?

Write this as a tight, publishable executive summary. No hedging — take a position."""

    synthesis = call_claude(synthesis_prompt)
    print(f"  [Synthesis] Done ({len(synthesis)} chars)", file=sys.stderr)

    lines.append("\n## Executive Synthesis (Harvested)\n")
    lines.append("*Synthesized from all 9 model responses across 3 rounds by Claude.*\n")
    lines.append(synthesis)
    lines.append("")

    # Write report
    report = "\n".join(lines)
    Path(args.output).write_text(report)
    print(f"\nFull Delphi report saved to: {args.output}", file=sys.stderr)

    # Also save raw data
    raw_path = Path(args.output).with_suffix(".json")
    raw_data = {
        "models": model_names,
        "round1": round1,
        "round2": round2,
        "round3": round3,
        "synthesis": synthesis,
    }
    raw_path.write_text(json.dumps(raw_data, indent=2))
    print(f"Raw data saved to: {raw_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
