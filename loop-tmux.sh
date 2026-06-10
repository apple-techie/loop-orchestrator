#!/usr/bin/env bash
# loop-tmux.sh — project-agnostic, loop-aware tmux bootstrap.
#
# Spins up a six-window session (coord / web / infra / validate / ops / docs)
# wired to a small orchestrator state-file + mailbox convention. See README.md
# for the lane roles, presets, and state-file schema.

set -euo pipefail

SESSION_NAME=""
PROJECT_NAME=""
PROJECT_ROOT=""
INFRA_ROOT=""
WEB_ROOT=""        # --worktree-web override; defaults to PROJECT_ROOT
INFRA_CD_ROOT=""   # --worktree-infra override; defaults to INFRA_ROOT
STATE_FILE=""
MAILBOX_DIR=""
GATEWAY_HEALTH_CMD=""
LOG_STREAM_CMD=""
VALIDATE_CMD=""
PRESET=""
AUTO_COORD_CMD=""
AUTO_WEB_CMD=""
AUTO_INFRA_CMD=""
AUTO_VALIDATE_LEFT_CMD=""
AUTO_VALIDATE_RIGHT_CMD=""
AUTO_OPS_TOP_CMD=""
AUTO_OPS_BOTTOM_CMD=""
AUTO_DOCS_CMD=""
NO_ATTACH=0
PRINT_CMDS=0
LANE_CONFIG_PATH=""
AUTO_RESTART=0
BOOT_CHECK=0
BOOT_CHECK_GRACE=8

# Resolve symlinks so DIGEST_SCRIPT finds loop-digest.sh in the repo even when
# this script is invoked via a `make install` symlink in ~/.local/bin (where
# only the bare name `loop-digest` exists, not loop-digest.sh).
_resolve_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}
SCRIPT_DIR="$(_resolve_script_dir)"
DIGEST_SCRIPT="$SCRIPT_DIR/loop-digest.sh"
LIB_DIR="$SCRIPT_DIR/lib"
LANE_HEALTH_SCRIPT="$LIB_DIR/lane-health.sh"
LANE_CONFIG_RESOLVER="$LIB_DIR/lane-config-resolver.sh"
HARNESS_REGISTRY="$LIB_DIR/harness-registry.sh"
LANE_STATUS_SCRIPT="$SCRIPT_DIR/loop-lane-status.sh"

usage() {
  cat <<'EOF'
Usage:
  loop-tmux.sh --project <name> --project-root <abs-path> [options]

Runtime lane subcommands (grow/shrink a live session, e.g. from the coord agent):
  loop-tmux.sh add-lane  --session <s> --window <w> --harness <h> [--model <m>] [--repo <p>] [--role <r>] [--auto-approve] [--cmd <c>] [--wait-ready]
  loop-tmux.sh drop-lane --session <s> --window <w> [--force]
  loop-tmux.sh list-lanes [--session <s>]
  (--session defaults to the current session when run inside tmux. drop-lane
   only kills windows created by add-lane unless --force. --cmd overrides the
   harness launch with a literal command.)

Required:
  --project <name>            Project label (becomes tmux session name by default)
  --project-root <abs-path>   Absolute path to the primary repo / working dir

Optional:
  --session <name>            Tmux session name (default: <project>)
  --infra-root <abs-path>     Second repo for the 'infra' window
                              (default: reuse --project-root)
  --state-file <path>         Orchestrator state JSON
                              (default: <project-root>/.loop/orchestrator-state.json)
  --mailbox-dir <path>        Mailbox directory
                              (default: <project-root>/.loop/messages)
  --gateway-health-cmd <cmd>  Command for ops-top pane (fleet / target health probe)
  --log-stream-cmd <cmd>      Command for ops-bottom pane (log tail)
  --validate-cmd <cmd>        Command for validate-left pane (e.g. mprocs / test watcher)
  --preset <name>             Apply preset lane composition (see below)

Per-lane overrides (override preset defaults):
  --coord-cmd <cmd>
  --web-cmd <cmd>
  --infra-cmd <cmd>
  --validate-left-cmd <cmd>
  --validate-right-cmd <cmd>
  --ops-top-cmd <cmd>
  --ops-bottom-cmd <cmd>
  --docs-cmd <cmd>

Multi-harness lane composition:
  --lane-config <path>    YAML config that declares per-lane harness, model,
                          repo, role, optional cmd override. Composable with
                          --preset and --*-cmd; later flags override earlier
                          ones (last-wins). See examples/lane-config.example.yaml.

Health probes:
  --boot-check [secs]     After lanes launch, wait <secs> (default 8), then
                          probe each AI lane's pane process. Print PASS/FAIL.
                          Exits non-zero if any lane fell back to a bare
                          shell (caught silent boot failures: bad model id,
                          missing binary, --flag mismatch).
  --auto-restart          Spawn a background watchdog that restarts AI lanes
                          whose process has exited. Logs to
                          <state-dir>/lane-restarts.jsonl. Combine with
                          --state-dir or set LOOP_TMUX_STATE_DIR.
  --state-dir <path>      Where --auto-restart writes its log + state.
                          Default: <project-root>/.loop/sessions/<session>

Worktree overrides:
  --worktree-web <path>   Open the web pane in <path> instead of --project-root
                          (e.g. a git worktree for a parallel branch).
                          Validated; refuses to launch if dir missing.
  --worktree-infra <path> Same idea for the infra pane vs --infra-root.

Inspection / dry-run:
  --print-cmds            Print resolved AUTO_*_CMD slots + working dirs and
                          exit without touching tmux. Useful for diffing
                          --preset vs --lane override compositions.
  --no-attach             Create/reuse the session but do not attach

Presets:
  pi-claude        web=pi, infra=claude, validate-left=--validate-cmd,
                   ops-top=--gateway-health-cmd, ops-bottom=--log-stream-cmd,
                   coord=live loop-digest
  all-pi           as pi-claude, but infra=pi
  all-claude       as pi-claude, but web=claude
  validation-only  validate-left=--validate-cmd (no AI lanes)
  monitor          ops-top=--gateway-health-cmd, ops-bottom=--log-stream-cmd,
                   coord=live loop-digest (no AI lanes)

Environment overrides (used when corresponding flag is not set):
  LOOP_PROJECT, LOOP_PROJECT_ROOT, LOOP_INFRA_ROOT,
  LOOP_WORKTREE_WEB, LOOP_WORKTREE_INFRA,
  LOOP_STATE_FILE, LOOP_MAILBOX_DIR,
  LOOP_GATEWAY_HEALTH_CMD, LOOP_LOG_STREAM_CMD, LOOP_VALIDATE_CMD

Examples:
  loop-tmux.sh --project my-app \
      --project-root ~/code/my-app \
      --infra-root  ~/code/my-app-infra \
      --validate-cmd 'npm run test:watch' \
      --gateway-health-cmd 'curl -s https://app.example.com/healthz' \
      --log-stream-cmd 'docker compose logs -f --tail=50' \
      --preset pi-claude

  loop-tmux.sh --project my-app --project-root ~/code/my-app \
      --preset all-pi

  loop-tmux.sh --project my-app --project-root ~/code/my-app \
      --preset monitor
EOF
}

