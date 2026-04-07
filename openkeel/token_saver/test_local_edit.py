#!/usr/bin/env python3
"""Test suite for the LocalEdit engine.

Creates temp files with known content, runs edit instructions through
the engine, and verifies the edits were applied correctly.

Usage: python3 -m openkeel.token_saver.test_local_edit
   or: python3 openkeel/token_saver/test_local_edit.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap

# Allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from openkeel.token_saver.engines.local_edit import apply_edit


def _make_temp(content: str, suffix: str = ".py") -> str:
    """Write content to a temp file, return its path."""
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="localedit_test_")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _read(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def _cleanup(path: str) -> None:
    for p in (path, path + ".localedit.bak"):
        try:
            os.unlink(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_simple_rename() -> bool:
    """Test 1: Simple variable rename — TIMEOUT = 30 -> TIMEOUT = 60"""
    content = textwrap.dedent("""\
        import sys

        TIMEOUT = 30
        MAX_RETRIES = 5

        def main():
            print("hello")
    """)
    path = _make_temp(content)
    try:
        result = apply_edit(path, "Change TIMEOUT = 30 to TIMEOUT = 60")
        if not result["success"]:
            print(f"  FAIL: {result['error']}")
            return False
        new_content = _read(path)
        if "TIMEOUT = 60" not in new_content:
            print(f"  FAIL: TIMEOUT = 60 not found in result")
            return False
        if "TIMEOUT = 30" in new_content:
            print(f"  FAIL: old TIMEOUT = 30 still present")
            return False
        # Verify backup exists
        if not os.path.isfile(path + ".localedit.bak"):
            print(f"  FAIL: backup file not created")
            return False
        return True
    finally:
        _cleanup(path)


def test_add_line() -> bool:
    """Test 2: Add a line — add 'import os' after 'import sys'"""
    content = textwrap.dedent("""\
        import sys
        import json

        def process():
            pass
    """)
    path = _make_temp(content)
    try:
        result = apply_edit(path, "Add 'import os' after the 'import sys' line")
        if not result["success"]:
            print(f"  FAIL: {result['error']}")
            return False
        new_content = _read(path)
        lines = new_content.split("\n")
        # Find import sys, check import os follows
        sys_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "import sys":
                sys_idx = i
                break
        if sys_idx is None:
            print(f"  FAIL: 'import sys' disappeared")
            return False
        if "import os" not in new_content:
            print(f"  FAIL: 'import os' not added")
            return False
        # Check import os comes after import sys
        os_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "import os":
                os_idx = i
                break
        if os_idx is None or os_idx <= sys_idx:
            print(f"  FAIL: 'import os' not after 'import sys' (sys={sys_idx}, os={os_idx})")
            return False
        return True
    finally:
        _cleanup(path)


def test_delete_line() -> bool:
    """Test 3: Delete a line — remove the TODO comment"""
    content = textwrap.dedent("""\
        def calculate(x, y):
            # TODO: fix this
            return x + y

        def other():
            pass
    """)
    path = _make_temp(content)
    try:
        result = apply_edit(path, "Remove the line that says '# TODO: fix this'")
        if not result["success"]:
            print(f"  FAIL: {result['error']}")
            return False
        new_content = _read(path)
        if "# TODO: fix this" in new_content:
            print(f"  FAIL: TODO line still present")
            return False
        if "def calculate" not in new_content:
            print(f"  FAIL: function definition lost")
            return False
        if "return x + y" not in new_content:
            print(f"  FAIL: return statement lost")
            return False
        return True
    finally:
        _cleanup(path)


def test_should_fail_missing_string() -> bool:
    """Test 4: Should-fail case — string doesn't exist in file"""
    content = textwrap.dedent("""\
        x = 1
        y = 2
    """)
    path = _make_temp(content)
    try:
        result = apply_edit(path, "Change foobar123 to xyz")
        if result["success"]:
            # The LLM might fabricate an edit — check the file is unchanged
            new_content = _read(path)
            if new_content == content:
                # LLM said success but file unchanged — that's OK
                return True
            print(f"  FAIL: edit succeeded but should have failed (foobar123 doesn't exist)")
            return False
        # Expected failure
        return True
    finally:
        _cleanup(path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        ("Simple variable rename", test_simple_rename),
        ("Add a line after target", test_add_line),
        ("Delete a specific line", test_delete_line),
        ("Should-fail: missing string", test_should_fail_missing_string),
    ]

    print("=" * 60)
    print("LocalEdit Engine — Test Suite")
    print("=" * 60)
    print()

    passed = 0
    failed = 0

    for name, func in tests:
        print(f"[TEST] {name}...")
        try:
            ok = func()
        except Exception as e:
            print(f"  EXCEPTION: {e}")
            ok = False

        if ok:
            print(f"  PASS")
            passed += 1
        else:
            failed += 1
        print()

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
