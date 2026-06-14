---
id: T0027
title: "F6 — gate resolves lane harness from lane-config fallback (per-lane, not per-window tmux tag)"
status: open
depends_on: []
scope: src/loop_orchestrator/engine/{loop.py,gate.py} + substrate.py + tests ONLY; ADDITIVE; must not change behavior for correctly-tagged single-pane sessions; no reinstall; no git push
loop: harness-governance
---

# T0027 — F6: gate harness resolution must not depend on tmux tags

## Objective
Phase 3/4 hardening. **F6 (found live activating the harness_policy):** the gate
resolves each lane's harness ONLY from the `@loop_lane_harness` tmux WINDOW tag
(`loop.py:460` `{info.window: info.harness for info in lane_infos if info.harness}`
← `loop-tmux list-lanes` ← `tmux show-options -w @loop_lane_harness`). Two live
failures:
1. **Pre-existing sessions lack the tag.** `loop-tmux` only sets it at boot
   (`loop-tmux.sh:344`); sessions booted before that code have untagged base
   lanes → `list-lanes` returns `harness=""` → the lane is absent from
   `lane_harnesses` → a dispatch to it (even the claude `web` lane) classifies
   **BLOCKED**. Activating `harness_policy` on the live govern + leo daemons
   stalled their builds for exactly this reason (worked around by manually
   `tmux set-option -w @loop_lane_harness` on each window).
2. **Multi-pane windows can't carry per-lane harness.** leo's `validate` window
   holds validate-left (claude) + validate-right (shell); a single window tag
   can't represent both, so per-window resolution is wrong for mixed windows.

## Required behavior (the fix)
Resolve each lane's harness from the **lane-config (authoritative)** as the
primary/fallback source, keyed **per lane** — not solely from the per-window
tmux tag. The tmux tag may remain an override/fast-path, but a missing tag must
fall back to the configured harness so `harness_policy` is safe to activate on
ANY session without manual tagging, and mixed-harness windows resolve per lane.
Keep behavior identical for correctly-tagged single-pane sessions.

## Context you need
Files: `engine/loop.py:443-460` (builds `lane_harnesses`), `engine/gate.py`
(`classify_harness` + `_classify_dispatch_target` consume it — BLOCKED when the
target lane's harness is unknown/not-in-roster), `substrate.py:141` (`list_lanes`
reads the tmux tag). The lane-config is already loaded (`load_config`) and has
the authoritative per-lane harness. `loop-tmux.sh:344` is the boot-time tagger.

## Deliverables
- Per-lane harness resolution with lane-config fallback (tmux tag optional).
  Mixed-harness windows resolve correctly per lane.
- Tests: untagged base lanes resolve from config (dispatch to a config-claude
  lane is NOT blocked); a mixed claude+shell window resolves each lane correctly;
  correctly-tagged single-pane sessions unchanged. Before/after note in
  `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Commit per the commit
policy (cite T0027 / F6). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Re-tagging running sessions (already done manually). If config-fallback can't
disambiguate a lane (no config + no tag), keep today's safe default (treat as
unknown) and escalate rather than guess an agent harness.
