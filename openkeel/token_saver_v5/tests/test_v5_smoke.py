"""
Token Saver v5 — smoke tests.

These are the FIRST tests in the token saver beyond test_local_edit.py.
They exist to catch regressions on the critical bug fixes before those
fixes reach users. Coverage is intentionally narrow — each test pins
one behavior that v3/v4 got wrong.

Run with:
    python -m pytest openkeel/token_saver_v5/tests/ -v
    # or without pytest:
    python -m openkeel.token_saver_v5.tests.test_v5_smoke
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

# Isolate from user's real state by pointing config at tempdirs.
_TMP = tempfile.mkdtemp(prefix="token_saver_v5_test_")
os.environ["TOKEN_SAVER_DEBUG_LOG"] = str(Path(_TMP) / "debug.log")
os.environ["TOKEN_SAVER_DEFERRED_CACHE"] = str(Path(_TMP) / "deferred.json")
os.environ["TOKEN_SAVER_ERROR_STATE"] = str(Path(_TMP) / "error_state.json")
os.environ["TOKEN_SAVER_V5_DEFERRED"] = "1"
os.environ["TOKEN_SAVER_V5_ERRORLOOP"] = "1"

from openkeel.token_saver_v5 import (  # noqa: E402
    debug_log,
    deferred_context,
    error_loop,
    hook_chatter,
    json_guard,
    localedit_verify,
)
from openkeel.token_saver_v5.config import CFG, reload as reload_cfg  # noqa: E402

# Config's CFG was constructed when openkeel.token_saver_v5 was first imported
# (which may have happened before the os.environ[...] setup above ran). Re-
# read env vars now that our test fixtures are in place.
reload_cfg()


class TestJSONGuard(unittest.TestCase):
    """The #1 bug this session — bash compressor corrupting JSON."""

    def test_detects_object(self):
        self.assertEqual(json_guard.looks_structured('{"a": 1, "b": 2}'), "json")

    def test_detects_array(self):
        self.assertEqual(json_guard.looks_structured('[1, 2, 3]'), "json")

    def test_detects_nested(self):
        payload = json.dumps({"results": [{"id": 1}, {"id": 2}]})
        self.assertEqual(json_guard.looks_structured(payload), "json")

    def test_detects_html(self):
        self.assertEqual(
            json_guard.looks_structured("<!DOCTYPE html><html><body>x</body></html>"),
            "html",
        )

    def test_detects_csv(self):
        csv = "a,b,c\n1,2,3\n4,5,6\n7,8,9"
        self.assertEqual(json_guard.looks_structured(csv), "csv")

    def test_plain_text_passes(self):
        self.assertEqual(
            json_guard.looks_structured("hello this is just some output"),
            "none",
        )

    def test_bypass_flag_matches(self):
        self.assertTrue(json_guard.should_bypass_compression('{"x":1}'))
        self.assertFalse(json_guard.should_bypass_compression("hello world"))

    def test_truncated_json_still_flagged(self):
        """Truncated curl output that STARTS like JSON must not be compressed."""
        truncated = '{"results": [{"text": "hello", "score"'
        self.assertEqual(json_guard.looks_structured(truncated), "json")


