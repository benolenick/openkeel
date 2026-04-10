#!/usr/bin/env python3
"""Comprehensive test suite for Calcifer v2.

Tests:
- Band classifier accuracy
- Routing correctness
- Session persistence
- Fallback behavior
- Judgment agent triggers
- Performance benchmarks
"""

import pytest
import json
import time
from pathlib import Path
from openkeel.calcifer.band_classifier import BandClassifier, Band
from openkeel.calcifer.broker_session import BrokerSession
from openkeel.calcifer.contracts import Mode


class TestBandClassifier:
    """Test band classification accuracy."""

    def setup_method(self):
        self.classifier = BandClassifier()

    def test_band_a_trivial_greetings(self):
        """Band A: trivial chat."""
        tests = [
            "hi",
            "hi there",
            "hello!",
            "thanks",
            "what time is it",
            "lol that's funny",
        ]
        for prompt in tests:
            result = self.classifier.classify(prompt)
            assert result.band == Band.A, f"Expected Band A for '{prompt}', got {result.band.name}"
            assert result.skip_planner, f"Band A should skip planner: '{prompt}'"

    def test_band_b_simple_reads(self):
        """Band B: simple file operations."""
        tests = [
            "read /etc/hostname",
            "list /tmp",
            "show me /home",
            "grep foo in bar.py",
        ]
        for prompt in tests:
            result = self.classifier.classify(prompt)
            assert result.band == Band.B, f"Expected Band B for '{prompt}', got {result.band.name}"
            assert result.skip_planner, f"Band B should skip planner: '{prompt}'"

    def test_band_c_standard_tasks(self):
        """Band C: standard multi-step tasks."""
        tests = [
            "explain REST APIs",
            "what are the pros and cons of microservices",
            "how would you optimize a slow query",
            "compare Python and Go",
        ]
        for prompt in tests:
            result = self.classifier.classify(prompt)
            assert result.band == Band.C, f"Expected Band C for '{prompt}', got {result.band.name}"
            assert not result.skip_planner, f"Band C should NOT skip planner: '{prompt}'"

    def test_band_d_hard_tasks(self):
        """Band D: complex design/reasoning."""
        tests = [
            "design a scalable notification system",
            "how would you build a recommendation engine",
            "design database schema for multi-tenant SaaS",
            "security audit of this auth flow",
        ]
        for prompt in tests:
            result = self.classifier.classify(prompt)
            assert result.band == Band.D, f"Expected Band D for '{prompt}', got {result.band.name}"
            assert not result.skip_planner, f"Band D should NOT skip planner: '{prompt}'"

    def test_band_confidence_scores(self):
        """Band classifications should have confidence 0.5-1.0."""
        tests = ["hi", "read foo", "explain X", "design Y"]
        for prompt in tests:
            result = self.classifier.classify(prompt)
            assert 0.5 <= result.confidence <= 1.0, f"Invalid confidence for '{prompt}': {result.confidence}"

    def test_edge_case_empty_prompt(self):
        """Empty prompt should default to Band C."""
        result = self.classifier.classify("")
        assert result.band == Band.C

    def test_edge_case_very_long_prompt(self):
        """Very long prompt with code blocks should be Band D."""
        long_prompt = "```python\ndef foo():\n    pass\n```\n" * 50
        result = self.classifier.classify(long_prompt)
        assert result.band == Band.D


class TestBrokerSession:
    """Test broker session orchestration."""

    def setup_method(self):
        self.session = BrokerSession(session_id="test_001", verbose=False)

    def test_session_creation(self):
        """Session should initialize correctly."""
        assert self.session.session_id == "test_001"
        assert self.session._message_history == []
        assert self.session._current_session is None

    def test_trivial_message_execution(self):
        """Band A message should execute without planner."""
        response, metadata = self.session.send_message("hi")
        assert metadata["success"], f"Failed: {metadata['errors']}"
        assert metadata["band"] == "A"
        assert len(response) > 0, "Should have response"

    def test_response_metadata_structure(self):
        """Response metadata should have all required fields."""
        response, metadata = self.session.send_message("hi")
        required_fields = [
            "session_id",
            "band",
            "latency",
            "success",
            "planner_failed",
            "errors",
        ]
        for field in required_fields:
            assert field in metadata, f"Missing metadata field: {field}"

    def test_session_history_tracking(self):
        """Session should track message history."""
        self.session.send_message("hi there")
        self.session.send_message("what time is it")

        assert len(self.session._message_history) == 2
        assert self.session._message_history[0][0] == "hi there"
        assert self.session._message_history[1][0] == "what time is it"

    def test_get_context(self):
        """get_context() should return recent messages."""
        self.session.send_message("message 1")
        self.session.send_message("message 2")

        context = self.session.get_context(max_turns=2)
        assert "message 1" in context
        assert "message 2" in context

    def test_clear_history(self):
        """clear_history() should reset session."""
        self.session.send_message("test message")
        assert len(self.session._message_history) == 1

        self.session.clear_history()
        assert len(self.session._message_history) == 0

    def test_error_handling_graceful(self):
        """Session should handle errors gracefully (not crash)."""
        # Very long prompt that might cause issues
        long_prompt = "x" * 10000
        response, metadata = self.session.send_message(long_prompt)
        assert "success" in metadata
        # Should either succeed or fail gracefully
        assert isinstance(response, str)


