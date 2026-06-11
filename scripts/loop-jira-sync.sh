#!/usr/bin/env bash
# loop-jira-sync.sh — bidirectional sync between tasks/ files and Jira. SHIM.
#
# Task files are the source of truth (AGENTS.md "Task files"); Jira is a thin
# sync target, not the brain. The implementation lives in the optional Python
# layer (`loop-pm sync --adapter jira`); this script keeps the stable bash CLI
# surface and execs into it. When loop-pm is not on PATH, it exits 64
# ("implementation unavailable" — the stable no-Python contract, CONTRACT.md).
#
# Contract (implemented by the Python jira adapter):
#   pull — for each Jira issue assigned to this project, create the matching
#          tasks/T<NNNN>-<slug>.md: issue key -> frontmatter `jira:`,
#          summary -> `title:`. Issues with no matching file get a new task
#          file with the required body sections stubbed for a human/agent to
#          complete. Files moved by the status<->location invariant
#          (done/dropped -> tasks/archive/) follow loop-task-lint.sh rules.
#   push — for each task file with a `jira:` key, transition the Jira issue
#          so its status matches the file's frontmatter `status:`.
#   both — pull, then push, in that order.
#
# Conflict rule: when file and Jira disagree on any synced field, the FILE
# WINS. pull never overwrites a local field that differs from Jira; the
# divergence is pushed back to Jira on the next push.
#
# Logging: every issue created or updated in either direction appends
#   ## [YYYY-MM-DD] sync | <issue key>
# to ops-wiki/log.md (append-only log; one entry per issue per run).
#
# Credentials: NEVER stored in this repo, this script, or task files. The
# implementation reads endpoint + auth from the environment only
# (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN) and fails fast with a clear
# error when they are unset.
#
# --dry-run must print the planned file writes / Jira transitions without
# performing any of them (no task-file writes, no Jira calls that mutate,
# no ops-wiki/log.md entries).

set -euo pipefail

MODE=""
DRY_RUN=0
TASKS_DIR=""

# Resolve this script's real directory (symlink-aware) so the default tasks
# dir (<repo>/tasks, repo = this script's parent-of-parent directory) is
# correct even when the script is invoked from anywhere or via a symlink.
_jira_sync_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}

usage() {
  cat <<'EOF'
Usage:
  loop-jira-sync.sh [options] <mode>

Modes (exactly one required):
  pull      Create/update task files from assigned Jira issues
            (issue key -> jira:, status -> status:, summary -> title:)
  push      Update Jira issues from task-file frontmatter
            (transition status, update summary from title:)
  both      pull, then push, in that order

Options:
  --dry-run           Print planned changes without performing any of them
                      (no task-file writes, no Jira mutations, no
                      ops-wiki/log.md entries)
  --tasks-dir <path>  Directory holding T<NNNN>-<slug>.md files (archive/
                      subdirectory included)
                      default: <repo>/tasks relative to this script
  -h, --help          Show this help

Contract:
  Task files are the source of truth; on any conflict the FILE wins (pull
  never overwrites a divergent local field — the divergence is pushed back
  on the next push). Every synced issue is logged to ops-wiki/log.md as
  '## [YYYY-MM-DD] sync | <issue key>'. Credentials come from the
  environment only (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN) — never from
  this repo or task files.

Status: shim. Delegates to 'loop-pm sync --adapter jira' (the optional
Python layer). When loop-pm is not on PATH this script exits 64
(implementation unavailable; install with: make install-python).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    pull|push|both)
      if [[ -n "$MODE" ]]; then
        echo "error: mode '$1' conflicts with '$MODE'" >&2
        exit 1
      fi
      MODE="$1"; shift
      ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --tasks-dir) TASKS_DIR="$2"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
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

if [[ -z "$MODE" ]]; then
  echo "error: one of pull|push|both is required" >&2
  usage >&2
  exit 1
fi

REPO_ROOT="$(cd "$(_jira_sync_script_dir)/.." && pwd)"
if [[ -z "$TASKS_DIR" ]]; then
  TASKS_DIR="$REPO_ROOT/tasks"
fi
if [[ ! -d "$TASKS_DIR" ]]; then
  echo "error: tasks dir not found: $TASKS_DIR" >&2
  exit 1
fi

# SHIM BODY — translate this script's flags into the Python implementation.
# No loop-pm on PATH = the stable no-Python case: actionable hint, exit 64.
if ! command -v loop-pm >/dev/null 2>&1; then
  echo "loop-jira-sync: implementation unavailable — 'loop-pm' is not on PATH" >&2
  echo "install the optional Python layer to enable Jira sync: make install-python" >&2
  exit 64
fi

PM_ARGS=(sync --adapter jira "$MODE" --tasks-dir "$TASKS_DIR" --project-root "$REPO_ROOT")
if [[ "$DRY_RUN" -eq 1 ]]; then
  PM_ARGS+=(--dry-run)
fi
exec loop-pm "${PM_ARGS[@]}"
