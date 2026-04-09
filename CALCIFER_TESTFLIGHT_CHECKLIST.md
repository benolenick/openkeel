# Calcifer v2 Test Flight Checklist

Run these tests systematically to validate production readiness.

## Band Classification Tests

- [ ] **Band A: Chat** — Message should skip planner, respond fast
  ```bash
  calcifer "hi there"
  calcifer "what time is it"
  calcifer "thanks for that"
  ```
  ✓ Expected: <15s latency, Band A classified, no planner call

- [ ] **Band B: Simple Reads** — Should skip planner, use Direct runner
  ```bash
  calcifer "read /etc/hostname"
  calcifer "list /tmp"
  calcifer "grep TODO in /home/user/projects/*"
  ```
  ✓ Expected: <10s latency, Band B classified, no planner call

- [ ] **Band C: Standard** — Should use Sonnet planner
  ```bash
  calcifer "explain REST APIs"
  calcifer "what's the difference between SQL and NoSQL"
  calcifer "how do you optimize database queries"
  ```
  ✓ Expected: 30-60s, Band C, Sonnet planner used

- [ ] **Band D: Hard** — Should use Opus planner
  ```bash
  calcifer "design a message queue for 1M messages/sec"
  calcifer "what security vulnerabilities exist in JWT"
  calcifer "design a system to handle distributed tracing"
  ```
  ✓ Expected: 60-120s, Band D, Opus planner used

## Session Persistence Tests

- [ ] **Multi-turn context** — Session should remember prior messages
  ```bash
  calcifer --session demo "what is a REST API"
  calcifer --session demo "now explain status codes"
  calcifer --session demo "what about headers"
  ```
  ✓ Expected: Each message references prior context

- [ ] **Session file saved** — Check `~/.calcifer/sessions/demo.json` exists
  ```bash
  ls -la ~/.calcifer/sessions/demo.json
  cat ~/.calcifer/sessions/demo.json | jq .
  ```
  ✓ Expected: File exists with full conversation history

- [ ] **Fresh session** — New session should have no prior context
  ```bash
  calcifer --new "explain microservices"
  ```
  ✓ Expected: Band C, no prior context

## Edge Cases & Robustness

- [ ] **Empty prompt** — Should default to Band C gracefully
  ```bash
  calcifer ""
  ```
  ✓ Expected: No crash, reasonable response

- [ ] **Very long prompt** — Should handle large inputs
  ```bash
  python3 -c "print('x' * 5000)" | calcifer
  ```
  ✓ Expected: No crash, processes normally

- [ ] **Special characters** — Handle punctuation/symbols
  ```bash
  calcifer "what's the deal with @decorator syntax?"
  calcifer "fix: bug #123 in /path/to/file.py"
  ```
  ✓ Expected: No parsing errors

- [ ] **Rapid messages** — Handle concurrent requests
  ```bash
  for i in {1..5}; do
    calcifer "message $i" &
  done
  wait
  ```
  ✓ Expected: All complete successfully, no conflicts

## JSON Output Tests

- [ ] **JSON mode** — Structured output for integration
  ```bash
  calcifer --json "explain REST"
  ```
  ✓ Expected: Valid JSON with response + metadata fields

- [ ] **JSON with context** — Include conversation history
  ```bash
  calcifer --json --context "explain REST"
  ```
  ✓ Expected: JSON includes `history` array

## Performance Benchmarks

- [ ] **Band A latency** — Chat should be <15s
  - Run 3 times, record latency
  - Expected: avg <15s, consistent

- [ ] **Band B latency** — Reads should be <10s
  - Run 3 times, record latency
  - Expected: avg <10s, very consistent

- [ ] **Band C latency** — Standard should be 30-60s
  - Run 2 times (expensive)
  - Expected: 30-60s range

- [ ] **Band D latency** — Hard should be 60-120s
  - Run 1 time (very expensive)
  - Expected: 60-120s range

## Error Handling & Fallback

- [ ] **Planner failure fallback** — If planner times out, fallback works
  - Watch logs for "Sonnet planning failed"
  - ✓ Expected: Still gets response (via fallback)

- [ ] **No crash on bad input** — Invalid prompts should be handled
  ```bash
  calcifer "$(printf '\x00\x01\x02')"  # null bytes
  calcifer "\\"*&^%$"                   # special chars
  ```
  ✓ Expected: Graceful handling, no crash

## Logging & Tracing

- [ ] **Live logging enabled** — Logs visible in real-time
  ```bash
  tail -f /tmp/calcifer_logs/calcifer.log &
  calcifer "test message"
  ```
  ✓ Expected: Message flow visible in logs (band, planner, executor)

- [ ] **Verbose mode** — Shows routing decisions
  ```bash
  calcifer --verbose "test"
  ```
  ✓ Expected: [0] Band classified, [2] Planner call, [3] Execution, etc.

## Integration Tests

- [ ] **Ladder GUI** — Messages in GUI should work
  - Open Ladder chat window
  - Send message: "hi there"
  - ✓ Expected: Instant response, Band A classification

- [ ] **CLI from Ladder** — Can run CLI while GUI is open
  - GUI: send "explain REST"
  - Terminal: `calcifer "what is JSON"`
  - ✓ Expected: Both complete successfully, no conflicts

## Stress & Soak Tests

- [ ] **10 rapid messages** — No memory leaks, all complete
  ```bash
  ./calcifer_live_test.sh  # Choose option 7
  ```
  ✓ Expected: All 10 complete, memory stable

- [ ] **Long session** — 20+ messages in same session
  ```bash
  calcifer --session soak "msg 1"
  calcifer --session soak "msg 2"
  # ... repeat 18 more times
  ```
  ✓ Expected: Context grows, no slowdown

## Judgment Agent Test

- [ ] **Trigger escalation** — Force a step to fail, see judgment kick in
  - This is hard to test without breaking something intentionally
  - Log should show `[4.X] → OpusJudgmentAgent.judge()`
  - ✓ Expected: When it triggers, decision is logged

## Final Validation

- [ ] **All bands working** — A, B, C, D tested and verified
- [ ] **Session persistence** — Multi-turn context works
- [ ] **No crashes** — Tested edge cases, robustness good
- [ ] **Logging clear** — Can trace every decision
- [ ] **Performance acceptable** — Latency within bounds
- [ ] **Fallback solid** — System doesn't break on planner failure

## Sign-Off

Once all tests pass:

```bash
git log --oneline | head -5
git status
```

Then:
```bash
echo "✓ Calcifer v2 Test Flight PASSED" > /tmp/TESTFLIGHT_PASSED
date >> /tmp/TESTFLIGHT_PASSED
```

---

**Ready to ship.**