class TestLocalEditVerify(unittest.TestCase):
    """The #3 bug — fake line counts, no post-write validation."""

    def test_real_diff_counts_match_reality(self):
        old = "line1\nline2\nline3\n"
        new = "line1\nline2-modified\nline3\nline4\n"
        diff, added, removed = localedit_verify.real_diff(old, new, "test.py")
        self.assertEqual(added, 2)  # modified line counted as +1, new line as +1
        self.assertEqual(removed, 1)
        self.assertIn("-line2", diff)
        self.assertIn("+line2-modified", diff)

    def test_rejects_syntax_break(self):
        path = Path(_TMP) / "broken.py"
        old = "def foo():\n    return 1\n"
        path.write_text(old)
        bak = Path(_TMP) / "broken.py.localedit.bak"
        bak.write_text(old)

        new = "def foo(:\n    return 1\n"  # syntax error
        path.write_text(new)

        result = localedit_verify.verify_edit(
            str(path), old, new, backup_path=str(bak),
        )
        self.assertFalse(result.ok)
        self.assertIn("syntax", result.reason.lower())
        self.assertTrue(result.rolled_back)
        self.assertEqual(path.read_text(), old)  # rollback succeeded

    def test_accepts_valid_py_change(self):
        old = "x = 1\n"
        new = "x = 2\n"
        result = localedit_verify.verify_edit("t.py", old, new, require_py_valid=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.lines_changed, 2)  # 1 removed + 1 added

    def test_no_op_detected(self):
        result = localedit_verify.verify_edit("t.py", "x = 1\n", "x = 1\n")
        self.assertFalse(result.ok)
        self.assertIn("no-op", result.reason)


class TestDebugLog(unittest.TestCase):
    def setUp(self):
        # Wipe the test log between cases
        if CFG.debug_log.exists():
            CFG.debug_log.unlink()

    def test_swallow_writes_line(self):
        try:
            raise ValueError("boom")
        except ValueError as e:
            debug_log.swallow("test_site", tool="Bash", error=e)

        entries = debug_log.tail(10)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["site"], "test_site")
        self.assertEqual(entries[0]["error_type"], "ValueError")
        self.assertIn("boom", entries[0]["error_msg"])

    def test_note_writes_line(self):
        debug_log.note("json_guard", "bypassed JSON", tool="Bash")
        entries = debug_log.tail(10)
        self.assertEqual(entries[-1]["level"], "note")


class TestErrorLoop(unittest.TestCase):
    def setUp(self):
        error_loop.clear()

    def test_first_two_silent(self):
        err = "Traceback ... ModuleNotFoundError: No module named 'foo'"
        self.assertIsNone(error_loop.observe("Bash", err))
        self.assertIsNone(error_loop.observe("Bash", err))

    def test_third_nudges(self):
        err = "Traceback ... ModuleNotFoundError: No module named 'foo'"
        error_loop.observe("Bash", err)
        error_loop.observe("Bash", err)
        nudge = error_loop.observe("Bash", err)
        self.assertIsNotNone(nudge)
        self.assertIn("3 times", nudge)

    def test_nudge_only_once_per_count(self):
        err = "Traceback ... ModuleNotFoundError: No module named 'bar'"
        error_loop.observe("Bash", err)
        error_loop.observe("Bash", err)
        first = error_loop.observe("Bash", err)
        self.assertIsNotNone(first)
        # 4th observation should re-nudge because count=4 > nudged_at=3
        second = error_loop.observe("Bash", err)
        self.assertIsNotNone(second)
        self.assertIn("4 times", second)

    def test_normalization_collapses_noise(self):
        """Same error with different pids / tempfiles should share fingerprint."""
        err1 = "Error: file /tmp/abc123.txt not found at pid 88421"
        err2 = "Error: file /tmp/xyz999.txt not found at pid 54333"
        fp1, _ = error_loop.fingerprint_error(err1)
        fp2, _ = error_loop.fingerprint_error(err2)
        self.assertEqual(fp1, fp2)

    def test_successful_bash_not_tracked(self):
        """Fix 2026-04-07: successful outputs were polluting the state file."""
        noise_samples = [
            "pid 2053817",
            "syntax OK\n---\n1|1775580537",
            "total 4612\ndrwxrwxr-x  2 om om    4096 Apr  7 11:55",
            '{"cpu":13.8,"disk_free_gb":54.4}',
            "Apr 7 12:49",
            "1273856",
            "error_loop_state.json\n/home/om/.openkeel/cache",
        ]
        for output in noise_samples:
            result = error_loop.observe("Bash", output)
            self.assertIsNone(result, f"false positive on: {output[:40]}")

    def test_real_failures_still_detected(self):
        """Gate must not false-negative on actual error output."""
        real_errors = [
            "Traceback (most recent call last):\n  File 'x.py', line 3\nValueError: bad",
            "bash: foo: command not found",
            "error: unknown option '--wat'",
            "fatal: not a git repository",
            "cp: cannot access '/nope': No such file or directory",
            "HTTP 404 Not Found",
            "connection refused on port 8100",
        ]
        error_loop.clear()
        for i, err in enumerate(real_errors):
            # Use unique variants so they don't share a fingerprint
            result = error_loop.observe("Bash", err + f" [{i}]")
            self.assertIsNone(result, "first observation should be silent")
        # Fire a real 3x loop
        error_loop.clear()
        for _ in range(2):
            self.assertIsNone(error_loop.observe("Bash", real_errors[0]))
        self.assertIsNotNone(error_loop.observe("Bash", real_errors[0]))

    def test_state_file_capped(self):
        """State file must not grow unboundedly."""
        error_loop.clear()
        # Generate 250 distinct fake failures
        for i in range(250):
            err = f"ValueError: bad value {i} at position X"
            error_loop.observe("Bash", err)
        state = error_loop._load_state()
        self.assertLessEqual(
            len(state.entries), error_loop.MAX_ENTRIES,
            f"state cap not enforced: {len(state.entries)} > {error_loop.MAX_ENTRIES}",
        )

    def test_different_errors_dont_cross_pollinate(self):
        e1 = "ModuleNotFoundError: No module named 'foo'"
        e2 = "FileNotFoundError: [Errno 2] No such file: '/etc/passwd'"
        fp1, _ = error_loop.fingerprint_error(e1)
        fp2, _ = error_loop.fingerprint_error(e2)
        self.assertNotEqual(fp1, fp2)


