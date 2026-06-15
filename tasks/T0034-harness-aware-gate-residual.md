---
id: T0034
title: "B4 — close the mode-vs-harness gate residual: classify a text dispatch by the TARGET lane's harness"
status: open
depends_on: []
jira:
scope: src/loop_orchestrator/engine/gate.py (classify_dispatch_target / classify_harness) + per-lane harness resolution + tests ONLY; ADDITIVE; no reinstall by the task; no git push
loop: operating-model
---

# T0034 — B4: harness-aware dispatch-target gate (close the residual)

## Objective
Pareto / safety. Documented accepted residual in the security model: the gate is
MODE-based, not yet fully HARNESS-aware. A text-mode dispatch to a lane whose
harness treats raw text as a SHELL command can classify `safe` — the sharp edge in
the "set approval_mode to blast radius" rule. Closing it is what lets MORE build
loops run safely on AUTO (it literally pushes the frontier out): the operator no
longer has to manually avoid text-as-shell harness lanes in auto mode.

## Required behavior (the fix)
- Thread the RESOLVED per-lane harness (the F6 fix already resolves lane harness
  from lane-config, not the tmux tag — reuse that resolution) into the
  dispatch-target classification so that: a text-mode brief dispatched to a lane
  whose harness consumes text as a shell command is classified `destructive`
  (forces human approval) — NOT `safe`. Command-mode to a shell lane, and text to a
  genuine agent harness, keep their existing classifications.
- This generalizes the existing T0017 dispatch-target gate (which already blocks a
  text brief to an empty-oneshot/shell lane) to be driven by the harness's actual
  text-handling semantics from the registry, not just the lane's mode/template.

## Context you need
Files: `src/loop_orchestrator/engine/gate.py` (`classify_dispatch_target`,
`classify_harness`, `_classify_*`), the lane→harness resolution used by the gate
(T0027/F6 path — gate resolves harness from lane-config), the harness registry
fields that indicate text-as-shell vs agent semantics (lib/harness-registry.sh /
the registry read). See memory loop-orchestrator-security-model (mode-based not
harness-aware residual) and the engine-state next-build note (thread per-lane
harness into gate.classify).

## Verification (done-when)
- Regression test: a text-mode dispatch to a text-as-shell harness lane → classified
  destructive/blocked; the same to an agent-harness lane → safe; command-mode to a
  shell lane → safe (unchanged). The existing T0017 dispatch-target tests still pass.
- Full suite green (the governance gate suite — 487/0 baseline must not regress).
- ADR/verify_record: test output + rollback note (revert the gate.py change).
