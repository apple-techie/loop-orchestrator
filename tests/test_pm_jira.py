"""JiraAdapter against recorded JSON fixtures via the injected transport.

No network, no real credentials: the FakeTransport answers from canned
payloads and records every call so write-HTTP can be asserted absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loop_orchestrator.pm import taskfiles
from loop_orchestrator.pm.jira import JiraAdapter

ADF_DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Users get logged out."}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "Repro: open two tabs."}]},
    ],
}

SEARCH_RESPONSE = {
    "issues": [
        {
            "key": "PROJ-1",
            "fields": {
                "summary": "Fix the Login Flow",
                "description": ADF_DESCRIPTION,
                "status": {"statusCategory": {"key": "indeterminate", "name": "In Progress"}},
            },
        }
    ]
}

ISSUE_STATUS_RESPONSE = {
    "key": "PROJ-1",
    "fields": {"status": {"statusCategory": {"key": "indeterminate", "name": "In Progress"}}},
}

TRANSITIONS_RESPONSE = {
    "transitions": [
        {"id": "11", "name": "to do"},
        {"id": "21", "name": "in progress"},
        {"id": "31", "name": "done"},
    ]
}

TASK_WITH_JIRA = """\
---
id: T0001
title: Fix the Login Flow
status: {status}
depends_on: []
jira: PROJ-1
scope: imported from Jira PROJ-1
---

# T0001 — Fix the Login Flow

