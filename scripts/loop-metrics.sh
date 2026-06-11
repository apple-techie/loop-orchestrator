#!/usr/bin/env bash
# loop-metrics.sh — coordinator-efficiency metrics for the keep/discard gate.
#
# Prints one summary block (AGENTS.md "### Experiment protocol" compares these
# numbers before/after every schema/workflow experiment):
#   checkpoint_tokens   latest loop-checkpoint.sh --print byte count / 4
#   pending_messages    loop-wiki-pending.sh --quiet
#   restarts_24h        lane-restarts.jsonl lines with NO `event` field
#                       (lane-health restart records — see CONTRACT.md)
#   giveups_24h         lane-restarts.jsonl lines with event "giving-up"
#   ingests_7d / lints_7d / checkpoints_7d / experiments
#                       counted from ops-wiki/log.md `## [date] <type> |`
#   dispatches          n/a — not derivable from substrate surfaces
#
# Missing inputs degrade to 0 (or n/a) with a note, never a crash. Timestamp
# and date comparisons run in python3 (a substrate dependency) because BSD and
# GNU date(1) disagree on relative-date flags.
#
# --log appends exactly one `## [YYYY-MM-DD] metrics | <one-line summary>`
# entry to ops-wiki/log.md.

set -euo pipefail

PROJECT_ROOT=""
SESSION_NAME=""
DO_LOG=0

# Resolve this script's real directory (symlink-aware) so the sibling scripts
# and the default project root (this script's parent-of-parent directory)
# resolve even when invoked via a ~/.local/bin symlink or from anywhere.
_metrics_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}
SCRIPT_DIR="$(_metrics_script_dir)"

usage() {
  cat <<'EOF'
Usage:
  loop-metrics.sh [options]

Prints the coordinator-efficiency summary block: checkpoint_tokens,
pending_messages, restarts_24h, giveups_24h, ingests_7d, lints_7d,
checkpoints_7d, experiments (dispatch counts are n/a). Missing inputs
degrade to 0/n-a with a note instead of failing.

Options:
  --log                 Also append `## [YYYY-MM-DD] metrics | <summary>`
                        (exactly one entry) to ops-wiki/log.md
  --session <name>      Session for .loop/sessions/<name>/lane-restarts.jsonl
                        default: the sole directory under .loop/sessions/,
                        else the current $TMUX session if it has one there
  --project-root <path> Repo root containing ops-wiki/ and .loop/
                        default: this script's parent-of-parent directory
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --log)          DO_LOG=1; shift ;;
    --session)      SESSION_NAME="$2"; shift 2 ;;
    --project-root) PROJECT_ROOT="$2"; shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      echo "Unexpected argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$PROJECT_ROOT" ]]; then
  PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

CHECKPOINT_SCRIPT="$SCRIPT_DIR/loop-checkpoint.sh"
PENDING_SCRIPT="$SCRIPT_DIR/loop-wiki-pending.sh"
LOG_FILE="$PROJECT_ROOT/ops-wiki/log.md"
SESSIONS_DIR="$PROJECT_ROOT/.loop/sessions"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required (substrate dependency, used for date math)" >&2
  exit 1
fi

NOTES=""
add_note() {
  NOTES="${NOTES}  - $1
"
}

# ---- checkpoint_tokens -------------------------------------------------------
CHECKPOINT_TOKENS="n/a"
CHECKPOINT_DETAIL=""
if [[ -x "$CHECKPOINT_SCRIPT" ]]; then
  if CHECKPOINT_PROMPT="$("$CHECKPOINT_SCRIPT" --print --project-root "$PROJECT_ROOT" 2>/dev/null)"; then
    CHECKPOINT_BYTES="$(printf '%s' "$CHECKPOINT_PROMPT" | wc -c | tr -d ' ')"
    CHECKPOINT_TOKENS=$(( CHECKPOINT_BYTES / 4 ))
    CHECKPOINT_DETAIL=" (${CHECKPOINT_BYTES} bytes / 4)"
  else
    add_note "checkpoint_tokens n/a: loop-checkpoint.sh --print failed (missing ops-wiki files?)"
  fi
else
  add_note "checkpoint_tokens n/a: sibling loop-checkpoint.sh not found/executable"
fi

# ---- pending_messages --------------------------------------------------------
PENDING="0"
if [[ -x "$PENDING_SCRIPT" ]]; then
  if ! PENDING="$("$PENDING_SCRIPT" --quiet --project-root "$PROJECT_ROOT" 2>/dev/null)"; then
    PENDING="n/a"
    add_note "pending_messages n/a: loop-wiki-pending.sh failed"
  fi
else
  PENDING="n/a"
  add_note "pending_messages n/a: sibling loop-wiki-pending.sh not found/executable"
fi

