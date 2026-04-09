#!/usr/bin/env python3
"""
Nova Personal — Brain module
Uses Claude CLI in pipe mode for real intelligence.
Connects to Hyphae for memory, Shallots for security, OpenKeel for governance.
"""

import subprocess
import json
import os
import time

CLAUDE_CMD = "claude"

SYSTEM_CONTEXT = """You are Nova, an AI operations assistant for Om's personal infrastructure. You manage and monitor a home lab network through a conversational interface.

ENVIRONMENT:
- kaloth (192.168.0.206): Main workstation, Linux, GTX 1050 + RTX 3070, runs OpenKeel, Hyphae
- jagg (192.168.0.224): Server, Linux, 2x RTX 3090, 152GB RAM, runs Security Shallots, Suricata, Argus, Ollama, Hyphae
- Multiple other machines on the 192.168.0.x and 192.168.2.x networks

SERVICES YOU CAN ACCESS:
- Hyphae memory (localhost:8100) — project memory, facts, decisions
- Security Shallots (jagg:8844) — security monitoring, alerts, incidents
- OpenKeel Command Board (localhost:8200) — task tracking, project management

YOUR ROLE:
- You are the interface between Om and his infrastructure
- You monitor, investigate, and recommend actions
- You NEVER execute destructive actions without explicit confirmation
- You delegate to your 4 agents for parallel investigation
- You speak concisely — short sentences, no filler

CRITICAL RULES:
- Keep responses under 30 words unless explaining something complex
- When Om gives a vague direction, generate 3-4 specific options
- Always confirm before any write/modify/delete/restart action
- Log important decisions to Hyphae
"""


class NovaBrain:
    """Claude-powered brain for Nova."""

    def __init__(self):
        self.conversation_history = []
        self.context_additions = []

    def add_context(self, context):
        """Add situational context for the next response."""
        self.context_additions.append(context)

    def think(self, user_input, max_words=30):
        """Get Nova's response via Claude CLI."""
        # Build the prompt with context
        context = SYSTEM_CONTEXT
        if self.context_additions:
            context += "\n\nCURRENT SITUATION:\n" + "\n".join(self.context_additions)
            self.context_additions = []

        # Add conversation history (last 10 exchanges)
        history = ""
        for msg in self.conversation_history[-10:]:
            history += f"\n{msg['role']}: {msg['content']}"

        prompt = f"{context}\n{history}\n\nUser: {user_input}\n\n[Respond in {max_words} words or fewer. Start with an emotion tag: [neutral] [amused] [thinking] [concerned] [impressed]]"

        try:
            result = subprocess.run(
                [CLAUDE_CMD, "-p", "--tools", ""],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=30,
            )

            response = result.stdout.strip()
            if not response or "error" in response.lower()[:20]:
                response = "[neutral] I'm here. What do you need?"

            # Parse emotion
            emotion = "neutral"
            for tag in ["amused", "thinking", "concerned", "impressed", "neutral"]:
                if f"[{tag}]" in response.lower():
                    emotion = tag
                    response = response.replace(f"[{tag}]", "").replace(f"[{tag.capitalize()}]", "").strip()
                    break

            # Update history
            self.conversation_history.append({"role": "User", "content": user_input})
            self.conversation_history.append({"role": "Nova", "content": response})

            return response, emotion

        except subprocess.TimeoutExpired:
            return "Thinking took too long. Try again?", "concerned"
        except Exception as e:
            return f"Brain error: {str(e)[:50]}", "concerned"

    def generate_options(self, user_input, situation_context):
        """Generate specific actionable options from a vague input."""
        prompt = f"""{SYSTEM_CONTEXT}

CURRENT SITUATION:
{situation_context}

The user said: "{user_input}"

Generate exactly 4 specific, actionable options that match what the user probably wants. Each option should be a concrete action, not a question.

Respond as JSON array:
[
  {{"id": "short_id", "label": "Short Label (4-6 words)", "description": "One sentence detail", "score": 10}},
  ...
]

Only respond with the JSON array, nothing else."""

        try:
            result = subprocess.run(
                [CLAUDE_CMD, "-p", "--tools", ""],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=30,
            )

            text = result.stdout.strip()
            # Find the JSON array
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                options = json.loads(text[start:end])
                return options

        except Exception as e:
            print(f"Option generation error: {e}")

        # Fallback options
        return [
            {"id": "investigate", "label": "Investigate Further", "description": "Send an agent to dig deeper", "score": 10},
            {"id": "monitor", "label": "Monitor and Wait", "description": "Keep watching for changes", "score": 8},
            {"id": "escalate", "label": "Escalate to Alert", "description": "Flag this for immediate attention", "score": 12},
            {"id": "dismiss", "label": "Dismiss", "description": "Not a concern right now", "score": 5},
        ]


async def test_brain():
    brain = NovaBrain()

    # Test basic response
    response, emotion = brain.think("How's the network looking?")
    print(f"[{emotion}] {response}")

    # Test option generation
    options = brain.generate_options(
        "investigate",
        "Suricata flagged lateral movement from 192.168.0.224. 3 IPs involved. Argus shows new processes on jagg."
    )
    print("\nGenerated options:")
    for opt in options:
        print(f"  [{opt['id']}] {opt['label']} — {opt.get('description', '')}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_brain())
