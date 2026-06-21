---
id: T0066
title: "Cost-metric accuracy fixes (follow-up to T0062 redesign)"
status: review
loop: code2
depends_on: []
scope: scripts/loop-metrics.sh cost aggregation, src/loop_orchestrator/engine/brain.py stream usage emission, and tests.
jira:
---

# T0066 - cost-metric accuracy

## Objective
Make the T0062 cost-axis metrics trustworthy for cost-per-shipped-unit and
cost-per-decision reporting.

## Context you need
T0062 verification left cost-metric accuracy residuals:

1. A failed brain turn must not null the whole 7 day token and cost window or
   hide spend from other attempts.
2. Stream usage with tokens but no reliable provider price must emit
   `cost_source="unpriced"` with `cost_usd: null`, not silently read as zero.
3. The fleet aggregate from `scripts/loop-metrics.sh --all` needs test coverage
   for cost aggregation.
4. `cost_per_decision` must use a documented denominator.
5. Failed retry attempts can incur spend and should record token/cost usage when
   stream-json reports it.
6. Float-encoded token counts like `100.0` should be accepted.
7. The cost numerator and decision denominator must use the same 7 day event
   window.
8. Multi-model stream-json turns should keep their model label.

## Deliverables
- Update `scripts/loop-metrics.sh` so missing failed-turn usage is skipped with a
  note instead of poisoning the whole window.
- Update `scripts/loop-metrics.sh` so `cost_per_decision` divides by total
  `decision-approved` plus `decision-rejected` events in the same 7 day event
  window.
- Update `src/loop_orchestrator/engine/brain.py` so stream-json usage records
  distinguish `provider`, `unpriced`, and `unavailable` cost sources.
- Record stream-json usage for failed brain retry attempts when usage is
  available.
- Add regression tests for the single-session and `--all` metric paths plus
  brain stream-json usage behavior.

## Acceptance criteria
- A failed or missing brain usage event no longer makes `brain_tokens_7d`,
  `cost_usd_7d`, or derived cost metrics `n/a` when other priced usage exists.
- Unknown or missing-price stream usage emits `cost_source="unpriced"` and
  `cost_usd: null`.
- `scripts/loop-metrics.sh --all` has tested token and cost aggregation.
- `cost_per_decision` uses the documented total-decision denominator.
- Failed retry attempts emit `brain-usage` when stream-json usage is available.
- Float token values and multi-model labels are preserved correctly.

## Verification
- Run `uv run --no-sync --group dev pytest -q tests/test_metrics_script.py tests/test_brain.py`.
- Run `make check-all`.

## Out of scope
- Do not add a local model pricing table.
- Do not change the metrics log schema beyond the documented denominator and
  existing event fields.
- Do not alter merge, deployment, Jira sync, or lane scheduling behavior.
