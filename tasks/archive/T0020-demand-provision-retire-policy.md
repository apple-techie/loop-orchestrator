---
id: T0020
title: "demand-provision-reuse-retire HarnessPolicy + HARD reuse-before-spawn gate"
status: done
accepted: 2026-06-14
gate: "440/0"
commit: 2ee52d7
depends_on: [T0019]
scope: src/loop_orchestrator/engine/{config.py,gate.py,loop.py} + contracts/checkpoint-header.md + ooLEO/govern lane-config (starter policy + role-vocab) + tests ONLY; ADDITIVE; empty policy = today; frozen status untouched; no reinstall; no git push
loop: harness-governance
---

# T0020 — demand→provision→reuse→retire policy (HARD gate)

## Objective
Phase 3. The missing piece: make WHICH agent fills WHICH lane a governed,
demand-driven decision instead of a free brain call. Extend `HarnessPolicy`
(config.py) with, per role: `preferred_harness`, a fallback chain, `spawn_when`
(an unclaimed brief exists for the role AND no live idle worker of that role),
and `retire_after_idle_cycles`. Operator decision (settled): enforce as a HARD
gate rule — an `add_lane` for a role that already has an idle worker lane
classifies DESTRUCTIVE (prefer reuse), overridable per role by a
`concurrency_allowance`. Also unify the role vocabulary so `role_tag_map`/
`role_defaults` bind to the real ooLEO roles (routes-and-flows / data-integration
/ design-system / build-checks / dev-server / log-tail / synthesis), and ship a
starter `engine.harness_policy` in the ooLEO + govern lane-configs so T0017's
dispatch-target gate (inert today behind the empty policy) goes LIVE.

## Context you need
Files: `engine/config.py` (`HarnessPolicy` dataclass ~:71-105), `engine/gate.py`
(`classify_harness` + `govern_add_lanes` — the pure pass above the shape rules),
`engine/loop.py` (resolves roster + lane_harnesses per cycle — thread the
idle-worker set in), `contracts/checkpoint-header.md` (tell the brain
reuse-before-spawn), the ooLEO + govern `lane-config.yaml` (role names + add a
`harness_policy` block). Builds on T0019's standing/worker `kind`. Empty policy /
`roster=None` = today's behavior (the short-circuit must stay byte-identical).

## Deliverables
- `HarnessPolicy` role rules (preferred / fallback / spawn_when /
  retire_after_idle_cycles / concurrency_allowance). Gate: reuse-before-spawn
  HARD rule. Role-vocab unify (map ooLEO roles). Starter `harness_policy` in
  ooLEO + govern lane-config.
- `checkpoint-header.md`: guidance to provision against declared demand, reuse
  by default, never spawn a duplicate idle worker.
- Tests (the reuse-before-spawn DESTRUCTIVE case + an empty-policy no-op
  regression). Before/after note in `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Existing tests pass
UNCHANGED. Commit per the commit policy (cite T0020). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev ruff check src tests && uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Idle auto-RETIREMENT execution (the engine actually dropping an idle worker) —
that ties to T0023's idle-marker trust; this task only DECLARES `retire_after`
and surfaces a retire-candidate flag. Auto-dropping a lane, or anything altering
the frozen status output non-additively → STOP and escalate.