# ---- session default ---------------------------------------------------------
if [[ -z "$SESSION_NAME" && -d "$SESSIONS_DIR" ]]; then
  SESSION_DIRS="$(find "$SESSIONS_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort)"
  if [[ -n "$SESSION_DIRS" ]]; then
    SESSION_COUNT="$(printf '%s\n' "$SESSION_DIRS" | wc -l | tr -d ' ')"
    if [[ "$SESSION_COUNT" -eq 1 ]]; then
      SESSION_NAME="$SESSION_DIRS"
      add_note "session defaulted to sole .loop/sessions/ entry '$SESSION_NAME'"
    elif [[ -n "${TMUX:-}" ]]; then
      TMUX_SESSION="$(tmux display-message -p '#S' 2>/dev/null || true)"
      if [[ -n "$TMUX_SESSION" && -d "$SESSIONS_DIR/$TMUX_SESSION" ]]; then
        SESSION_NAME="$TMUX_SESSION"
        add_note "session defaulted to current tmux session '$SESSION_NAME'"
      fi
    fi
    if [[ -z "$SESSION_NAME" ]]; then
      add_note "$SESSION_COUNT sessions under .loop/sessions/ — pass --session; restart counts read 0"
    fi
  fi
fi

# ---- restarts_24h / giveups_24h ------------------------------------------------
RESTARTS_24H=0
GIVEUPS_24H=0
RESTARTS_FILE=""
[[ -n "$SESSION_NAME" ]] && RESTARTS_FILE="$SESSIONS_DIR/$SESSION_NAME/lane-restarts.jsonl"
if [[ -n "$RESTARTS_FILE" && -f "$RESTARTS_FILE" ]]; then
  # Restart lines carry NO `event` field ({timestamp, session, lane, target,
  # cmd}); lifecycle lines carry `event` (e.g. "giving-up") — CONTRACT.md.
  read -r RESTARTS_24H GIVEUPS_24H <<EOF
$(python3 - "$RESTARTS_FILE" <<'PYEOF'
import datetime
import json
import sys

cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
restarts = giveups = 0
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        try:
            when = datetime.datetime.strptime(
                rec.get("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            continue
        if "event" not in rec:
            restarts += 1
        elif rec["event"] == "giving-up":
            giveups += 1
print(restarts, giveups)
PYEOF
)
EOF
elif [[ -n "$SESSION_NAME" ]]; then
  add_note "no lane-restarts.jsonl for session '$SESSION_NAME' — restarts_24h/giveups_24h read 0"
elif [[ ! -d "$SESSIONS_DIR" ]]; then
  add_note "no .loop/sessions/ directory — restarts_24h/giveups_24h read 0"
fi

# ---- log.md prefix counts ------------------------------------------------------
INGESTS_7D=0
LINTS_7D=0
CHECKPOINTS_7D=0
EXPERIMENTS=0
if [[ -f "$LOG_FILE" ]]; then
  read -r INGESTS_7D LINTS_7D CHECKPOINTS_7D EXPERIMENTS <<EOF
$(python3 - "$LOG_FILE" <<'PYEOF'
import datetime
import re
import sys

cutoff = datetime.date.today() - datetime.timedelta(days=7)
counts = {"ingest": 0, "lint": 0, "checkpoint": 0}
experiments = 0
pat = re.compile(r"^## \[(\d{4})-(\d{2})-(\d{2})\] ([a-z-]+) \|")
with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        m = pat.match(line)
        if not m:
            continue
        try:
            day = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        kind = m.group(4)
        if kind == "experiment":
            experiments += 1
        elif kind in counts and day >= cutoff:
            counts[kind] += 1
print(counts["ingest"], counts["lint"], counts["checkpoint"], experiments)
PYEOF
)
EOF
else
  add_note "no ops-wiki/log.md — ingests_7d/lints_7d/checkpoints_7d/experiments read 0"
fi

# ---- summary block --------------------------------------------------------------
TODAY="$(date +%Y-%m-%d)"
SESSION_LABEL="${SESSION_NAME:-none}"
echo "loop-metrics — $TODAY (session: $SESSION_LABEL)"
echo "  checkpoint_tokens: ${CHECKPOINT_TOKENS}${CHECKPOINT_DETAIL}"
echo "  pending_messages:  $PENDING"
echo "  restarts_24h:      $RESTARTS_24H"
echo "  giveups_24h:       $GIVEUPS_24H"
echo "  ingests_7d:        $INGESTS_7D"
echo "  lints_7d:          $LINTS_7D"
echo "  checkpoints_7d:    $CHECKPOINTS_7D"
echo "  experiments:       $EXPERIMENTS"
echo "  dispatches:        n/a (not derivable from substrate surfaces; out of scope)"
if [[ -n "$NOTES" ]]; then
  echo "notes:"
  printf '%s' "$NOTES"
fi

# ---- --log: append exactly one metrics entry -------------------------------------
if [[ "$DO_LOG" -eq 1 ]]; then
  if [[ ! -f "$LOG_FILE" ]]; then
    echo "error: --log needs $LOG_FILE" >&2
    exit 1
  fi
  SUMMARY="tokens=$CHECKPOINT_TOKENS pending=$PENDING restarts24h=$RESTARTS_24H giveups24h=$GIVEUPS_24H ingests7d=$INGESTS_7D lints7d=$LINTS_7D checkpoints7d=$CHECKPOINTS_7D experiments=$EXPERIMENTS"
  printf '\n## [%s] metrics | %s\n' "$TODAY" "$SUMMARY" >> "$LOG_FILE"
  echo "logged: ## [$TODAY] metrics | $SUMMARY" >&2
fi
