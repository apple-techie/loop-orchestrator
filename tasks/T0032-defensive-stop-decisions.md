---
id: T0032
title: "B2 — defensive stop: a brain stop on a healthy-looking fleet re-probes readiness once before executing"
status: open
depends_on: []
jira:
scope: src/loop_orchestrator/engine/ (stop-action handling in loop.py + watch.py; possibly a small readiness re-probe via substrate) + tests ONLY; ADDITIVE; no reinstall by the task; no git push
loop: operating-model
---

# T0032 — B2: defensive stop decisions (re-probe on healthy fleet)

## Objective
Continuity. The scariest silent unattended failure is the IDLE-STALL STOP: the
bash lane-status classifier is a heuristic shared across harnesses; when a
harness's idle chrome matches another's "working" marker, every idle lane reads
busy → the working→idle transition never fires → the engine stalls → the brain
sees a perpetually-busy fleet and decides `stop`. The fleet is actually idle; the
loop dies quietly. (Fixed once in loop-lane-status.sh after fddafaa; T0015
harness-aware readiness is the real fix, but any new harness chrome can re-trip
the shared classifier — so the STOP path itself must be defensive.)

## Required behavior (the fix)
- Before EXECUTING a brain `stop` action, if any lane shows recent activity
  inconsistent with "everything idle, nothing to do" (e.g. a lane that transitioned
  to working within the last cycle, or a lane currently classified working), the
  engine RE-PROBES lane readiness once (fresh `loop-lane-status`/observe) before
  honoring the stop.
- If the re-probe shows the fleet is NOT genuinely idle (a lane is actually
  working), do NOT execute the stop — treat it as a suspected idle-stall: emit a
  distinct event (e.g. `stop-suspected-idle-stall`) and either skip/no-op the
  cycle or escalate, rather than halting the loop.
- A genuine stop (fleet really idle, nothing pending) still executes normally.

## Context you need
Files: `src/loop_orchestrator/engine/loop.py` (where a `stop` action is executed /
classified), `watch.py` (cycle outcome), `substrate.py` (lane_status_all re-probe).
The decision contract `stop` action lives in decision.py/gate.py. See the
loop-orchestrator-engine-state GOTCHA block (esc-to-interrupt idle footer) for the
exact failure mode the re-probe must catch.

## Verification (done-when)
- Unit test: a `stop` decision while a lane shows recent working activity →
  re-probe is invoked; if re-probe says working, stop is NOT executed (event
  emitted); if re-probe confirms idle, stop executes.
- A scripted/forced idle-stall scenario (lane appears working via stale marker but
  is idle vs genuinely working) demonstrates the two branches.
- ADR/verify_record: test output green + rollback note (revert the loop.py guard).
