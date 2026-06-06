#!/usr/bin/env bash
# loop-digest.sh — project-agnostic orchestrator state digest.
#
# Reads the orchestrator state file (schema v2; see README.md) and the mailbox
# directory, and prints the "orchestrator loops @ HH:MM" ASCII summary.
#
# Runs in a loop by default (refresh every --interval seconds). Pass
# --once to print a single digest and exit.

set -euo pipefail

STATE_FILE=""
MAILBOX_DIR=""
PROJECT_ROOT=""
ADR_DIR="${LOOP_DIGEST_ADR_DIR:-}"
INTERVAL="${LOOP_DIGEST_INTERVAL:-30}"
ONCE=0
NO_CLEAR=0
EXTRA_REPOS=()

usage() {
  cat <<'EOF'
Usage:
  loop-digest.sh [options]

Options:
  --state-file <path>      Orchestrator state JSON (schema v2)
                           default: <project-root>/.loop/orchestrator-state.json
  --mailbox-dir <path>     Mailbox directory (see README.md "Schema expectations")
                           default: <project-root>/.loop/messages
  --project-root <path>    Used to resolve defaults + unpushed-commits count.
                           If omitted, unpushed-commits for the primary repo
                           is skipped.
  --extra-repo <path>      Additional repo to include in unpushed-commits
                           block. Repeatable. Tries origin/<current-branch>,
                           falling back to origin/main.
  --adr-dir <path>         MADR decision-record dir for the ledger block.
                           default: <project-root>/docs/adr (env: LOOP_DIGEST_ADR_DIR)
  --interval <seconds>     Refresh interval (default: 30; env: LOOP_DIGEST_INTERVAL)
  --once                   Print once and exit (no clear, no loop)
  --no-clear               Do not clear the screen between refreshes
  -h, --help               Show this help

Examples:
  loop-digest.sh --project-root ~/code/my-app
  loop-digest.sh --state-file /path/to/state.json \
                 --mailbox-dir /path/to/messages --once
  loop-digest.sh --project-root ~/code/my-app --extra-repo ~/code/my-app-infra
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-file)     STATE_FILE="$2"; shift 2 ;;
    --mailbox-dir)    MAILBOX_DIR="$2"; shift 2 ;;
    --project-root)   PROJECT_ROOT="$2"; shift 2 ;;
    --adr-dir)        ADR_DIR="$2"; shift 2 ;;
    --extra-repo)     EXTRA_REPOS+=("$2"); shift 2 ;;
    --interval)       INTERVAL="$2"; shift 2 ;;
    --once)           ONCE=1; shift ;;
    --no-clear)       NO_CLEAR=1; shift ;;
    -h|--help)        usage; exit 0 ;;
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

# Derive defaults from --project-root.
if [[ -n "$PROJECT_ROOT" ]]; then
  : "${STATE_FILE:=$PROJECT_ROOT/.loop/orchestrator-state.json}"
  : "${MAILBOX_DIR:=$PROJECT_ROOT/.loop/messages}"
  : "${ADR_DIR:=$PROJECT_ROOT/docs/adr}"
fi

if [[ -z "$STATE_FILE" && -z "$MAILBOX_DIR" ]]; then
  echo "error: provide --project-root, or --state-file and --mailbox-dir" >&2
  usage >&2
  exit 1
fi

render_state() {
  if [[ -f "$STATE_FILE" ]]; then
    STATE_FILE="$STATE_FILE" python3 - <<'PY'
import json, os, pathlib, sys
p = pathlib.Path(os.environ["STATE_FILE"])
try:
    d = json.loads(p.read_text())
except Exception as e:
    print(f"  (failed to parse state file: {e})")
    sys.exit(0)

if not isinstance(d, dict):
    print(f"  (unexpected state shape: {type(d).__name__}, expected an object)")
    sys.exit(0)

# Schema v2 (see README.md):
#   schema_version, updated_at, loops.<id>.{status,name,branch,deployed_to,...}
# An extended shape also carried "session" + "lanes".
print(f"schema_version={d.get('schema_version','?')}  session={d.get('session','-')}  updated={d.get('updated_at','?')}")
print()

lanes = d.get("lanes") or {}
if isinstance(lanes, dict) and lanes:
    print("LANES:")
    for k, v in lanes.items():
        if not isinstance(v, dict):
            print(f"  {k:14s}  {str(v)[:30]:30s}  -")
            continue
        status = v.get("status") or v.get("cmd") or "?"
        agent  = v.get("agent") or "-"
        print(f"  {k:14s}  {str(status)[:30]:30s}  {agent}")
    print()

loops = d.get("loops") or {}
if not isinstance(loops, dict):
    loops = {}
if loops:
    print("LOOPS:")
    for k, v in loops.items():
        if not isinstance(v, dict):
            print(f"  {k:18s}  {str(v)[:24]:24s}  {'-':14s}  {'-':28s}")
            continue
        status = (v.get("status") or "?")
        name   = (v.get("name") or "")
        branch = (v.get("branch") or "-")
        deployed = v.get("deployed_to") or []
        if isinstance(deployed, list):
            depl = ",".join(str(x) for x in deployed) or "-"
        else:
            depl = str(deployed) or "-"
        # Truncate long fields so the row stays on one line-ish.
        status = str(status)[:24]
        branch = str(branch)[:14]
        depl   = depl[:28]
        print(f"  {k:18s}  {status:24s}  {branch:14s}  {depl:28s}  {name}")
else:
    print("LOOPS: (none)")
PY
  else
    echo "  (no state file at $STATE_FILE)"
  fi
}

