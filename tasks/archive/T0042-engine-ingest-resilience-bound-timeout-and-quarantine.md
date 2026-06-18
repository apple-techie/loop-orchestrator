---
id: T0042
title: "F17 — engine ingest resilience: bound the ingest timeout + quarantine repeatedly-failing mailbox messages (no 10-min cycle stall, no re-hang loop)"
status: done
depends_on: []
scope: src/loop_orchestrator/engine/loop.py (_headless_ingest + its caller) and engine/config.py (ingest timeout) so a hung/failed headless ingest degrades FAST and the offending message is NOT re-ingested every subsequent cycle. Engine-only; no harness/CLI/dispatch-gate changes.
jira:
---

# T0042 — F17: ingest resilience (bound timeout + quarantine on failure)

## Objective
Make the engine's mailbox ingest resilient to a hung or failing headless-ingest call so
that (a) a stuck ingest does NOT block the decision cycle for the full 600 s, and (b) a
message that times out / fails is NOT re-attempted on every subsequent cycle. Today a
failed-ingest message stays pending in `.loop/messages/` and re-hangs the loop ~10 min
each cycle until a human moves it.

## Context you need (observed 2026-06-18, leo session)
A coord mailbox message triggered `_headless_ingest` (`engine/loop.py:248`). The headless
`claude -p` docs-lane ingest produced 0 bytes and hung to the full timeout:
  `ingest-call` (17:18:09) → `ingest-timeout` → `ingest-failed {"error":"timed out after 600s"}` (17:28:10)
The engine degraded correctly (proceeded to the brain and proposed the right dispatch), BUT
the offending message stayed in `.loop/messages/` (never moved to `processed/`), so the NEXT
cycle re-ingested it and would re-hang another 600 s — an indefinite per-cycle stall until a
human `mv`d it to `processed/`. A standalone `claude -p` was healthy (~9 s), so the defect is
the engine's ingest RESILIENCE, not ingest latency.

Code pointers:
- `src/loop_orchestrator/engine/loop.py` — `_headless_ingest` (248–286) runs the ingest via
  `run_oneshot`, which already emits `ingest-timeout` / `ingest-failed` on failure (see the
  comments at lines 258 and 286). Failure handling + quarantine belongs here or in its caller
  (the observe/cycle step that lists pending messages and invokes ingest).
- `src/loop_orchestrator/engine/config.py:40` — `timeout_s: int = 600` (the harness timeout
  currently used). Give ingest its own, shorter timeout.
- `src/loop_orchestrator/engine/improve.py:221` `_ingest_clusters` already mines
  `ingest-timeout` events — keep emitting greppable events.

## Deliverables
1. **Bound the stall.** Add a configurable `ingest_timeout_s` (separate from the brain
   `timeout_s`) defaulting materially lower than 600 s (~120 s). A hung ingest must degrade
   fast. Do NOT shorten the brain/coord timeout.
2. **Quarantine-on-failure (the core fix).** When ingest times out / fails for the pending
   message(s), MOVE those file(s) out of the pending queue so they are NOT re-ingested next
   cycle — to `.loop/messages/processed/` or a new `.loop/messages/failed/`, recording the
   failure reason + UTC timestamp (a sibling `<name>.ingest-failed.txt` or a log line). Emit a
   new `ingest-quarantined` event (keep the existing `ingest-failed`). Add-only / never delete
   a message, per the mailbox single-writer rules; idempotent.
3. **Degrade, don't abort.** The cycle must still proceed to the brain step after an ingest
   failure, exactly as today.
4. Pick the simplest correct quarantine rule and document it in the docstring: a single
   timeout/failure may quarantine immediately, or after K=2 consecutive failures for the same
   message — whichever is simpler to implement correctly and test.

## Acceptance criteria
- [ ] A test simulates an ingest that times out/fails and asserts: (a) the offending message is
  moved out of `.loop/messages/` after the failure, and (b) a second cycle does NOT re-invoke
  ingest for that message.
- [ ] The cycle still reaches the brain step after an ingest failure (no abort) — covered by test.
- [ ] Ingest timeout is configurable and defaults lower than the brain `timeout_s`.
- [ ] An `ingest-quarantined` (or equivalent) event is emitted and is greppable by `improve.py`.
- [ ] Full repo gate green: `make check` + the Python suite; no regressions.

## Verification
`uv run pytest tests/ -q` (the repo's pytest suite — `testpaths=["tests"]`) green including the
new ingest-resilience test; `make check` green. Show the new test passing and an events sample
containing `ingest-quarantined`.

## Out of scope
- WHY the headless `claude -p` ingest hangs (harness / API latency) — this task makes the engine
  RESILIENT (bounded + non-recurring); it does not speed up ingest.
- Any change to the brain/coord call path, the dispatch gate, or mailbox ack semantics beyond
  moving a failed message out of the pending queue.
