#!/usr/bin/env bash
# loop-task-lint.sh — validate the tasks-as-files convention (AGENTS.md
# "Task files").
#
# Checks every task file under tasks/ and tasks/archive/ (README.md exempt):
#   - filename matches T<NNNN>-<slug>.md (lowercase slug)
#   - frontmatter present with required keys id, title, status, depends_on,
#     scope (loop and jira are optional and may be absent)
#   - id matches T<NNNN> and the filename prefix; ids are unique
#   - status is one of open|in-progress|review|done|dropped
#   - status<->location invariant: open/in-progress/review live in tasks/,
#     done/dropped live in tasks/archive/
#   - required body sections (## headings; a trailing annotation after the
#     section name is allowed): Objective, Context you need, Deliverables,
#     Acceptance criteria, Verification, Out of scope
#   - every depends_on id exists as a task file, and the dependency graph
#     is acyclic
#
# Prints a one-line summary and exits 0 when clean; prints per-file findings
# and exits non-zero on any violation.

set -euo pipefail

TASKS_DIR=""

# Resolve this script's real directory (symlink-aware) so the default tasks
# dir (<repo>/tasks, repo = this script's parent-of-parent directory) is
# correct even when the script is invoked from anywhere or via a symlink.
_lint_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}

# Default project root for the tasks dir: prefer the git working tree we are run
# from (correct in a git worktree), then $PWD when it holds tasks/, else the
# legacy install-relative parent-of-parent. An explicit --tasks-dir always wins.
_lint_default_root() {
  local top
  if top="$(git rev-parse --show-toplevel 2>/dev/null)" && [[ -n "$top" ]]; then
    printf '%s\n' "$top"; return 0
  fi
  if [[ -d "$PWD/tasks" ]]; then printf '%s\n' "$PWD"; return 0; fi
  (cd "$(_lint_script_dir)/.." && pwd)
}

usage() {
  cat <<'EOF'
Usage:
  loop-task-lint.sh [options]

Validates task files against the convention in AGENTS.md "Task files":
filename T<NNNN>-<slug>.md, required frontmatter keys (id, title, status,
depends_on, scope; loop/jira optional), required body sections, the
status<->location invariant (open/in-progress/review in tasks/, done/dropped in
tasks/archive/), and depends_on ids that exist with no cycles.

Options:
  --tasks-dir <path>  Directory holding T<NNNN>-<slug>.md files; its archive/
                      subdirectory is linted too. README.md is skipped.
                      default: <repo>/tasks relative to this script
  -h, --help          Show this help

Exit status: 0 and a one-line summary when clean; 1 with one finding per
line ("<file>: <problem>") otherwise.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

if [[ -z "$TASKS_DIR" ]]; then
  TASKS_DIR="$(_lint_default_root)/tasks"
fi
if [[ ! -d "$TASKS_DIR" ]]; then
  echo "error: tasks dir not found: $TASKS_DIR" >&2
  exit 1
fi
ARCHIVE_DIR="$TASKS_DIR/archive"

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/loop-task-lint.XXXXXX")"
trap 'rm -rf "$TMP_DIR"' EXIT
NODES="$TMP_DIR/nodes"        # one task id per line
EDGES="$TMP_DIR/edges"        # "<id> <dep> <file>" per line
RESOLVED="$TMP_DIR/resolved"  # ids proven cycle-free
FILES="$TMP_DIR/files"        # task files to lint, one path per line
: > "$NODES"
: > "$EDGES"
: > "$RESOLVED"

FINDINGS=0
finding() {  # $1 = file (or dir for graph-level findings), $2 = message
  echo "$1: $2"
  FINDINGS=$((FINDINGS + 1))
}

lint_file() {  # $1 = path, $2 = location (tasks|archive)
  local f="$1" where="$2"
  local base id_val status_val deps_line deps_inner dep fm fm_end key sec
  base="$(basename "$f")"

  if ! printf '%s\n' "$base" | grep -Eq '^T[0-9]{4}-[a-z0-9][a-z0-9-]*\.md$'; then
    finding "$f" "filename must match T<NNNN>-<slug>.md (lowercase slug)"
  fi

  if [[ "$(sed -n '1p' "$f")" != "---" ]]; then
    finding "$f" "missing frontmatter (first line must be ---)"
    return 0
  fi
  fm_end="$(awk 'NR > 1 && /^---[[:space:]]*$/ { print NR; exit }' "$f")"
  if [[ -z "$fm_end" ]]; then
    finding "$f" "unterminated frontmatter (no closing ---)"
    return 0
  fi
  fm="$(awk -v end="$fm_end" 'NR > 1 && NR < end' "$f")"

  # Required keys; loop: and jira: are optional and not checked for absence.
  for key in id title status depends_on scope; do
    if ! printf '%s\n' "$fm" | grep -q "^${key}:"; then
      finding "$f" "frontmatter missing required key '${key}'"
    fi
  done

  id_val="$(printf '%s\n' "$fm" | sed -n 's/^id:[[:space:]]*//p' | head -1)"
  if [[ -n "$id_val" ]]; then
    if ! printf '%s\n' "$id_val" | grep -Eq '^T[0-9]{4}$'; then
      finding "$f" "id '$id_val' must match T<NNNN>"
      id_val=""
    elif [[ "$base" != "${id_val}-"* ]]; then
      finding "$f" "id '$id_val' does not match filename prefix"
    fi
  fi
  if [[ -n "$id_val" ]]; then
    if grep -q "^${id_val}\$" "$NODES"; then
      finding "$f" "duplicate task id '$id_val'"
    else
      printf '%s\n' "$id_val" >> "$NODES"
    fi
  fi

  status_val="$(printf '%s\n' "$fm" | sed -n 's/^status:[[:space:]]*//p' | head -1)"
  if [[ -n "$status_val" ]]; then
    case "$status_val" in
      open|in-progress|review)
        if [[ "$where" == "archive" ]]; then
          finding "$f" "status '$status_val' but file is in tasks/archive/ (open/in-progress/review live in tasks/)"
        fi
        ;;
      done|dropped)
        if [[ "$where" == "tasks" ]]; then
          finding "$f" "status '$status_val' but file is in tasks/ (done/dropped live in tasks/archive/)"
        fi
        ;;
      *)
        finding "$f" "status '$status_val' not one of open|in-progress|review|done|dropped"
        ;;
    esac
  fi

  deps_line="$(printf '%s\n' "$fm" | grep '^depends_on:' | head -1 || true)"
  if [[ -n "$deps_line" ]]; then
    if ! printf '%s\n' "$deps_line" | grep -Eq '^depends_on:[[:space:]]*\[[^][]*\][[:space:]]*$'; then
      finding "$f" "depends_on must be an inline list like [] or [T0001, T0002]"
    else
      deps_inner="$(printf '%s\n' "$deps_line" | sed 's/^depends_on:[[:space:]]*\[//; s/\][[:space:]]*$//')"
      for dep in $(printf '%s\n' "$deps_inner" | tr ',' ' '); do
        if ! printf '%s\n' "$dep" | grep -Eq '^T[0-9]{4}$'; then
          finding "$f" "depends_on entry '$dep' must match T<NNNN>"
        elif [[ -n "$id_val" ]]; then
          printf '%s %s %s\n' "$id_val" "$dep" "$f" >> "$EDGES"
        fi
      done
    fi
  fi

  # Required body sections. Match on the heading prefix so an annotated
  # heading like "## Context you need (no other context exists)" passes.
  for sec in "Objective" "Context you need" "Deliverables" "Acceptance criteria" "Verification" "Out of scope"; do
    if ! grep -q "^## ${sec}" "$f"; then
      finding "$f" "missing required section '## ${sec}'"
    fi
  done
}