class TestDeferredContext(unittest.TestCase):
    def setUp(self):
        # Clear any leftover dumps
        for p in Path(_TMP).glob("deferred_context_*.json"):
            p.unlink()

    def test_relevant_message_gets_matching_block(self):
        blocks = [
            deferred_context.ContextBlock(
                label="token_saver",
                priority=2,
                text="Token Saver v3 intercepts Bash and Read calls, caches files, compresses outputs via qwen2.5:3b on jagg",
                keywords=["token saver", "compression", "cache"],
            ),
            deferred_context.ContextBlock(
                label="monitor_board",
                priority=3,
                text="Monitor board tracks automations — embed pipeline, chemister backend, hyphae",
                keywords=["monitor", "health", "automation"],
            ),
        ]
        deferred_context.capture("sess-1", blocks)
        out = deferred_context.score_and_emit(
            "sess-1", "how does the token saver cache work exactly?",
        )
        self.assertIsNotNone(out)
        self.assertIn("token_saver", out)
        # The irrelevant block should not dominate
        self.assertLess(out.find("monitor"), out.find("token_saver") + 500)

    def test_second_emit_is_noop(self):
        blocks = [
            deferred_context.ContextBlock("x", 3, "hello world", []),
        ]
        deferred_context.capture("sess-2", blocks)
        first = deferred_context.score_and_emit("sess-2", "tell me about hello world")
        second = deferred_context.score_and_emit("sess-2", "tell me about hello world")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_short_message_emits_nothing(self):
        blocks = [deferred_context.ContextBlock("x", 3, "rich context here", [])]
        deferred_context.capture("sess-3", blocks)
        out = deferred_context.score_and_emit("sess-3", "hi")
        self.assertIsNone(out)


class TestHookChatter(unittest.TestCase):
    def test_edit_applied_compact(self):
        msg = hook_chatter.edit_applied("/home/om/foo/bar/baz.py", 12, 1000, 1050)
        self.assertLess(len(msg), 60)
        self.assertIn("12L", msg)
        self.assertIn("+50c", msg)

    def test_bash_passthrough_labelled(self):
        msg = hook_chatter.bash_passthrough("json")
        self.assertIn("passthrough", msg)
        self.assertLess(len(msg), 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