# --------------------- dynamic lane subcommands ---------------------
# Runtime lane management so a coordinator (or the coord lane's agent) can grow
# and shrink a session as work evolves, instead of fixing every lane up front.
# Lane metadata is stored as tmux @loop_lane_* window options, so it lives and
# dies with the window — there is no external state file to leak or reconcile.
# All window targeting goes through stable window ids (@N) resolved by an EXACT
# name match, because tmux name-targets do fuzzy (prefix/fnmatch) resolution —
# operating on a name could otherwise hit (or kill) the wrong window.

_lane_usage() {
  cat >&2 <<'EOF'
Runtime lane management for a live loop-tmux session:

  loop-tmux.sh add-lane  --session <s> --window <w> --harness <h> [--model <m>]
                         [--repo <path>] [--role <r>] [--auto-approve] [--cmd <command>]
                         [--wait-ready] [--ready-timeout <secs>]
  loop-tmux.sh drop-lane --session <s> --window <w> [--force]
  loop-tmux.sh list-lanes [--session <s>]

  --session defaults to the current session when run inside tmux.
  --cmd runs a literal command instead of a registered harness.
  --auto-approve appends the harness's skip-permissions flag (where it has one).
  --wait-ready blocks until an AI lane is input-ready (loop-lane-status idle),
    up to --ready-timeout secs (default 20; env LANE_READY_TIMEOUT) — so a
    following dispatch can't race a slow-booting TUI.
  drop-lane only kills windows created by add-lane unless --force.
EOF
}

