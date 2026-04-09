#!/usr/bin/env python3
"""SemanticRunner: local Ollama (gemma, qwen) for reasoning that doesn't need cloud."""

import json
import urllib.request
from openkeel.calcifer.contracts import StepSpec, StatusPacket


class SemanticRunner:
    """Execute via local Ollama models."""

    def __init__(self, host: str = "http://127.0.0.1:11434"):
        self.host = host

    def execute(self, step: StepSpec) -> StatusPacket:
        """Run a step on local Ollama."""
        model = step.inputs.get("model", "gemma4:e2b")
        prompt = step.inputs.get("prompt", "")

        try:
            response = self._call_ollama(model, prompt)
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=[f"queried {model}"],
                artifacts_touched=[],
                result_summary=response,
                acceptance_checks=[("ollama_responded", True, f"{model} ok")],
                runner_id="semantic",
                cost_units=0.1,
            )
        except Exception as e:
            return StatusPacket(
                step_id=step.step_id,
                objective=step.task_class,
                actions_taken=[f"failed to query {model}"],
                artifacts_touched=[],
                result_summary=f"Error: {e}",
                acceptance_checks=[("ollama_responded", False, str(e))],
                needs_escalation=True,
                runner_id="semantic",
                cost_units=0.0,
            )

    def _call_ollama(self, model: str, prompt: str) -> str:
        """Call Ollama API."""
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data.get("response", "").strip()