# ---- pass 1: per-file structural checks -------------------------------------
find "$TASKS_DIR" -maxdepth 1 -type f -name '*.md' ! -name 'README.md' | sort > "$FILES"
if [[ -d "$ARCHIVE_DIR" ]]; then
  find "$ARCHIVE_DIR" -maxdepth 1 -type f -name '*.md' ! -name 'README.md' | sort >> "$FILES"
fi
FILE_COUNT="$(wc -l < "$FILES" | tr -d ' ')"

while IFS= read -r f; do
  if [[ "$(dirname "$f")" == "$ARCHIVE_DIR" ]]; then
    lint_file "$f" archive
  else
    lint_file "$f" tasks
  fi
done < "$FILES"

# ---- pass 2: depends_on ids must exist --------------------------------------
while IFS=' ' read -r src dep src_file; do
  if ! grep -q "^${dep}\$" "$NODES"; then
    finding "$src_file" "depends_on '$dep' does not exist as a task file"
  fi
done < "$EDGES"

# ---- pass 3: cycle detection (iterative elimination, bash 3.2 safe) ---------
# Repeatedly resolve any id whose known-existing deps are all resolved
# (missing deps were reported in pass 2 and do not block). Ids left
# unresolved when a pass makes no progress sit on or behind a cycle.
changed=1
while [[ "$changed" -eq 1 ]]; do
  changed=0
  while IFS= read -r node; do
    if grep -q "^${node}\$" "$RESOLVED"; then
      continue
    fi
    blocked=0
    while IFS=' ' read -r src dep _; do
      if [[ "$src" != "$node" ]]; then
        continue
      fi
      if ! grep -q "^${dep}\$" "$NODES"; then
        continue
      fi
      if ! grep -q "^${dep}\$" "$RESOLVED"; then
        blocked=1
        break
      fi
    done < "$EDGES"
    if [[ "$blocked" -eq 0 ]]; then
      printf '%s\n' "$node" >> "$RESOLVED"
      changed=1
    fi
  done < "$NODES"
done

cycle_ids=""
while IFS= read -r node; do
  if ! grep -q "^${node}\$" "$RESOLVED"; then
    cycle_ids="$cycle_ids $node"
  fi
done < "$NODES"
if [[ -n "$cycle_ids" ]]; then
  finding "$TASKS_DIR" "depends_on cycle detected involving:$cycle_ids"
fi

# ---- verdict -----------------------------------------------------------------
if [[ "$FINDINGS" -gt 0 ]]; then
  echo "FAIL: $FINDINGS finding(s) across $FILE_COUNT task file(s) in $TASKS_DIR" >&2
  exit 1
fi
echo "ok: $FILE_COUNT task file(s) pass lint ($TASKS_DIR)"