_lane_warn_missing_binary() {
  local label="$1" cmd="$2"
  [[ -z "$cmd" ]] && return 0
  local first="${cmd%% *}"
  first="${first#\'}"
  case "$first" in */* | '') return 0 ;; esac
  command -v "$first" >/dev/null 2>&1 || \
    echo "warning: lane '$label' command '$first' not found on PATH — pane will fall back to a bare shell" >&2
}

# Print the first pane's stable id (%N) for a window target.
_lane_first_pane() {
  tmux list-panes -t "$1" -F '#{pane_index} #{pane_id}' 2>/dev/null \
    | sort -n | head -n1 | cut -d' ' -f2-
}

_lane_default_session() {
  [[ -n "${TMUX:-}" ]] && tmux display-message -p '#S' 2>/dev/null || true
}

# Wait for a freshly-launched AI lane to become ready for input: first that the
# harness process actually came up (not a bare shell), then that loop-lane-status
# reports 'idle' (composer rendered, not booting/working). Bounds the wait by
# <timeout>s; warns and returns on timeout rather than blocking forever. Avoids
# the race where a paste lands before a slow TUI (e.g. Claude Code) is ready.
_lane_wait_ready() {
  local session="$1" window="$2" wid="$3" timeout="$4" elapsed=0 proc status
  while (( elapsed < timeout )); do
    proc="$(tmux display-message -p -t "$wid" '#{pane_current_command}' 2>/dev/null || echo "")"
    case "$proc" in zsh|bash|sh|fish|"") : ;; *) break ;; esac
    sleep 1; elapsed=$((elapsed + 1))
  done
  if [[ ! -x "$LANE_STATUS_SCRIPT" && ! -f "$LANE_STATUS_SCRIPT" ]]; then
    echo "[loop-tmux] add-lane: --wait-ready: loop-lane-status not found; skipping readiness poll" >&2
    return 0
  fi
  while (( elapsed < timeout )); do
    status="$(bash "$LANE_STATUS_SCRIPT" "$session" "$window" 2>/dev/null || echo unknown)"
    [[ "$status" == "idle" ]] && { echo "[loop-tmux] add-lane: lane '$window' ready"; return 0; }
    sleep 1; elapsed=$((elapsed + 1))
  done
  echo "[loop-tmux] add-lane: lane '$window' not confirmed ready within ${timeout}s (proceeding)" >&2
  return 0
}

# Resolve a window NAME to its stable window id (@N) via an EXACT literal match
# (window_id never contains spaces, so the name is the read remainder). Prints
# the id and returns 0 on an exact match; returns 1 if no window matches.
_lane_window_id() {
  local session="$1" name="$2" id wname
  while IFS=' ' read -r id wname; do
    [[ "$wname" == "$name" ]] && { printf '%s' "$id"; return 0; }
  done < <(tmux list-windows -t "$session" -F '#{window_id} #{window_name}' 2>/dev/null)
  return 1
}

_lane_subcommand_add() {
  local session="" window="" harness="" model="" repo="" role="" auto_approve=0 cmd_override=""
  local wait_ready=0 ready_timeout="${LANE_READY_TIMEOUT:-20}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --session)        session="${2:?add-lane: --session requires a value}"; shift 2 ;;
      --window)         window="${2:?add-lane: --window requires a value}"; shift 2 ;;
      --harness)        harness="${2:?add-lane: --harness requires a value}"; shift 2 ;;
      --model)          model="${2:?add-lane: --model requires a value}"; shift 2 ;;
      --repo)           repo="${2:?add-lane: --repo requires a value}"; shift 2 ;;
      --role)           role="${2:?add-lane: --role requires a value}"; shift 2 ;;
      --auto-approve)   auto_approve=1; shift ;;
      --cmd)            cmd_override="${2:?add-lane: --cmd requires a value}"; shift 2 ;;
      --wait-ready)     wait_ready=1; shift ;;
      --ready-timeout)  ready_timeout="${2:?add-lane: --ready-timeout requires a value}"; shift 2 ;;
      -h|--help)        _lane_usage; return 0 ;;
      *) echo "add-lane: unknown arg: $1" >&2; return 1 ;;
    esac
  done
  [[ -z "$session" ]] && session="$(_lane_default_session)"
  [[ -z "$session" ]] && { echo "add-lane: --session <name> required (or run inside tmux)" >&2; return 1; }
  [[ -z "$window" ]]  && { echo "add-lane: --window <name> required" >&2; return 1; }
  [[ -z "$harness" && -z "$cmd_override" ]] && { echo "add-lane: --harness <name> or --cmd <command> required" >&2; return 1; }
  command -v tmux >/dev/null 2>&1 || { echo "add-lane: tmux not installed or not on PATH" >&2; return 1; }
  tmux has-session -t "$session" 2>/dev/null || { echo "add-lane: session '$session' does not exist" >&2; return 1; }
  if _lane_window_id "$session" "$window" >/dev/null 2>&1; then
    echo "add-lane: window '$window' already exists in session '$session'" >&2; return 1
  fi

  # meta_* are what list-lanes will report — keep them truthful vs the launch.
  local launch="" meta_harness="${harness:-cmd}" meta_model="$model"
  if [[ -n "$cmd_override" ]]; then
    [[ -n "$harness" ]] && echo "add-lane: note — --harness '$harness' ignored because --cmd was given" >&2
    launch="$cmd_override"; meta_harness="cmd"; meta_model=""
  else
    # shellcheck source=lib/harness-registry.sh
    source "$HARNESS_REGISTRY"
    if ! harness_known "$harness"; then
      echo "add-lane: unknown harness '$harness' (known: ${HARNESS_REGISTRY_NAMES[*]})" >&2; return 1
    fi
    launch="$(harness_resolve_launch "$harness" "$model")" || return 1
    if [[ -n "$model" ]]; then
      local mf; mf="$(harness_field "$harness" model_flag)"
      if [[ "$mf" == "config" || "$mf" == "skip" ]]; then
        echo "add-lane: note — --model '$model' not applied; harness '$harness' selects its model via config" >&2
        meta_model=""
      fi
    fi
    if [[ "$auto_approve" -eq 1 ]]; then
      local flag; flag="$(harness_field "$harness" auto_approve_flag)"
      if [[ -n "$flag" ]]; then
        launch="$launch $flag"
      else
        echo "add-lane: note — --auto-approve has no effect for harness '$harness' (no interactive auto-approve flag; configure approvals in the harness)" >&2
      fi
    fi
  fi

  # Deterministic working dir: explicit --repo, else the base (first) window's
  # path — which stays on the project root — not the session's active window.
  local cwd="$repo"
  if [[ -z "$cwd" ]]; then
    local base_wid; base_wid="$(tmux list-windows -t "$session" -F '#{window_id}' 2>/dev/null | head -n1)"
    [[ -n "$base_wid" ]] && cwd="$(tmux display-message -p -t "$base_wid" '#{pane_current_path}' 2>/dev/null || true)"
  fi
  if [[ -n "$cwd" && ! -d "$cwd" ]]; then
    echo "add-lane: --repo path does not exist: $cwd" >&2; return 1
  fi

  # Create the window and capture its stable id for all subsequent targeting.
  local wid
  if [[ -n "$cwd" ]]; then
    wid="$(tmux new-window -t "$session" -n "$window" -c "$cwd" -P -F '#{window_id}')"
  else
    wid="$(tmux new-window -t "$session" -n "$window" -P -F '#{window_id}')"
  fi

  tmux set-option -w -t "$wid" @loop_lane 1 >/dev/null 2>&1 || true
  tmux set-option -w -t "$wid" @loop_lane_harness "$meta_harness" >/dev/null 2>&1 || true
  [[ -n "$meta_model" ]] && tmux set-option -w -t "$wid" @loop_lane_model "$meta_model" >/dev/null 2>&1 || true
  [[ -n "$role" ]]       && tmux set-option -w -t "$wid" @loop_lane_role "$role" >/dev/null 2>&1 || true
  tmux set-option -w -t "$wid" @loop_lane_cmd "$launch" >/dev/null 2>&1 || true

  _lane_warn_missing_binary "$window" "$launch"

  sleep "${LOOP_TMUX_SHELL_READY_DELAY:-0.6}"
  local target; target="$(_lane_first_pane "$wid")"
  if [[ -n "$launch" && -n "$target" ]]; then
    tmux send-keys -t "$target" -l -- "$launch"
    tmux send-keys -t "$target" Enter
  fi
  echo "[loop-tmux] add-lane: window '$window' ($wid) in '$session' (harness=$meta_harness${role:+, role=$role})"
  [[ -n "$launch" ]] && echo "             launched: $launch"

  # Optionally block until the lane is input-ready — only meaningful for AI
  # harness lanes (a shell/cmd lane is ready as soon as it is created).
  if [[ "$wait_ready" -eq 1 && -z "$cmd_override" \
        && "$meta_harness" != "cmd" && "$meta_harness" != "shell" && "$meta_harness" != "mprocs" ]]; then
    _lane_wait_ready "$session" "$window" "$wid" "$ready_timeout"
  fi
}

_lane_subcommand_drop() {
  local session="" window="" force=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --session) session="${2:?drop-lane: --session requires a value}"; shift 2 ;;
      --window)  window="${2:?drop-lane: --window requires a value}"; shift 2 ;;
      --force)   force=1; shift ;;
      -h|--help) _lane_usage; return 0 ;;
      *) echo "drop-lane: unknown arg: $1" >&2; return 1 ;;
    esac
  done
  [[ -z "$session" ]] && session="$(_lane_default_session)"
  [[ -z "$session" ]] && { echo "drop-lane: --session <name> required (or run inside tmux)" >&2; return 1; }
  [[ -z "$window" ]]  && { echo "drop-lane: --window <name> required" >&2; return 1; }
  command -v tmux >/dev/null 2>&1 || { echo "drop-lane: tmux not installed or not on PATH" >&2; return 1; }
  tmux has-session -t "$session" 2>/dev/null || { echo "drop-lane: session '$session' does not exist" >&2; return 1; }
  local wid
  if ! wid="$(_lane_window_id "$session" "$window")"; then
    echo "drop-lane: window '$window' not found in session '$session'" >&2; return 1
  fi
  # Safety: only drop lanes that add-lane created, unless --force — so a
  # coordinator can't accidentally kill a base lane (coord/web/infra/...).
  local is_dynamic; is_dynamic="$(tmux show-options -wqv -t "$wid" @loop_lane 2>/dev/null || true)"
  if [[ "$is_dynamic" != "1" && "$force" -ne 1 ]]; then
    echo "drop-lane: window '$window' was not created by add-lane; refusing (pass --force to override)" >&2
    return 1
  fi
  tmux kill-window -t "$wid"
  echo "[loop-tmux] drop-lane: killed window '$window' ($wid) in '$session'"
}

_lane_subcommand_list() {
  local session="" json=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --session) session="${2:?list-lanes: --session requires a value}"; shift 2 ;;
      --json)    json=1; shift ;;
      -h|--help) _lane_usage; return 0 ;;
      *) echo "list-lanes: unknown arg: $1" >&2; return 1 ;;
    esac
  done
  [[ -z "$session" ]] && session="$(_lane_default_session)"
  [[ -z "$session" ]] && { echo "list-lanes: --session <name> required (or run inside tmux)" >&2; return 1; }
  command -v tmux >/dev/null 2>&1 || { echo "list-lanes: tmux not installed or not on PATH" >&2; return 1; }
  tmux has-session -t "$session" 2>/dev/null || { echo "list-lanes: session '$session' does not exist" >&2; return 1; }
  [[ "$json" -eq 0 ]] && printf '%-16s %-4s %-12s %-14s %s\n' WINDOW DYN HARNESS ROLE CMD
  local wid wname dyn harness model role cmd
  # Read by window id so option lookups can't prefix-collide between windows.
  # --json: emit unit-separator records and let python3 do the escaping —
  # the %-16s table is for humans and breaks on long names / cmds with spaces.
  {
    while IFS=' ' read -r wid wname; do
      [[ -z "$wid" ]] && continue
      dyn="$(tmux show-options -wqv -t "$wid" @loop_lane 2>/dev/null || true)"
      harness="$(tmux show-options -wqv -t "$wid" @loop_lane_harness 2>/dev/null || true)"
      model="$(tmux show-options -wqv -t "$wid" @loop_lane_model 2>/dev/null || true)"
      role="$(tmux show-options -wqv -t "$wid" @loop_lane_role 2>/dev/null || true)"
      cmd="$(tmux show-options -wqv -t "$wid" @loop_lane_cmd 2>/dev/null || true)"
      if [[ "$json" -eq 1 ]]; then
        printf '%s\x1f%s\x1f%s\x1f%s\x1f%s\x1f%s\n' "$wname" "$dyn" "$harness" "$model" "$role" "$cmd"
      else
        printf '%-16s %-4s %-12s %-14s %s\n' "$wname" "$([[ "$dyn" == "1" ]] && echo yes || echo '-')" "${harness:--}" "${role:--}" "${cmd:--}"
      fi
    done < <(tmux list-windows -t "$session" -F '#{window_id} #{window_name}' 2>/dev/null)
  } | {
    if [[ "$json" -eq 1 ]]; then
      SESSION="$session" python3 -c '
import datetime, json, os, sys

lanes = []
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line:
        continue
    wname, dyn, harness, model, role, cmd = line.split("\x1f")
    lanes.append({
        "window": wname,
        "harness": harness or None,
        "model": model or None,
        "role": role or None,
        "cmd": cmd or None,
        "base": dyn != "1",
    })

print(json.dumps({
    "contract_version": 1,
    "session": os.environ["SESSION"],
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "lanes": lanes,
}, indent=2))
'
    else
      cat
    fi
  }
}

case "${1:-}" in
  add-lane)   shift; _lane_subcommand_add  "$@"; exit $? ;;
  drop-lane)  shift; _lane_subcommand_drop "$@"; exit $? ;;
  list-lanes) shift; _lane_subcommand_list "$@"; exit $? ;;
esac

# --------------------- argparse ---------------------

# Apply env defaults first; flags will override below.
PROJECT_NAME="${LOOP_PROJECT:-}"
PROJECT_ROOT="${LOOP_PROJECT_ROOT:-}"
INFRA_ROOT="${LOOP_INFRA_ROOT:-}"
WEB_ROOT="${LOOP_WORKTREE_WEB:-}"
INFRA_CD_ROOT="${LOOP_WORKTREE_INFRA:-}"
STATE_FILE="${LOOP_STATE_FILE:-}"
MAILBOX_DIR="${LOOP_MAILBOX_DIR:-}"
GATEWAY_HEALTH_CMD="${LOOP_GATEWAY_HEALTH_CMD:-}"
LOG_STREAM_CMD="${LOOP_LOG_STREAM_CMD:-}"
VALIDATE_CMD="${LOOP_VALIDATE_CMD:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)              PROJECT_NAME="$2"; shift 2 ;;
    --project-root)         PROJECT_ROOT="$2"; shift 2 ;;
    --infra-root)           INFRA_ROOT="$2"; shift 2 ;;
    --worktree-web)         WEB_ROOT="$2"; shift 2 ;;
    --worktree-infra)       INFRA_CD_ROOT="$2"; shift 2 ;;
    --state-file)           STATE_FILE="$2"; shift 2 ;;
    --mailbox-dir)          MAILBOX_DIR="$2"; shift 2 ;;
    --gateway-health-cmd)   GATEWAY_HEALTH_CMD="$2"; shift 2 ;;
    --log-stream-cmd)       LOG_STREAM_CMD="$2"; shift 2 ;;
    --validate-cmd)         VALIDATE_CMD="$2"; shift 2 ;;
    --session)              SESSION_NAME="$2"; shift 2 ;;
    --preset)               PRESET="$2"; shift 2 ;;
    --coord-cmd)            AUTO_COORD_CMD="$2"; shift 2 ;;
    --web-cmd)              AUTO_WEB_CMD="$2"; shift 2 ;;
    --infra-cmd)            AUTO_INFRA_CMD="$2"; shift 2 ;;
    --validate-left-cmd)    AUTO_VALIDATE_LEFT_CMD="$2"; shift 2 ;;
    --validate-right-cmd)   AUTO_VALIDATE_RIGHT_CMD="$2"; shift 2 ;;
    --ops-top-cmd)          AUTO_OPS_TOP_CMD="$2"; shift 2 ;;
    --ops-bottom-cmd)       AUTO_OPS_BOTTOM_CMD="$2"; shift 2 ;;
    --docs-cmd)             AUTO_DOCS_CMD="$2"; shift 2 ;;
    --no-attach)            NO_ATTACH=1; shift ;;
    --print-cmds)           PRINT_CMDS=1; shift ;;
    --lane-config)          LANE_CONFIG_PATH="$2"; shift 2 ;;
    --auto-restart)         AUTO_RESTART=1; shift ;;
    --boot-check)
      BOOT_CHECK=1
      # Optional numeric grace period as next arg (default 8).
      if [[ "${2:-}" =~ ^[0-9]+$ ]]; then
        BOOT_CHECK_GRACE="$2"
        shift 2
      else
        shift
      fi
      ;;
    --state-dir)            STATE_DIR="$2"; shift 2 ;;
    -h|--help)              usage; exit 0 ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      # Legacy positional support: first bare arg = session name.
      if [[ -z "$SESSION_NAME" ]]; then
        SESSION_NAME="$1"
        shift
      else
        echo "Unexpected argument: $1" >&2
        usage >&2
        exit 1
      fi
      ;;
  esac
done

# --------------------- validate + derive defaults ---------------------

if [[ -z "$PROJECT_NAME" ]]; then
  echo "error: --project <name> is required (or set LOOP_PROJECT)" >&2
  usage >&2
  exit 1
fi

if [[ -z "$PROJECT_ROOT" ]]; then
  echo "error: --project-root <abs-path> is required (or set LOOP_PROJECT_ROOT)" >&2
  usage >&2
  exit 1
fi

if [[ ! -d "$PROJECT_ROOT" ]]; then
  echo "error: --project-root does not exist: $PROJECT_ROOT" >&2
  exit 1
fi
# Canonicalize to an absolute path (the docs promise absolute). A relative
# --project-root would otherwise embed './…' into the coord/pane commands and
# the derived state-file/mailbox paths.
PROJECT_ROOT="$(cd "$PROJECT_ROOT" && pwd)"

SESSION_NAME="${SESSION_NAME:-$PROJECT_NAME}"
INFRA_ROOT="${INFRA_ROOT:-$PROJECT_ROOT}"
STATE_FILE="${STATE_FILE:-$PROJECT_ROOT/.loop/orchestrator-state.json}"
MAILBOX_DIR="${MAILBOX_DIR:-$PROJECT_ROOT/.loop/messages}"

if [[ ! -d "$INFRA_ROOT" ]]; then
  echo "warning: --infra-root does not exist: $INFRA_ROOT (falling back to project-root)" >&2
  INFRA_ROOT="$PROJECT_ROOT"
fi
INFRA_ROOT="$(cd "$INFRA_ROOT" && pwd)"

# Worktree overrides default to the canonical roots. If passed, validate them
# — a missing dir would silently `cd $HOME` and confuse every downstream cmd.
WEB_ROOT="${WEB_ROOT:-$PROJECT_ROOT}"
INFRA_CD_ROOT="${INFRA_CD_ROOT:-$INFRA_ROOT}"
for _check_path in "$WEB_ROOT" "$INFRA_CD_ROOT"; do
  if [[ ! -d "$_check_path" ]]; then
    echo "error: worktree path does not exist: $_check_path" >&2
    exit 1
  fi
done
# Canonicalize the (validated) worktree roots too, for the same reason.
WEB_ROOT="$(cd "$WEB_ROOT" && pwd)"
INFRA_CD_ROOT="$(cd "$INFRA_CD_ROOT" && pwd)"

# --auto-restart state dir — per-session so multiple sessions don't fight
# over the same lane-restarts.jsonl.
STATE_DIR="${STATE_DIR:-${LOOP_TMUX_STATE_DIR:-$PROJECT_ROOT/.loop/sessions/$SESSION_NAME}}"

# --------------------- preset handling ---------------------

# POSIX single-quote a string for safe embedding in a shell command line,
# escaping any embedded single quote (' -> '\'').
_shq() {
  local s=$1 q="'"
  s=${s//$q/$q\\$q$q}
  printf '%s' "$q$s$q"
}

_digest_cmd() {
  # Build the live loop-digest invocation for the coord pane. Each path is
  # single-quoted via _shq so spaces — or an apostrophe in a path — don't break
  # the command sent into the pane.
  printf '%s --state-file %s --mailbox-dir %s --project-root %s' \
    "$(_shq "$DIGEST_SCRIPT")" "$(_shq "$STATE_FILE")" \
    "$(_shq "$MAILBOX_DIR")" "$(_shq "$PROJECT_ROOT")"
}

apply_preset() {
  local coord_digest
  coord_digest="$(_digest_cmd)"
  case "$1" in
    pi-claude)
      : "${AUTO_COORD_CMD:=$coord_digest}"
      : "${AUTO_WEB_CMD:=pi}"
      : "${AUTO_INFRA_CMD:=claude}"
      : "${AUTO_VALIDATE_LEFT_CMD:=$VALIDATE_CMD}"
      : "${AUTO_OPS_TOP_CMD:=$GATEWAY_HEALTH_CMD}"
      : "${AUTO_OPS_BOTTOM_CMD:=$LOG_STREAM_CMD}"
      ;;
    all-pi)
      : "${AUTO_COORD_CMD:=$coord_digest}"
      : "${AUTO_WEB_CMD:=pi}"
      : "${AUTO_INFRA_CMD:=pi}"
      : "${AUTO_VALIDATE_LEFT_CMD:=$VALIDATE_CMD}"
      : "${AUTO_OPS_TOP_CMD:=$GATEWAY_HEALTH_CMD}"
      : "${AUTO_OPS_BOTTOM_CMD:=$LOG_STREAM_CMD}"
      ;;
    all-claude)
      : "${AUTO_COORD_CMD:=$coord_digest}"
      : "${AUTO_WEB_CMD:=claude}"
      : "${AUTO_INFRA_CMD:=claude}"
      : "${AUTO_VALIDATE_LEFT_CMD:=$VALIDATE_CMD}"
      : "${AUTO_OPS_TOP_CMD:=$GATEWAY_HEALTH_CMD}"
      : "${AUTO_OPS_BOTTOM_CMD:=$LOG_STREAM_CMD}"
      ;;
    validation-only)
      : "${AUTO_VALIDATE_LEFT_CMD:=$VALIDATE_CMD}"
      ;;
    monitor)
      : "${AUTO_COORD_CMD:=$coord_digest}"
      : "${AUTO_OPS_TOP_CMD:=$GATEWAY_HEALTH_CMD}"
      : "${AUTO_OPS_BOTTOM_CMD:=$LOG_STREAM_CMD}"
      ;;
    *)
      echo "Unknown preset: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
}

if [[ -n "$PRESET" ]]; then
  apply_preset "$PRESET"
fi

# --------------------- lane-config (M002 multi-harness) ---------------------

# Populates AUTO_*_CMD vars from a lane-config YAML. Maps lane names from the
# YAML schema to AUTO_* slots. Lanes outside the AUTO_* set (e.g. coord with
# harness=shell) are silently ignored — coord auto-runs loop-digest by
# default; let the preset / --coord-cmd flag own it.
#
# Precedence (last wins):
#   defaults < preset < per-flag (--web-cmd etc.) < lane-config
# apply_preset uses ':=' (assign-if-unset), so an explicit --*-cmd flag set
# earlier in argparse beats the preset default for that lane; apply_lane_config
# below assigns unconditionally, so lane-config beats both. To beat the YAML
# for a lane, edit the YAML instead.
apply_lane_config() {
  local config_path="$1"
  if [[ ! -f "$config_path" ]]; then
    echo "error: --lane-config file not found: $config_path" >&2
    exit 1
  fi
  if [[ ! -f "$LANE_CONFIG_RESOLVER" ]]; then
    echo "error: lane-config-resolver not found: $LANE_CONFIG_RESOLVER" >&2
    echo "       (expected alongside loop-tmux.sh in <repo>/lib/)" >&2
    exit 1
  fi
  # Pass --project-root into the resolver so any `cmd: scripts/foo.sh`
  # inside the YAML resolves against the caller's tree, not loop-orch.
  export LANE_CONFIG_PROJECT_ROOT="$PROJECT_ROOT"
  # shellcheck source=lib/lane-config-resolver.sh
  source "$LANE_CONFIG_RESOLVER"
  if ! lane_config_load "$config_path"; then
    echo "error: failed to load lane-config: $config_path" >&2
    exit 1
  fi
  if ! lane_config_validate; then
    echo "error: lane-config validation failed: $config_path" >&2
    exit 1
  fi

  local lane launch
  while IFS= read -r lane; do
    [[ -z "$lane" ]] && continue
    launch="$(lane_config_launch "$lane" with-auto-approve)" || {
      echo "error: failed to resolve launch for lane '$lane'" >&2
      exit 1
    }
    case "$lane" in
      coord)           : ;;  # owned by preset / --coord-cmd
      web)             AUTO_WEB_CMD="$launch" ;;
      infra)           AUTO_INFRA_CMD="$launch" ;;
      validate-left)   AUTO_VALIDATE_LEFT_CMD="$launch" ;;
      validate-right)  AUTO_VALIDATE_RIGHT_CMD="$launch" ;;
      ops-top)         AUTO_OPS_TOP_CMD="$launch" ;;
      ops-bottom)      AUTO_OPS_BOTTOM_CMD="$launch" ;;
      docs)            AUTO_DOCS_CMD="$launch" ;;
      *)
        echo "warning: lane-config lane '$lane' has no tmux slot — ignoring" >&2
        ;;
    esac
  done < <(lane_config_lanes)
}

if [[ -n "$LANE_CONFIG_PATH" ]]; then
  apply_lane_config "$LANE_CONFIG_PATH"
fi

# --------------------- dry-run inspection ---------------------

if [[ "$PRINT_CMDS" -eq 1 ]]; then
  printf 'SESSION_NAME=%s\n'             "$SESSION_NAME"
  printf 'WEB_ROOT=%s\n'                 "$WEB_ROOT"
  printf 'INFRA_CD_ROOT=%s\n'            "$INFRA_CD_ROOT"
  printf 'AUTO_COORD_CMD=%s\n'           "$AUTO_COORD_CMD"
  printf 'AUTO_WEB_CMD=%s\n'             "$AUTO_WEB_CMD"
  printf 'AUTO_INFRA_CMD=%s\n'           "$AUTO_INFRA_CMD"
  printf 'AUTO_VALIDATE_LEFT_CMD=%s\n'   "$AUTO_VALIDATE_LEFT_CMD"
  printf 'AUTO_VALIDATE_RIGHT_CMD=%s\n'  "$AUTO_VALIDATE_RIGHT_CMD"
  printf 'AUTO_OPS_TOP_CMD=%s\n'         "$AUTO_OPS_TOP_CMD"
  printf 'AUTO_OPS_BOTTOM_CMD=%s\n'      "$AUTO_OPS_BOTTOM_CMD"
  printf 'AUTO_DOCS_CMD=%s\n'            "$AUTO_DOCS_CMD"
  exit 0
fi

# --------------------- tmux bootstrap ---------------------

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed or not on PATH" >&2
  exit 1
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  if [[ "$NO_ATTACH" -eq 1 ]]; then
    echo "Session '$SESSION_NAME' already exists."
    exit 0
  fi
  echo "Session '$SESSION_NAME' already exists. Attaching..."
  exec tmux attach -t "$SESSION_NAME"
fi

# Sort panes numerically by index (prefix the index, sort -n, strip it) so the
# order is 0,1,2,…,10 rather than the lexical .10-before-.2 of a plain sort.
first_pane_target() {
  local window="$1"
  tmux list-panes -t "$SESSION_NAME:$window" -F '#{pane_index} #{session_name}:#{window_name}.#{pane_index}' 2>/dev/null | sort -n | head -n 1 | cut -d' ' -f2-
}

nth_pane_target() {
  local window="$1"
  local n="$2"
  tmux list-panes -t "$SESSION_NAME:$window" -F '#{pane_index} #{session_name}:#{window_name}.#{pane_index}' 2>/dev/null | sort -n | sed -n "${n}p" | cut -d' ' -f2-
}

send_optional_cmd() {
  local target="$1"
  local cmd="$2"
  if [[ -n "$cmd" ]]; then
    # Send the command as a literal string (-l) so a payload whose first token
    # is a tmux key name (Enter/Up/C-c/…) is typed rather than interpreted as a
    # keystroke; submit it with a separate Enter.
    tmux send-keys -t "$target" -l -- "$cmd"
    tmux send-keys -t "$target" Enter
  fi
}

# Warn (don't fail) if a lane's command launches a bare binary that isn't on
# PATH — otherwise the pane silently falls back to a login shell, which looks
# identical to a healthy idle lane. Path-based first tokens (shell/ops/digest
# lanes) and empty commands are skipped since they aren't simple PATH lookups.
warn_if_missing_binary() {
  local lane="$1" cmd="$2"
  [[ -z "$cmd" ]] && return 0
  local first="${cmd%% *}"
  first="${first#\'}"
  case "$first" in
    */* | '') return 0 ;;
  esac
  if ! command -v "$first" >/dev/null 2>&1; then
    echo "warning: lane '$lane' command '$first' not found on PATH — pane will fall back to a bare shell" >&2
  fi
}

