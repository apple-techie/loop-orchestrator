#!/usr/bin/env bash
# loop-adr.sh — lightweight MADR (Markdown Any Decision Record) helper for
# loop-orchestrator decision loops.
#
# A decision with lasting / irreversible consequences gets a Proposed ADR
# before implementation; it may only become Accepted once it links a verify
# record AND a rollback (or canary) plan. Agents draft; a human/coord accepts.
#
# Usage:
#   loop-adr new "<title>" [--dir <adr-dir>] [--deciders "<names>"]
#   loop-adr list          [--dir <adr-dir>]
#   loop-adr show  <id>    [--dir <adr-dir>]
#   loop-adr accept <id>   [--dir <adr-dir>]   # gated on verify + rollback links
#
# ADRs live in <adr-dir> (default: docs/adr; env: LOOP_ADR_DIR) as
# NNNN-<slug>.md with a small frontmatter block (status, date, deciders,
# verify_record, canary_record, rollback) followed by the MADR skeleton.
set -euo pipefail

ADR_DIR_DEFAULT="docs/adr"

usage() {
  cat <<'EOF'
loop-adr — lightweight MADR decision records for loop-orchestrator.

Usage:
  loop-adr new "<title>" [--dir <adr-dir>] [--deciders "<names>"]
  loop-adr list          [--dir <adr-dir>]
  loop-adr show  <id>    [--dir <adr-dir>]
  loop-adr accept <id>   [--dir <adr-dir>]

Policy: a decision with lasting/irreversible consequences gets a Proposed ADR
before work starts; `accept` only succeeds once the ADR links a verify_record
AND a rollback (or canary_record). Agents draft; a human/coord accepts.

ADRs live in <adr-dir> (default: docs/adr; env: LOOP_ADR_DIR) as NNNN-<slug>.md.
EOF
}

# ─── helpers ──────────────────────────────────────────────────────────────

# Print the value of a frontmatter field ("<key>: <value>") from an ADR file.
_adr_field() {
  sed -n "s/^$2:[[:space:]]*//p" "$1" 2>/dev/null | head -n1
}

# Slugify a title: lowercase, non-alphanumeric runs -> single hyphen, trimmed.
_adr_slug() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' \
    | sed -e 's/[^a-z0-9]\{1,\}/-/g' -e 's/^-*//' -e 's/-*$//'
}

# Next zero-padded 4-digit id (max existing + 1).
_adr_next_id() {
  local dir="$1" max=0 n f
  for f in "$dir"/[0-9][0-9][0-9][0-9]-*.md; do
    [[ -e "$f" ]] || continue
    n="$(basename "$f" | sed -n 's/^\([0-9]\{4\}\)-.*/\1/p')"
    n="$((10#$n))"
    (( n > max )) && max="$n"
  done
  printf '%04d' "$(( max + 1 ))"
}

