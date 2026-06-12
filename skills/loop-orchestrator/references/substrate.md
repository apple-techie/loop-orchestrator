# Substrate reference (bash layer)

Everything here needs only bash ≥ 3.2, tmux ≥ 3.3, python3 (PyYAML only for
`--lane-config`). Install: `make install` symlinks the five root CLIs into
`~/.local/bin`. The frozen surface contract is the repo's `CONTRACT.md` —
every JSON payload carries `"contract_version": 1`, additive-only changes.

## loop-tmux — sessions and lanes

```bash
loop-tmux --project NAME --project-root PATH [--infra-root PATH] \
          [--preset pi-claude|all-pi|all-claude|validation-only|monitor] \
          [--web-cmd …] [--validate-cmd …] [--gateway-health-cmd …] \
          [--log-stream-cmd …] [--lane-config FILE] \
          [--worktree-web PATH] [--worktree-infra PATH] \
          [--no-attach] [--boot-check [secs]] [--auto-restart] [--print-cmds]
```

- Fixed windows: `coord web infra validate ops docs`. Precedence: preset <
  `--*-cmd` < lane-config YAML. `--print-cmds` = dry run, no tmux.
- `--no-attach --boot-check` is the CI/smoke pattern: exits non-zero if an
  AI lane fell back to a bare shell.
- `--auto-restart` spawns the watchdog (30s poll, max 3 restarts then a
  `giving-up` record in `<state-dir>/lane-restarts.jsonl`; restart lines
  carry NO `event` field — treat missing `event` as a restart).

Dynamic lanes on a running session (`--session` defaults to the current
tmux session):

```bash
loop-tmux add-lane  --window W (--harness H | --cmd C) [--model M] \
                    [--repo P] [--role R] [--auto-approve] [--wait-ready]
loop-tmux drop-lane --window W            # refuses base lanes without --force
loop-tmux list-lanes [--json]             # --json: window/harness/model/role/cmd/base
```

Harnesses (registry: `lib/harness-registry.sh list|fields|field|oneshot|probe`):
pi claude opencode codex cursor-agent hermes droid forge amp openclaw mprocs
shell. `oneshot <name>` prints the one-shot template (e.g. `claude -p
{prompt}`) used as the engine brain; shlex-split it and substitute `{prompt}`
as ONE argument — never via shell interpolation.

## loop-dispatch — paste into a lane

```bash
loop-dispatch [--session S] [--mode command|text] [--no-enter] [--verify] \
              [--wait-ready [--ready-timeout s]] [--interrupt] LANE PAYLOAD
```

- Lanes: `coord web infra validate-left validate-right ops-top ops-bottom
  docs` or any add-lane window name. `--window S:win.pane` direct-addresses.
- `--mode text` = bracketed paste + delay (`LOOP_DISPATCH_PASTE_DELAY`, 2.0s
  default) + Enter — use for AI prompts. `command` for shell lanes.
- `--wait-ready` polls readiness until `idle` first. `--interrupt` sends
  Escape, waits 1s, then pastes (cancels in-flight generation — steering).
- Exit 0 = delivered; non-zero = NOT delivered. At-most-once; never retry
  blindly.

## loop-lane-status — readiness

```bash
loop-lane-status S LANE              # word: working|awaiting-approval|idle|errored|unknown
loop-lane-status --print-target S LANE   # resolved tmux -t target (opaque shape)
loop-lane-status --json S LANE
loop-lane-status --json --all S      # whole fleet; absent fixed lanes skipped
```

Classification is pane-tail heuristics; extend idle detection for unusual
harness chrome with `LOOP_LANE_IDLE_HOME_PATTERN` (ERE alternation). Never
auto-act on `unknown` or `errored` lanes. Codex lanes render the idle home
chrome as the model·cwd footer, so set
`LOOP_LANE_IDLE_HOME_PATTERN='gpt-5\.5.*·'` (or your model) for them; working
detection handles codex out of the box (the live `esc to interrupt` marker is
matched across the full tail, above codex's composer).

## loop-digest — state digest

`loop-digest --project-root P --once` (human ASCII) or `--json`:
`{state, mailbox: {pending: [{file,from,to,subject,mtime}], processed_count},
unpushed, adrs}`. Default mode loops every 30s (coord pane default).

## loop-adr — decision records (MADR)

`loop-adr new "title"` → `docs/adr/NNNN-….md` (Proposed); `list`; `show N`;
`accept N` — refuses unless frontmatter links a `verify_record` AND a
`rollback` (or `canary_record`). Accept is HUMAN-ONLY; agents draft.

## scripts/ — compiled-coordinator helpers

```bash
scripts/loop-wiki-pending.sh [--project-root P] [--quiet]   # pending count (bare int)
scripts/loop-checkpoint.sh (--print | --dispatch [lane]) [--project-root P] [--header-file F]
scripts/loop-task-lint.sh [--tasks-dir D]                   # validate tasks/ files
scripts/loop-metrics.sh [--session S] [--project-root P] [--log]
scripts/loop-wiki-lint.sh (--print | --dispatch [--lane L]) [--session S] [--project-root P]
scripts/loop-jira-sync.sh (pull|push|both) [--dry-run] [--tasks-dir D]  # shim onto loop-pm
```

`loop-checkpoint --print` assembles the stateless coordinator prompt (header
+ checkpoint.md + index.md + pending summary); size report on stderr, warns
above 24k tokens; `--header-file` swaps the header (the engine uses this to
demand a side-effect-free decision block).

## State files (read-only unless stated)

- `.loop/orchestrator-state.json` — schema v2 (`loops.<id>.{status, branch,
  artifacts, …}`); written only by agents.
- `.loop/messages/` — mailbox `YYYYMMDD-HHMMSS-<from>-to-<to>.md` with
  `subject:` frontmatter. Anyone may ADD following the convention; never
  modify/delete existing. `processed/` = ack dir (docs lane only moves).
- `.loop/sessions/<s>/lane-restarts.jsonl` — watchdog log (append-only).
- `.loop/sessions/<s>/engine/` — engine-owned (events.jsonl, snapshot.json,
  pending-decision.json, decisions/, brain/, proposals/, engine.pid).