# Create all six windows first, THEN dispatch commands. If we interleave
# create-then-send-keys, the first send-keys often lands before the freshly
# spawned shell has rendered its prompt, and zsh discards the C-m. Batching
# the create phase + one settling sleep makes the dispatch deterministic.

# Window 1: coord — stays on PROJECT_ROOT (canonical loop state lives here,
# not in a worktree). Coord shouldn't shift with branch experiments.
tmux new-session -d -s "$SESSION_NAME" -n coord -c "$PROJECT_ROOT"

# Window 2: web (primary repo implementation lane) — honors --worktree-web
tmux new-window -t "$SESSION_NAME" -n web -c "$WEB_ROOT"

# Window 3: infra (secondary repo implementation lane) — honors --worktree-infra
tmux new-window -t "$SESSION_NAME" -n infra -c "$INFRA_CD_ROOT"

# Window 4: validate — stays on WEB_ROOT since tests/lint/typecheck live with
# the implementation tree.
tmux new-window -t "$SESSION_NAME" -n validate -c "$WEB_ROOT"
tmux split-window -h -t "$SESSION_NAME":validate -c "$WEB_ROOT"

# Window 5: ops — stays on PROJECT_ROOT (prod-facing scripts shouldn't run
# against an experimental worktree by default).
tmux new-window -t "$SESSION_NAME" -n ops -c "$PROJECT_ROOT"
tmux split-window -v -t "$SESSION_NAME":ops -c "$PROJECT_ROOT"

