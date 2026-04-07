# LocalEdit Engine — Code Review

**Reviewer:** Claude (critic role)
**Date:** 2026-04-06
**Files reviewed:** `local_edit.py`, `test_local_edit.py`

## What's Good

- Backup before edit — solid safety net
- Exact-once match validation (count == 1) prevents ambiguous edits
- JSON extraction with fallback for markdown fences — handles LLM messiness
- Clean error reporting via result dict
- Temperature 0.0 + thinking disabled — right choices for deterministic edits

## Critical Issues

### 1. `_build_context` is a no-op (lines 87-92)
For files >500 lines it still sends the entire file content. Gemma 4's context window will choke on large files, or worse, silently truncate and return an `old_string` from the wrong section.

**Fix:** Implement keyword-based windowing — extract ~200 lines around the instruction's target using grep/keyword matching. Send line numbers so the model knows where it is.

### 2. No retry loop
If Gemma returns malformed JSON or a wrong `old_string`, it just fails. One bad response = total failure.

**Fix:** Add at least one retry. Feed the error back to the model: "Your old_string wasn't found. Here are the lines around keyword X — try again."

### 3. Whole-file string replacement (line 224)
Uses `content.replace(old_string, new_string, 1)`. Even though count==1 is validated, this is string replacement not line-aware. Trailing whitespace differences (tabs vs spaces, \r\n vs \n) will cause silent match failures.

**Fix:** Consider normalizing whitespace for matching, or doing line-by-line comparison. At minimum, strip trailing whitespace from both old_string and the file content before matching.

### 4. No syntax validation after edit
It writes the file and calls it done. A broken edit (missing colon, unmatched bracket, bad indentation) gets applied silently.

**Fix:** For .py files, run `py_compile.compile(path, doraise=True)` after writing. If it fails, auto-restore from backup. For other file types, consider basic bracket/brace matching.

### 5. 45-second timeout is tight
At 80°C with thermal throttling, the 3070 slows inference significantly. Large files with 500+ lines of context will take longer.

**Fix:** Default to 60s minimum. Better: scale timeout with file size (e.g., `max(60, line_count // 10)`).

### 6. Silent failure in `_ollama_chat` (line 57)
Catches all exceptions and returns `""`. Can't distinguish "Ollama is down" from "Gemma OOM'd" from "network timeout."

**Fix:** Log the exception type and message. Return a structured error or at least include the exception in the result dict so the caller can report it.

### 7. Test suite doesn't cover the hard cases
Current tests: simple rename, add line, delete line, missing string. These are trivial.

**Missing tests:**
- Multi-line edit (replace a 5-line block)
- Indentation-sensitive edit (add an `if` block inside a function body)
- File with duplicate-looking code (two similar functions, edit only one)
- Unicode content
- File >500 lines (to exercise the context windowing — currently broken)
- Edit that produces invalid Python (should be caught by syntax validation)

### 8. No integration wiring
The engine exists in isolation. There was no hook integration initially — the CLAUDE.md `#LOCALEDIT:` prefix convention was added separately but the pre_tool hook needs to intercept it and route to `apply_edit()`.

**Status:** The other agent has added a `#LOCALEDIT:` prefix convention in CLAUDE.md. Need to verify the pre_tool hook actually intercepts this and calls `apply_edit()`.

## Architecture Observations

- The routing decision (Gemma vs Claude) is currently manual via `#LOCALEDIT:` prefix. This is actually the simplest and most reliable approach — no classifier needed.
- The "always verify the diff" note in CLAUDE.md is important. Gemma 4 is a small model and will occasionally produce wrong edits. The human-in-the-loop is the real safety net.
- Consider adding an undo command (`#LOCALEDIT_UNDO: /path/to/file.py`) that restores from `.localedit.bak`.