## Objective
hand-written content the sync must never touch
"""


class FakeTransport:
    """Recorded fixtures keyed by (method, url fragment); logs every call."""

    def __init__(self, fixtures: list[tuple[str, str, dict]]):
        self.fixtures = fixtures
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        assert headers["Authorization"].startswith("Basic ")
        for fix_method, fragment, payload in self.fixtures:
            if method == fix_method and fragment in url:
                return 200, json.dumps(payload).encode("utf-8")
        return 404, b'{"errorMessages": ["no fixture for this call"]}'

    def writes(self) -> list[tuple[str, str, bytes | None]]:
        return [call for call in self.calls if call[0] != "GET"]


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "tasks" / "archive").mkdir(parents=True)
    return tmp_path


def _log_text(project: Path) -> str:
    log_md = project / "ops-wiki" / "log.md"
    return log_md.read_text(encoding="utf-8") if log_md.exists() else ""


def test_validate_env_and_available(monkeypatch: pytest.MonkeyPatch):
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    adapter = JiraAdapter()
    assert adapter.available() is False
    assert adapter.validate_env() == ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    assert adapter.validate_env() == ["JIRA_EMAIL", "JIRA_API_TOKEN"]
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    assert adapter.available() is True


def test_pull_creates_task_file(jira_env, project: Path):
    transport = FakeTransport([("GET", "/rest/api/3/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    path = project / "tasks" / "T0001-fix-the-login-flow.md"
    assert result.created == [str(path)]
    assert result.conflicts == [] and result.errors == []
    assert not result.dry_run
    frontmatter, body = taskfiles.split_task(path)
    assert frontmatter["id"] == "T0001"
    assert frontmatter["title"] == "Fix the Login Flow"
    assert frontmatter["status"] == "open"
    assert frontmatter["jira"] == "PROJ-1"
    assert frontmatter["depends_on"] == []
    assert "scope" in frontmatter
    assert "## Objective\nFix the Login Flow" in body
    assert "Users get logged out." in body  # ADF description -> Context you need
    for section in (
        "Context you need",
        "Deliverables",
        "Acceptance criteria",
        "Verification",
        "Out of scope",
    ):
        assert f"## {section}" in body
    assert "sync | PROJ-1" in _log_text(project)
    method, url, _ = transport.calls[0]
    assert method == "GET" and "jql=assignee" in url and "fields=summary" in url


def test_pull_allocates_next_id(jira_env, project: Path):
    (project / "tasks" / "T0004-existing.md").write_text(
        TASK_WITH_JIRA.format(status="open").replace("PROJ-1", "PROJ-9").replace("T0001", "T0004"),
        encoding="utf-8",
    )
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    assert result.created == [str(project / "tasks" / "T0005-fix-the-login-flow.md")]


def test_pull_divergent_status_is_conflict_file_untouched(jira_env, project: Path):
    path = project / "tasks" / "T0001-fix-the-login-flow.md"
    path.write_text(TASK_WITH_JIRA.format(status="open"), encoding="utf-8")
    before = path.read_bytes()
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")  # remote maps to in-progress

    assert result.created == [] and result.errors == []
    assert result.conflicts == ["PROJ-1: local status 'open' vs remote 'in-progress'"]
    assert path.read_bytes() == before  # file wins: byte-identical
    assert "sync | PROJ-1 conflict: file wins (local status 'open' vs remote" in _log_text(project)


def test_pull_matching_status_is_noop(jira_env, project: Path):
    path = project / "tasks" / "T0001-fix-the-login-flow.md"
    path.write_text(TASK_WITH_JIRA.format(status="in-progress"), encoding="utf-8")
    before = path.read_bytes()
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    assert result.created == [] and result.conflicts == [] and result.errors == []
    assert path.read_bytes() == before
    assert _log_text(project) == ""


def test_pull_dry_run_no_file_writes_no_log(jira_env, project: Path):
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks", dry_run=True)

    assert result.dry_run is True
    assert result.created == [str(project / "tasks" / "T0001-fix-the-login-flow.md")]
    assert list((project / "tasks").glob("T*.md")) == []
    assert _log_text(project) == ""
    assert transport.writes() == []


def test_push_transitions_issue(jira_env, project: Path):
    archive = project / "tasks" / "archive"
    (archive / "T0001-fix-the-login-flow.md").write_text(
        TASK_WITH_JIRA.format(status="done"), encoding="utf-8"
    )
    transport = FakeTransport(
        [
            ("GET", "/rest/api/3/issue/PROJ-1?fields=status", ISSUE_STATUS_RESPONSE),
            ("GET", "/rest/api/3/issue/PROJ-1/transitions", TRANSITIONS_RESPONSE),
            ("POST", "/rest/api/3/issue/PROJ-1/transitions", {}),
        ]
    )
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks")

    assert result.updated == ["PROJ-1"]
    assert result.conflicts == [] and result.errors == []
    writes = transport.writes()
    assert len(writes) == 1
    method, url, body = writes[0]
    assert method == "POST" and url.endswith("/rest/api/3/issue/PROJ-1/transitions")
    assert json.loads(body) == {"transition": {"id": "31"}}  # 'Done' matched case-insensitively
    assert "sync | PROJ-1" in _log_text(project)


def test_push_same_status_skips_transition(jira_env, project: Path):
    path = project / "tasks" / "T0001-fix-the-login-flow.md"
    path.write_text(TASK_WITH_JIRA.format(status="in-progress"), encoding="utf-8")
    transport = FakeTransport(
        [("GET", "/rest/api/3/issue/PROJ-1?fields=status", ISSUE_STATUS_RESPONSE)]
    )
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks")

    assert result.updated == [] and result.errors == []
    assert transport.writes() == []


def test_push_dry_run_no_write_http_no_log(jira_env, project: Path):
    path = project / "tasks" / "archive" / "T0001-fix-the-login-flow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TASK_WITH_JIRA.format(status="done"), encoding="utf-8")
    transport = FakeTransport(
        [
            ("GET", "/rest/api/3/issue/PROJ-1?fields=status", ISSUE_STATUS_RESPONSE),
            ("GET", "/rest/api/3/issue/PROJ-1/transitions", TRANSITIONS_RESPONSE),
        ]
    )
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks", dry_run=True)

    assert result.dry_run is True
    assert result.updated == ["PROJ-1"]  # planned, not performed
    assert transport.writes() == []  # GETs allowed, no POST
    assert _log_text(project) == ""


def test_push_dropped_is_conflict_no_http(jira_env, project: Path):
    path = project / "tasks" / "archive" / "T0001-fix-the-login-flow.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TASK_WITH_JIRA.format(status="dropped"), encoding="utf-8")
    transport = FakeTransport([])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks")

    assert result.conflicts == ["PROJ-1: status 'dropped' has no remote mapping"]
    assert result.updated == [] and result.errors == []
    assert transport.calls == []
    assert "conflict: file wins (status 'dropped' has no remote mapping)" in _log_text(project)


def test_pull_search_failure_is_error(jira_env, project: Path):
    transport = FakeTransport([])  # every call 404s
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    assert result.created == []
    assert len(result.errors) == 1 and "HTTP 404" in result.errors[0]