# Window 6: docs — stays on PROJECT_ROOT (docs scan repos from the canonical
# tree).
tmux new-window -t "$SESSION_NAME" -n docs -c "$PROJECT_ROOT"

# Let every spawned shell render its prompt before we feed it keys. Override
# with LOOP_TMUX_SHELL_READY_DELAY if your shell init (oh-my-zsh plugins,
# powerlevel10k, conda hooks) takes longer.
sleep "${LOOP_TMUX_SHELL_READY_DELAY:-0.6}"

# Surface missing harness/tool binaries loudly before dispatch — a bare
# command-not-found otherwise drops the pane to a shell that looks healthy.
warn_if_missing_binary coord          "$AUTO_COORD_CMD"
warn_if_missing_binary web            "$AUTO_WEB_CMD"
warn_if_missing_binary infra          "$AUTO_INFRA_CMD"
warn_if_missing_binary validate-left  "$AUTO_VALIDATE_LEFT_CMD"
warn_if_missing_binary validate-right "$AUTO_VALIDATE_RIGHT_CMD"
warn_if_missing_binary ops-top        "$AUTO_OPS_TOP_CMD"
warn_if_missing_binary ops-bottom     "$AUTO_OPS_BOTTOM_CMD"
warn_if_missing_binary docs           "$AUTO_DOCS_CMD"

