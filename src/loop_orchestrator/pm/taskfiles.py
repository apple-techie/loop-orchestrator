"""Read/write helpers for the T0004 task-file convention (AGENTS.md
'### Task files').

This module is the PM layer's writer of task files and of ops-wiki/log.md
sync lines — that is its documented job per T0004 (it is NOT the deck, which
stays a non-writer). ops-wiki/log.md is append-only: helpers here only ever
add lines, never rewrite existing content.

Conflict rule: the FILE WINS. Remote-vs-file divergence on a file carrying a
`jira:` key never overwrites the file; it is recorded as a conflict and
appended to ops-wiki/log.md as
'## [YYYY-MM-DD] sync | <issue-key> conflict: file wins (<detail>)'.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

FRONTMATTER_KEY_ORDER = ("id", "title", "status", "depends_on", "loop", "jira", "scope")

_TASK_ID_RE = re.compile(r"^T(\d{4})-")
# Plain YAML scalars that round-trip unquoted: no indicators, no ': '/' #',
# no leading/trailing space, not a bool/null/number lookalike.
_NEEDS_QUOTE_RE = re.compile(r"(^[\s'\"#&*!|>%@`?,{}\[\]-])|[:#]\s|:$|\s$|[\n\t]")
_NUMBER_RE = re.compile(r"-?\d+(\.\d+)?")
_RESERVED_WORDS = frozenset({"true", "false", "null", "yes", "no", "on", "off", "~"})


def slugify(text: str, max_len: int = 60) -> str:
    """Lowercase-alphanumerics-and-hyphens slug per the tasks/ convention."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def list_tasks(tasks_dir: str | Path) -> list[Path]:
    """Task files under tasks_dir and tasks_dir/archive; README.md is exempt."""
    tasks_dir = Path(tasks_dir)
    out: list[Path] = []
    for directory in (tasks_dir, tasks_dir / "archive"):
        if not directory.is_dir():
            continue
        out.extend(path for path in sorted(directory.glob("*.md")) if path.name != "README.md")
    return out


def next_task_id(tasks_dir: str | Path) -> str:
    """'T<NNNN>' one past the highest id across tasks/ and tasks/archive/."""
    highest = 0
    for path in list_tasks(tasks_dir):
        match = _TASK_ID_RE.match(path.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"T{highest + 1:04d}"


def split_task(path: str | Path) -> tuple[dict, str]:
    """(frontmatter mapping, body) — body is everything after the closing
    '---' line, byte-preserved (including its leading newline)."""
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: no frontmatter (file must start with '---')")
    end = text.find("\n---\n", 3)
    if end < 0:
        raise ValueError(f"{path}: unterminated frontmatter")
    raw = yaml.safe_load(text[4 : end + 1])
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: frontmatter is not a mapping")
    return raw, text[end + 5 :]


def parse_frontmatter(path: str | Path) -> dict:
    return split_task(path)[0]


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    if (
        not text
        or _NEEDS_QUOTE_RE.search(text)
        or text.lower() in _RESERVED_WORDS
        or _NUMBER_RE.fullmatch(text)
    ):
        return json.dumps(text, ensure_ascii=False)  # JSON strings are valid YAML
    return text


def render_frontmatter(frontmatter: dict) -> str:
    """Canonical frontmatter block: known keys in convention order, inline
    lists for depends_on, values quoted only when YAML requires it."""
    keys = [key for key in FRONTMATTER_KEY_ORDER if key in frontmatter]
    keys += [key for key in frontmatter if key not in FRONTMATTER_KEY_ORDER]
    lines = ["---"]
    for key in keys:
        value = frontmatter[key]
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(_scalar(item) for item in value)}]")
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def write_task(path: str | Path, frontmatter: dict, body: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if body and not body.startswith("\n"):
        body = "\n" + body
    path.write_text(render_frontmatter(frontmatter) + body, encoding="utf-8")


def update_frontmatter(path: str | Path, **changes: object) -> None:
    """Re-render the frontmatter with `changes` applied; body byte-preserved."""
    frontmatter, body = split_task(path)
    frontmatter.update(changes)
    Path(path).write_text(render_frontmatter(frontmatter) + body, encoding="utf-8")


def find_by_jira(tasks_dir: str | Path, issue_key: str) -> Path | None:
    """The task file (tasks/ or archive/) whose frontmatter has jira: <key>."""
    for path in list_tasks(tasks_dir):
        try:
            frontmatter = parse_frontmatter(path)
        except ValueError:
            continue
        if frontmatter.get("jira") == issue_key:
            return path
    return None


# ── ops-wiki/log.md (append-only) ────────────────────────────────────────────


def log_path(project_root: str | Path) -> Path:
    return Path(project_root) / "ops-wiki" / "log.md"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def append_log(log_md: Path, line: str) -> None:
    """Append one entry; never rewrites existing content (append-only file)."""
    log_md.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    try:
        existing = log_md.read_bytes()
        if existing and not existing.endswith(b"\n"):
            prefix = "\n"
    except OSError:
        pass
    with open(log_md, "a", encoding="utf-8") as fh:
        fh.write(prefix + line.rstrip("\n") + "\n")


def record_sync(log_md: Path, issue_key: str) -> str:
    """'## [YYYY-MM-DD] sync | <issue key>' per the T0004 sync contract."""
    line = f"## [{_today()}] sync | {issue_key}"
    append_log(log_md, line)
    return line


def record_created(log_md: Path, issue_key: str, task_id: str) -> str:
    """'## [YYYY-MM-DD] sync | <key> created from <task-id>' — push created a
    remote issue for a local task that had no jira: key."""
    line = f"## [{_today()}] sync | {issue_key} created from {task_id}"
    append_log(log_md, line)
    return line


def record_conflict(log_md: Path, issue_key: str, detail: str) -> str:
    """File-wins conflict entry; the divergent file is never modified."""
    line = f"## [{_today()}] sync | {issue_key} conflict: file wins ({detail})"
    append_log(log_md, line)
    return line
