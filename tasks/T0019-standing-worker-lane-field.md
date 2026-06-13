---
id: T0019
title: "standing/worker declared lane field — base-lane protection + demand-provision substrate"
status: open
depends_on: []
scope: lane-config schema + lib/lane-config-resolver.sh + loop-tmux.sh (add-lane/drop-lane) + src/loop_orchestrator/engine/ + CONTRACT.md + tests ONLY; ADDITIVE; absent field = today's base/dynamic inference; frozen lane-status output untouched; do NOT reinstall or touch running daemons; no git push
loop: harness-governance
---

# T0019 — standing/worker declared lane field

## Objective
Phase 3 (operability + continuity). Promote the standing-vs-dynamic distinction
from the implicit `@loop_lane` tmux tag to a first-class declared lane property.
A lane is either `kind: standing` (coord/ops/docs and long-lived writer agents —
never auto-dropped; the engine may dispatch/steer but never `drop_lane` them) or
`kind: worker` (dynamic, demand-provisioned, auto-retire-eligible). `coord` stays
untargetable. This is the substrate T0020's demand-provisioning policy needs.

## Context you need
Files: `lib/lane-config-resolver.sh` (role/harness/cmd already parsed — add
`kind`), `loop-tmux.sh` (add-lane sets `@loop_lane=1`; base lanes from boot
config; drop-lane refuses base lanes without `--force` at ~:368-376),
`src/loop_orchestrator/engine/{decision.py,gate.py,actions.py}` (drop_lane never
passes `--force`; add a standing-lane guard so a `drop_lane` targeting a declared
`standing` lane classifies BLOCKED), `CONTRACT.md`. Default: no `kind` = infer
base-vs-dynamic exactly as today (additive, byte-identical default).

## Deliverables
- `kind: standing|worker` lane-config field, resolved + surfaced (`list-lanes
  --json` carries it; a `@loop_lane_kind` tmux option). `drop_lane` of a standing
  lane → BLOCKED in the gate. ADDITIVE; absent field = today's inference.
- `CONTRACT.md` updated for the new field + tmux option.
- Tests for the new field + a regression check that existing configs classify
  unchanged. Before/after note appended to `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Existing tests pass
UNCHANGED. Commit per the commit policy (cite T0019). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev ruff check src tests && uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
The demand/spawn/reuse/retire POLICY (T0020) and idle auto-retirement (T0023). If
a change would alter the FROZEN lane-status output or a frozen verb
non-additively, or require a reinstall — STOP and escalate.