send_optional_cmd "$(first_pane_target coord)"   "$AUTO_COORD_CMD"
send_optional_cmd "$(first_pane_target web)"     "$AUTO_WEB_CMD"
send_optional_cmd "$(first_pane_target infra)"   "$AUTO_INFRA_CMD"
send_optional_cmd "$(nth_pane_target validate 1)" "$AUTO_VALIDATE_LEFT_CMD"
send_optional_cmd "$(nth_pane_target validate 2)" "$AUTO_VALIDATE_RIGHT_CMD"
send_optional_cmd "$(nth_pane_target ops 1)"     "$AUTO_OPS_TOP_CMD"
send_optional_cmd "$(nth_pane_target ops 2)"     "$AUTO_OPS_BOTTOM_CMD"
send_optional_cmd "$(first_pane_target docs)"    "$AUTO_DOCS_CMD"

tmux select-window -t "$SESSION_NAME":coord

# --------------------- boot-check (one-shot lane probe) ---------------------

BOOT_CHECK_EXIT=0
if [[ "$BOOT_CHECK" -eq 1 ]]; then
  if [[ ! -f "$HARNESS_REGISTRY" ]]; then
    echo "warning: --boot-check skipped — harness-registry not found at $HARNESS_REGISTRY" >&2
  else
    # shellcheck source=lib/harness-registry.sh
    source "$HARNESS_REGISTRY"

    echo "[loop-tmux] boot-check: waiting ${BOOT_CHECK_GRACE}s for lanes to settle..."
    sleep "$BOOT_CHECK_GRACE"

    _boot_failed=0
    _boot_total=0
    _probe_lane() {
      local lane="$1" window="$2" pane_n="$3" cmd="$4"
      [[ -z "$cmd" ]] && return 0
      _boot_total=$((_boot_total + 1))
      local target
      target="$(nth_pane_target "$window" "$pane_n")"
      if [[ -z "$target" ]]; then
        printf '  FAIL  %-16s — could not resolve pane %s:%s\n' "$lane" "$window" "$pane_n" >&2
        _boot_failed=$((_boot_failed + 1))
        return 0
      fi
      local current
      current="$(tmux display-message -p -t "$target" '#{pane_current_command}' 2>/dev/null || echo "")"
      if harness_is_bare_shell_process "$current"; then
        printf '  FAIL  %-16s pane=%s current=%s\n          expected harness from cmd: %s\n' \
          "$lane" "$target" "${current:-<empty>}" "$cmd" >&2
        _boot_failed=$((_boot_failed + 1))
      else
        printf '  PASS  %-16s pane=%s current=%s\n' "$lane" "$target" "$current"
      fi
    }

    echo "[loop-tmux] boot-check results:"
    _probe_lane "web"            web      1 "$AUTO_WEB_CMD"
    _probe_lane "infra"          infra    1 "$AUTO_INFRA_CMD"
    _probe_lane "validate-left"  validate 1 "$AUTO_VALIDATE_LEFT_CMD"
    _probe_lane "validate-right" validate 2 "$AUTO_VALIDATE_RIGHT_CMD"
    _probe_lane "ops-top"        ops      1 "$AUTO_OPS_TOP_CMD"
    _probe_lane "ops-bottom"     ops      2 "$AUTO_OPS_BOTTOM_CMD"
    _probe_lane "docs"           docs     1 "$AUTO_DOCS_CMD"

    if (( _boot_failed > 0 )); then
      echo "[loop-tmux] boot-check: $_boot_failed/$_boot_total lane(s) failed to launch" >&2
      echo "[loop-tmux] common causes: invalid model id, missing harness binary, wrong --flag" >&2
      BOOT_CHECK_EXIT=1
    else
      echo "[loop-tmux] boot-check: all $_boot_total lane(s) live"
    fi
  fi
