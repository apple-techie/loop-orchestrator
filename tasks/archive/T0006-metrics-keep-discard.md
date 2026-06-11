---
id: T0006
title: Metrics + keep/discard gate in log.md
status: done
depends_on: [T0002, T0003]
scope: one metrics script + AGENTS.md experiment protocol
---

# T0006 — Coordinator-efficiency metrics and the keep/discard gate

## Objective
Make every schema/workflow change to the orchestration system falsifiable.
Track coordinator efficiency per checkpoint cycle and adopt an explicit
keep/discard rule: a change to AGENTS.md or the wiki workflow is kept only if
the numbers do not regress.

## Context you need
- Checkpoint prompt size is printed by scripts/loop-checkpoint.sh (T0003):
  bytes and approx tokens (bytes/4).
- Lane restarts: `.loop/sessions/<session>/lane-restarts.jsonl` (one JSON per
  line; "giving-up" events included).
- log.md entries are greppable: `grep "^## \[" ops-wiki/log.md`.
- Loop cycle timing source: `updated_at` transitions in
  `.loop/orchestrator-state.json` per loop status change, as mirrored into
  loop pages by the T0002 ingest.

## Deliverables
1. `scripts/loop-metrics.sh` printing one summary block and, with `--log`,
   appending `## [date] metrics | <one-line summary>` to ops-wiki/log.md:
   - checkpoint_tokens: latest `loop-checkpoint.sh --print | wc -c` / 4.
   - pending_messages: from `loop-wiki-pending.sh --quiet`.
   - restarts_24h and giveups_24h: parsed from lane-restarts.jsonl
     (python3 one-liner acceptable; the repo already requires python3).
   - ingests_7d / lints_7d / checkpoints_7d: counted from log.md prefixes.
   - dispatch counts are OUT of scope unless trivially derivable; note as n/a.
2. AGENTS.md `### Experiment protocol`:
   - Any change to AGENTS.md rules, ingest protocol, or checkpoint assembly is
     an EXPERIMENT: log `## [date] experiment | <change>` when applied, run
     normally for >= 3 checkpoint cycles, then compare loop-metrics output
     before/after. Keep if checkpoint_tokens and restarts did not regress and
     pending_messages does not trend up; otherwise revert and log
     `## [date] experiment | reverted: <change>`.
   - Metrics are recorded by `loop-metrics.sh --log` at the end of every lint
     run and at least daily.

## Acceptance criteria
- Script runs cleanly with missing inputs (absent jsonl => 0 with a note,
  no crash). `--log` appends exactly one correctly-prefixed line.
- AGENTS.md contains the experiment protocol with the 3-cycle rule.

## Verification
```
bash -n scripts/loop-metrics.sh
scripts/loop-metrics.sh            # full block, no errors
scripts/loop-metrics.sh --log && grep '| metrics' ops-wiki/log.md | tail -1
```

## Out of scope
Dashboards, per-harness token accounting, cost tracking.
