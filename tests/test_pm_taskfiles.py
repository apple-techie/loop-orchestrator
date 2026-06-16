"""taskfiles: parse/create/next-id round-trips + the file-wins conflict log."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from loop_orchestrator.pm import taskfiles

TASK_TEXT = """\
---
id: T0001
title: Bootstrap the wiki
status: open
depends_on: [T0002, T0003]
jira: PROJ-7
scope: one line
---

# T0001 — Bootstrap the wiki

## Objective
body text stays byte-identical
"""


def _write(tasks_dir: Path, name: str, text: str = TASK_TEXT) -> Path:
    path = tasks_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_frontmatter(tmp_path: Path):
    path = _write(tmp_path / "tasks", "T0001-bootstrap-the-wiki.md")
    frontmatter = taskfiles.parse_frontmatter(path)
    assert frontmatter == {
        "id": "T0001",
        "title": "Bootstrap the wiki",
        "status": "open",
        "depends_on": ["T0002", "T0003"],
        "jira": "PROJ-7",
        "scope": "one line",
    }


def test_parse_frontmatter_rejects_missing(tmp_path: Path):
    path = _write(tmp_path / "tasks", "T0001-x.md", "# no frontmatter\n")
    with pytest.raises(ValueError, match="no frontmatter"):
        taskfiles.parse_frontmatter(path)


def test_split_task_reraises_malformed_yaml_as_value_error(tmp_path: Path):
    """F11 (T0036): unparseable frontmatter YAML must surface as ValueError, not
    a bare yaml.YAMLError — every caller guards on ValueError, and YAMLError is
    NOT a ValueError subclass, so a raw YAMLError escapes them all."""
    path = _write(
        tmp_path / "tasks",
        "T0001-x.md",
        "---\nid: T0001\ntitle: bad: unquoted: colons\nstatus: open\n---\n\nbody\n",
    )
    with pytest.raises(ValueError, match="frontmatter"):
        taskfiles.split_task(path)


def test_split_write_round_trip(tmp_path: Path):
    path = _write(tmp_path / "tasks", "T0001-bootstrap-the-wiki.md")
    frontmatter, body = taskfiles.split_task(path)
    copy = tmp_path / "tasks" / "T0001-copy.md"
    taskfiles.write_task(copy, frontmatter, body)
    frontmatter2, body2 = taskfiles.split_task(copy)
    assert frontmatter2 == frontmatter
    assert body2 == body  # body byte-identical through the round trip


def test_update_frontmatter_preserves_body(tmp_path: Path):
    path = _write(tmp_path / "tasks", "T0001-bootstrap-the-wiki.md")
    _, body_before = taskfiles.split_task(path)
    taskfiles.update_frontmatter(path, status="in-progress")
    frontmatter, body_after = taskfiles.split_task(path)
    assert frontmatter["status"] == "in-progress"
    assert frontmatter["title"] == "Bootstrap the wiki"
    assert body_after == body_before


def test_unsafe_title_round_trips_quoted(tmp_path: Path):
    title = "fix: login fails — #42 [urgent]"
    path = tmp_path / "tasks" / "T0002-fix-login.md"
    taskfiles.write_task(path, {"id": "T0002", "title": title, "depends_on": []}, "\nbody\n")
    assert taskfiles.parse_frontmatter(path)["title"] == title


def test_list_tasks_includes_archive_skips_readme(tmp_path: Path):
    tasks_dir = tmp_path / "tasks"
    open_task = _write(tasks_dir, "T0002-open.md")
    archived = _write(tasks_dir / "archive", "T0001-archived.md")
    _write(tasks_dir, "README.md", "# not a task\n")
    assert taskfiles.list_tasks(tasks_dir) == [open_task, archived]
    assert taskfiles.list_tasks(tmp_path / "missing") == []


def test_next_task_id(tmp_path: Path):
    tasks_dir = tmp_path / "tasks"
    assert taskfiles.next_task_id(tasks_dir) == "T0001"
    _write(tasks_dir, "T0003-a.md")
    _write(tasks_dir / "archive", "T0007-b.md")
    assert taskfiles.next_task_id(tasks_dir) == "T0008"


def test_find_by_jira(tmp_path: Path):
    tasks_dir = tmp_path / "tasks"
    path = _write(tasks_dir, "T0001-bootstrap-the-wiki.md")
    assert taskfiles.find_by_jira(tasks_dir, "PROJ-7") == path
    assert taskfiles.find_by_jira(tasks_dir, "PROJ-404") is None


def test_slugify():
    assert taskfiles.slugify("Fix the Login Flow!") == "fix-the-login-flow"
    assert taskfiles.slugify("  --weird__ INPUT 42  ") == "weird-input-42"
    assert taskfiles.slugify("???") == "task"
    assert len(taskfiles.slugify("x" * 200)) <= 60


def test_record_conflict_appends_file_wins_line(tmp_path: Path):
    log_md = tmp_path / "ops-wiki" / "log.md"
    existing = "# ops log\n\n## [2026-06-01] schema | initial\n"
    log_md.parent.mkdir(parents=True)
    log_md.write_text(existing, encoding="utf-8")

    line = taskfiles.record_conflict(log_md, "PROJ-7", "local status 'open' vs remote 'done'")

    content = log_md.read_text(encoding="utf-8")
    assert content.startswith(existing)  # append-only: prefix byte-identical
    assert re.fullmatch(
        r"## \[\d{4}-\d{2}-\d{2}\] sync \| PROJ-7 conflict: "
        r"file wins \(local status 'open' vs remote 'done'\)",
        line,
    )
    assert content == existing + line + "\n"


def test_record_sync_creates_log(tmp_path: Path):
    log_md = tmp_path / "ops-wiki" / "log.md"
    line = taskfiles.record_sync(log_md, "PROJ-9")
    assert re.fullmatch(r"## \[\d{4}-\d{2}-\d{2}\] sync \| PROJ-9", line)
    assert log_md.read_text(encoding="utf-8") == line + "\n"


def test_append_log_terminates_torn_last_line(tmp_path: Path):
    log_md = tmp_path / "ops-wiki" / "log.md"
    log_md.parent.mkdir(parents=True)
    log_md.write_text("torn line without newline", encoding="utf-8")
    taskfiles.append_log(log_md, "## [2026-06-10] sync | PROJ-1")
    assert log_md.read_text(encoding="utf-8") == (
        "torn line without newline\n## [2026-06-10] sync | PROJ-1\n"
    )