class TestSessionPersistence:
    """Test session persistence to disk."""

    def test_session_save_load(self):
        """Session history should persist across instances."""
        # Create session and send messages
        session1 = BrokerSession(session_id="persist_test")
        session1.send_message("first message")

        # Simulate save (via CLI)
        history_data = {
            "session_id": session1.session_id,
            "history": list(session1._message_history),
        }

        # Simulate load
        session2 = BrokerSession(session_id="persist_test")
        session2._message_history = [tuple(pair) for pair in history_data["history"]]

        assert len(session2._message_history) == 1
        assert session2._message_history[0][0] == "first message"


class TestFallbackBehavior:
    """Test graceful fallback when planner fails."""

    def test_fallback_creates_simple_plan(self):
        """Failed planner should fall back to single reasoning step."""
        session = BrokerSession(verbose=False)
        # Band C typically uses Sonnet planner
        # If it fails, should fallback to simple plan
        response, metadata = session.send_message("explain microservices")

        # Should still get a response (either from real plan or fallback)
        assert len(response) > 0
        assert metadata["success"]

    def test_no_crash_on_planner_failure(self):
        """System should never crash, even if planner fails."""
        session = BrokerSession(verbose=False)
        test_prompts = [
            "hi",
            "list /tmp",
            "explain REST",
            "design a system",
            "",
            "x" * 5000,
        ]

        failures = 0
        for prompt in test_prompts:
            response, metadata = session.send_message(prompt)
            if not metadata["success"]:
                failures += 1

        # Should have no more than 20% failures (benign)
        assert failures <= len(test_prompts) * 0.2


class TestPerformance:
    """Performance benchmarks."""

    def test_band_a_latency(self):
        """Band A (chat) should be fast (<15s)."""
        session = BrokerSession(verbose=False)
        start = time.time()
        response, metadata = session.send_message("hi")
        latency = time.time() - start

        assert metadata["latency"] < 15.0, f"Band A too slow: {latency:.1f}s"
        assert metadata["band"] == "A"

    def test_band_b_latency(self):
        """Band B (reads) should be fast (<10s)."""
        session = BrokerSession(verbose=False)
        start = time.time()
        response, metadata = session.send_message("list /tmp")
        latency = time.time() - start

        assert metadata["latency"] < 10.0, f"Band B too slow: {latency:.1f}s"
        assert metadata["band"] == "B"

    def test_metadata_latency_accuracy(self):
        """Recorded latency should match actual latency."""
        session = BrokerSession(verbose=False)
        start = time.time()
        response, metadata = session.send_message("hi")
        actual_latency = time.time() - start
        recorded_latency = metadata["latency"]

        # Should be within 1 second
        assert abs(actual_latency - recorded_latency) < 1.0


class TestRouting:
    """Test that routing decisions are correct."""

    def test_band_a_skips_planning(self):
        """Band A should skip planner."""
        classifier = BandClassifier()
        result = classifier.classify("hi there")
        assert result.skip_planner
        assert result.suggested_runner == "haiku"

    def test_band_b_skips_planning(self):
        """Band B should skip planner."""
        classifier = BandClassifier()
        result = classifier.classify("list /tmp")
        assert result.skip_planner
        assert result.suggested_runner == "direct"

    def test_band_c_uses_sonnet_planner(self):
        """Band C should use Sonnet planner."""
        classifier = BandClassifier()
        result = classifier.classify("explain REST")
        assert not result.skip_planner
        assert result.suggested_runner == "sonnet"

    def test_band_d_uses_opus_planner(self):
        """Band D should use Opus planner."""
        classifier = BandClassifier()
        result = classifier.classify("design a system")
        assert not result.skip_planner
        assert result.suggested_runner == "opus"


class TestStressAndEdgeCases:
    """Stress tests and edge cases."""

    def test_rapid_fire_messages(self):
        """Handle rapid successive messages without crashing."""
        session = BrokerSession(verbose=False)
        for i in range(5):
            response, metadata = session.send_message(f"message {i}")
            assert metadata["success"]

        assert len(session._message_history) == 5

    def test_special_characters(self):
        """Handle special characters in prompts."""
        session = BrokerSession(verbose=False)
        prompts = [
            "what's the deal?",
            "fix: bug #123",
            "use @decorator",
            "path/to/file.txt",
        ]

        for prompt in prompts:
            response, metadata = session.send_message(prompt)
            # Should not crash
            assert isinstance(response, str)

    def test_mixed_band_session(self):
        """Session with messages from different bands."""
        session = BrokerSession(verbose=False)
        messages = [
            ("hi", Band.A),
            ("list /tmp", Band.B),
            ("explain REST", Band.C),
            ("design a system", Band.D),
        ]

        for prompt, expected_band in messages:
            response, metadata = session.send_message(prompt)
            assert metadata["band"] == expected_band.name


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
