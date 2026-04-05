"""Token Saver — reduce Claude Code cloud token usage via local LLM pre-processing.

Intercepts tool calls, caches file reads, summarizes large content via a local
Ollama model, and tracks savings in a ledger.
"""

__version__ = "0.1.0"