fi

# --------------------- auto-restart watchdog ---------------------

if [[ "$AUTO_RESTART" -eq 1 ]]; then
  if [[ ! -x "$LANE_HEALTH_SCRIPT" ]]; then
    echo "warning: --auto-restart skipped — lane-health not executable at $LANE_HEALTH_SCRIPT" >&2
  else
    mkdir -p "$STATE_DIR"
    _watchdog_pidfile="$STATE_DIR/lane-health.pid"
    # Don't stack watchdogs: if a live one is already managing this state-dir
    # (e.g. an orphan from a prior run with the same session name), leave it be.
    _existing_pid=""
    [[ -f "$_watchdog_pidfile" ]] && _existing_pid="$(cat "$_watchdog_pidfile" 2>/dev/null || true)"
    if [[ -n "$_existing_pid" ]] && kill -0 "$_existing_pid" 2>/dev/null; then
      echo "[loop-tmux] auto-restart watchdog already running (pid=$_existing_pid) — not starting another"
    else
      nohup "$LANE_HEALTH_SCRIPT" \
        --session "$SESSION_NAME" \
        --state-dir "$STATE_DIR" \
        --interval 30 \
        --web-cmd            "$AUTO_WEB_CMD" \
        --infra-cmd          "$AUTO_INFRA_CMD" \
        --docs-cmd           "$AUTO_DOCS_CMD" \
        --validate-left-cmd  "$AUTO_VALIDATE_LEFT_CMD" \
        --validate-right-cmd "$AUTO_VALIDATE_RIGHT_CMD" \
        > "$STATE_DIR/lane-health.log" 2>&1 &
      echo "[loop-tmux] auto-restart watchdog started (pid=$!) — log: $STATE_DIR/lane-health.log"
      echo "[loop-tmux] stop it with: kill \$(cat '$_watchdog_pidfile')"
    fi
  fi
fi

if [[ "$NO_ATTACH" -eq 1 ]]; then
  echo "Session '$SESSION_NAME' prepared."
  exit "$BOOT_CHECK_EXIT"
fi

exec tmux attach -t "$SESSION_NAME"