render_mailbox() {
  if [[ -z "$MAILBOX_DIR" || ! -d "$MAILBOX_DIR" ]]; then
    echo "  (no mailbox at $MAILBOX_DIR)"
    return
  fi
  # Newest 4 messages, skipping READMEs.
  local count=0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    local base subj
    base="$(basename "$f")"
    subj="$(grep -m1 '^subject:' "$f" 2>/dev/null | sed 's/^subject: //')"
    printf "  %-40s  %s\n" "$base" "${subj:--}"
    count=$((count + 1))
    [[ "$count" -ge 4 ]] && break
  done < <(ls -t "$MAILBOX_DIR"/*.md 2>/dev/null | grep -v -i 'readme' || true)
  if [[ "$count" -eq 0 ]]; then
    echo "  (no messages)"
  fi
}

render_unpushed() {
  local any=0
  render_one() {
    local repo="$1"
    [[ -d "$repo/.git" ]] || return 0
    local branch count upstream
    branch="$(git -C "$repo" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    # Try origin/<branch>; fall back to origin/main then origin/master.
    if git -C "$repo" rev-parse --verify --quiet "origin/$branch" >/dev/null; then
      upstream="origin/$branch"
    elif git -C "$repo" rev-parse --verify --quiet "origin/main" >/dev/null; then
      upstream="origin/main"
    elif git -C "$repo" rev-parse --verify --quiet "origin/master" >/dev/null; then
      upstream="origin/master"
    else
      upstream=""
    fi
    if [[ -n "$upstream" ]]; then
      count="$(git -C "$repo" log --oneline "${upstream}..HEAD" 2>/dev/null | wc -l | tr -d ' ')"
    else
      count="?"
    fi
    printf "  %-40s  %s (%s vs %s)\n" "$(basename "$repo")" "$count" "$branch" "${upstream:-no-upstream}"
    any=1
  }
  [[ -n "$PROJECT_ROOT" ]] && render_one "$PROJECT_ROOT"
  for r in "${EXTRA_REPOS[@]+"${EXTRA_REPOS[@]}"}"; do
    render_one "$r"
  done
  if [[ "$any" -eq 0 ]]; then
    echo "  (no repos configured; pass --project-root or --extra-repo)"
  fi
}

render_adr() {
  if [[ -z "$ADR_DIR" || ! -d "$ADR_DIR" ]]; then
    echo "  (no ADRs at ${ADR_DIR:-<unset>})"
    return
  fi
  # Newest 8 decision records (highest id first).
  local f id status title count=0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    id="$(basename "$f" | sed -n 's/^\([0-9]\{4\}\)-.*/\1/p')"
    status="$(sed -n 's/^status:[[:space:]]*//p' "$f" 2>/dev/null | head -n1)"
    status="${status:-?}"
    title="$(sed -n 's/^# [0-9]\{1,\}\. //p' "$f" 2>/dev/null | head -n1)"
    [[ -z "$title" ]] && title="$(basename "$f")"
    printf "  %-6s %-10s %s\n" "$id" "$status" "${title:0:48}"
    count=$((count + 1))
    [[ "$count" -ge 8 ]] && break
  done < <(ls -1 "$ADR_DIR"/[0-9][0-9][0-9][0-9]-*.md 2>/dev/null | sort -r || true)
  if [[ "$count" -eq 0 ]]; then
    echo "  (no ADRs in $ADR_DIR)"
  fi
}

render_frame() {
  local ts
  ts="$(date +%H:%M)"
  echo "════ orchestrator loops @ ${ts} ════"
  render_state
  echo
  echo "════ latest 4 messages ════"
  render_mailbox
  echo
  echo "════ unpushed commits ════"
  render_unpushed
  echo
  echo "════ decisions (MADR) ════"
  render_adr
}

if [[ "$ONCE" -eq 1 ]]; then
  render_frame
  exit 0
fi

while true; do
  if [[ "$NO_CLEAR" -eq 0 ]]; then
    clear
  fi
  render_frame
  sleep "$INTERVAL"
done