# Resolve an id (any digits) to its ADR file path, or return 1.
_adr_resolve() {
  local dir="$1" id="$2" p padded
  id="$(printf '%s' "$id" | sed 's/[^0-9]//g')"
  [[ -z "$id" ]] && return 1
  padded="$(printf '%04d' "$((10#$id))")"
  for p in "$dir/$padded"-*.md; do
    [[ -e "$p" ]] && { printf '%s' "$p"; return 0; }
  done
  return 1
}

# ─── subcommands ──────────────────────────────────────────────────────────

cmd_new() {
  local title="$1" dir="$2" deciders="$3"
  [[ -z "$title" ]] && { echo "loop-adr new: <title> required" >&2; return 1; }
  mkdir -p "$dir"
  local id slug date file
  id="$(_adr_next_id "$dir")"
  slug="$(_adr_slug "$title")"; [[ -z "$slug" ]] && slug="decision"
  date="$(date +%Y-%m-%d)"
  file="$dir/$id-$slug.md"
  [[ -e "$file" ]] && { echo "loop-adr: $file already exists" >&2; return 1; }
  cat > "$file" <<EOF
---
status: Proposed
date: $date
deciders: $deciders
verify_record:
canary_record:
rollback:
---

# $id. $title

## Context and Problem Statement

<!-- What decision is needed and why? Blast radius / reversibility? -->

## Decision Drivers

-

## Considered Options

- Option A —
- Option B —
- Option C —

## Decision Outcome

Chosen option: "TBD", because <!-- justification -->.

### Consequences

- Good:
- Bad:
- Risk / unproven assumptions:

## Pros and Cons of the Options

## More Information

<!-- Fill verify_record and rollback (or canary_record) in the frontmatter
     above before this ADR can be Accepted. -->
EOF
  echo "$file"
}

cmd_list() {
  local dir="$1" f id status title any=0
  [[ -d "$dir" ]] || { echo "(no ADR dir: $dir)"; return 0; }
  printf '%-6s %-10s %s\n' ID STATUS TITLE
  for f in "$dir"/[0-9][0-9][0-9][0-9]-*.md; do
    [[ -e "$f" ]] || continue
    id="$(basename "$f" | sed -n 's/^\([0-9]\{4\}\)-.*/\1/p')"
    status="$(_adr_field "$f" status)"; status="${status:-?}"
    title="$(sed -n 's/^# [0-9]\{1,\}\. //p' "$f" | head -n1)"
    [[ -z "$title" ]] && title="$(basename "$f")"
    printf '%-6s %-10s %s\n' "$id" "$status" "$title"
    any=1
  done
  [[ "$any" -eq 0 ]] && echo "(no ADRs in $dir)"
}

cmd_show() {
  local dir="$1" idarg="$2" file
  [[ -z "$idarg" ]] && { echo "loop-adr show: <id> required" >&2; return 1; }
  file="$(_adr_resolve "$dir" "$idarg")" || { echo "loop-adr show: no ADR matching '$idarg' in $dir" >&2; return 1; }
  cat "$file"
}

cmd_accept() {
  local dir="$1" idarg="$2" file status verify canary rollback
  [[ -z "$idarg" ]] && { echo "loop-adr accept: <id> required" >&2; return 1; }
  file="$(_adr_resolve "$dir" "$idarg")" || { echo "loop-adr accept: no ADR matching '$idarg' in $dir" >&2; return 1; }
  status="$(_adr_field "$file" status)"
  verify="$(_adr_field "$file" verify_record)"
  canary="$(_adr_field "$file" canary_record)"
  rollback="$(_adr_field "$file" rollback)"
  # Gate: a decision can only be Accepted once it is provable AND reversible.
  local missing=()
  [[ -z "$verify" ]] && missing+=("verify_record")
  [[ -z "$rollback" && -z "$canary" ]] && missing+=("rollback (or canary_record)")
  if (( ${#missing[@]} > 0 )); then
    echo "loop-adr accept: refusing — $(basename "$file") is missing: ${missing[*]}" >&2
    echo "  Fill those frontmatter links first; Accepted decisions must be provable + reversible." >&2
    return 1
  fi
  local today tmp; today="$(date +%Y-%m-%d)"
  # BSD and GNU sed disagree on -i, so edit via temp file + move.
  tmp="$(mktemp "${TMPDIR:-/tmp}/loop-adr.XXXXXX")" || { echo "loop-adr: mktemp failed" >&2; return 1; }
  sed -e 's/^status:.*/status: Accepted/' -e "s/^date:.*/date: $today/" "$file" > "$tmp"
  mv "$tmp" "$file"
  echo "loop-adr: accepted $(basename "$file") (status: ${status:-?} -> Accepted)"
}

# ─── dispatch ─────────────────────────────────────────────────────────────

sub="${1:-}"
shift || true

DIR="${LOOP_ADR_DIR:-$ADR_DIR_DEFAULT}"
DECIDERS="coord, human"
POS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)      DIR="${2:?loop-adr: --dir requires a value}"; shift 2 ;;
    --deciders) DECIDERS="${2:?loop-adr: --deciders requires a value}"; shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *)          POS+=("$1"); shift ;;
  esac
done
set -- ${POS[@]+"${POS[@]}"}

case "$sub" in
  new)            cmd_new "${1:-}" "$DIR" "$DECIDERS" ;;
  list)           cmd_list "$DIR" ;;
  show)           cmd_show "$DIR" "${1:-}" ;;
  accept)         cmd_accept "$DIR" "${1:-}" ;;
  -h|--help|"")   usage ;;
  *)              echo "loop-adr: unknown subcommand: $sub" >&2; usage >&2; exit 1 ;;
esac
