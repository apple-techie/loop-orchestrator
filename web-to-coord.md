# web → coord

## T0036 — F11: loops-sync skips malformed task YAML
- **Task:** T0036
- **Branch:** loop/lo-janitor/web  (commit af2b121)
- **Status:** ready-for-merge
- **Summary:** `pm/taskfiles.split_task` now re-raises `yaml.YAMLError` as
  `ValueError`, so a malformed task frontmatter is skipped by all six
  ValueError-guarded callers instead of propagating out of
  `derive_loops_from_tasks` → `sync_loops_registry` and aborting the engine
  cycle; `observe.derive_loops_from_tasks` also widens its guard to
  `(ValueError, yaml.YAMLError)` for defense in depth.

### Test evidence
Both tests reproduce the live `ScannerError` crash (red) before the fix and
pass (green) after:
- `tests/test_pm_taskfiles.py::test_split_task_reraises_malformed_yaml_as_value_error`
- `tests/test_loops_sync.py::test_derive_skips_malformed_yaml_instead_of_crashing`

Full gate green in the worktree (`make check-python`): ruff check + ruff
format --check clean, **543 passed** (541 baseline + 2 new), no regressions.

### Diff
```
 ops-wiki/loops/lo-janitor.md            | 82 +++++++++++++++++++++++++++++++++
 src/loop_orchestrator/engine/observe.py |  7 ++-
 src/loop_orchestrator/pm/taskfiles.py   |  9 +++-
 tests/test_loops_sync.py                | 17 +++++++
 tests/test_pm_taskfiles.py              | 13 ++++++
 5 files changed, 126 insertions(+), 2 deletions(-)
```

### Reconciliation note for the operator (merge-time)
- `ops-wiki/loops/lo-janitor.md` did not exist on this branch (only `.gitkeep`);
  it is operator-owned and **untracked** in the main checkout. To deliver the
  required before/after note I recreated the current operator content on the
  branch and appended a `## Before/after notes` section. Reconcile with main's
  copy on promotion — and note main's untracked copy currently ends with stray
  `</content>` / `</invoke>` tags (corruption) that I did NOT propagate.

### Escalation
Per the HARD limits I did NOT merge, push, reinstall, or edit the main
checkout. The branch is left in the worktree for human review. Promotion
(merge to main + reinstall + restart) is the human gate — Andrew's call.
Next backlog item after promotion: T0037 (F10, `bin/loop-restart`).
