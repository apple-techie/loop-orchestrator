"""JiraAdapter against recorded JSON fixtures via the injected transport.

No network, no real credentials: the FakeTransport answers from canned
payloads and records every call so write-HTTP can be asserted absent.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from loop_orchestrator.pm import taskfiles
from loop_orchestrator.pm.jira import JiraAdapter, JiraError

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
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    monkeypatch.delenv("JIRA_BOARD_ID", raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "tasks" / "archive").mkdir(parents=True)
    return tmp_path


def _log_text(project: Path) -> str:
    log_md = project / "ops-wiki" / "log.md"
    return log_md.read_text(encoding="utf-8") if log_md.exists() else ""


def test_http_base_url_rejected_before_any_transport_call(project: Path, monkeypatch):
    # FIX 4 (MEDIUM-4): the token must never leave over an unencrypted channel.
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.delenv("JIRA_ALLOW_INSECURE_BASE_URL", raising=False)
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    assert len(result.errors) == 1 and "not https" in result.errors[0]
    assert transport.calls == []  # transport never invoked — token never built/sent


def test_https_base_url_allows_request(jira_env, project: Path):
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    assert result.errors == []
    assert transport.calls  # the https path reaches the transport


def test_insecure_escape_hatch_allows_http(project: Path, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("JIRA_ALLOW_INSECURE_BASE_URL", "1")
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    assert result.errors == []
    assert transport.calls  # localhost dev escape hatch reaches the transport


def test_embedded_credentials_in_base_url_rejected(project: Path, monkeypatch):
    # A poisoned authority (userinfo '@') is rejected even over https and even
    # with the insecure escape hatch — credentials must never be smuggled in.
    monkeypatch.setenv("JIRA_BASE_URL", "https://attacker@evil.example.com")
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("JIRA_ALLOW_INSECURE_BASE_URL", "1")
    transport = FakeTransport([("GET", "/search/jql?", SEARCH_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.pull(project / "tasks")

    assert len(result.errors) == 1 and "credentials" in result.errors[0]
    assert transport.calls == []


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
    assert "no fixture for this call" in result.errors[0]  # errorMessages surfaced


# ── scrum: epics / sprints / comments ────────────────────────────────────────

EPIC_SEARCH_RESPONSE = {
    "issues": [
        {"key": "PROJ-100", "fields": {"summary": "Sprint Goals v2"}},
        {"key": "PROJ-42", "fields": {"summary": "Sprint Goals"}},
    ]
}

CREATED_RESPONSE = {"id": "10001", "key": "PROJ-77"}

ACTIVE_SPRINT_RESPONSE = {"values": [{"id": 7, "name": "Sprint 12", "state": "active"}]}

# Deliberately NOT in id order: --next must resolve the EARLIEST future sprint.
FUTURE_SPRINTS_RESPONSE = {
    "values": [
        {"id": 9, "name": "Sprint 14", "state": "future"},
        {"id": 8, "name": "Sprint 13", "state": "future"},
    ]
}

TASK_NO_JIRA = """\
---
id: T0002
title: Build the Sync
status: open
depends_on: []
scope: local-only work, not yet mirrored
---

# T0002 — Build the Sync

## Objective
Mirror loop work into Jira.

## Context you need
hand-written content the sync must never touch
"""

# F8 (T0030): the seeded form for issueless work — a PRESENT-but-blank jira: ""
# field (not merely absent). It must take the same create+backfill path, NOT a
# reconcile that 404s on a guessed key.
TASK_BLANK_JIRA = TASK_NO_JIRA.replace("depends_on: []\n", 'depends_on: []\njira: ""\n')


class ParentRejectingTransport(FakeTransport):
    """POST /issue with a 'parent' field -> 400 (company-managed project);
    without it -> created. Everything else falls through to fixtures."""

    def __call__(self, method, url, headers, body):
        if method == "POST" and url.endswith("/rest/api/3/issue"):
            self.calls.append((method, url, body))
            if "parent" in (json.loads(body).get("fields") or {}):
                detail = {"errors": {"parent": "Field 'parent' cannot be set on this project."}}
                return 400, json.dumps(detail).encode("utf-8")
            return 201, json.dumps(CREATED_RESPONSE).encode("utf-8")
        return super().__call__(method, url, headers, body)


def _jql_of(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query)["jql"][0]


def test_find_epic_exact_match(jira_env):
    transport = FakeTransport([("GET", "/rest/api/3/search/jql?", EPIC_SEARCH_RESPONSE)])
    adapter = JiraAdapter(transport=transport)

    assert adapter.find_epic("Sprint Goals", "PROJ") == "PROJ-42"  # not the fuzzy 'v2' hit
    jql = _jql_of(transport.calls[0][1])
    assert jql == 'project = PROJ AND issuetype = Epic AND summary ~ "Sprint Goals"'


def test_find_epic_miss_returns_none(jira_env):
    transport = FakeTransport([("GET", "/rest/api/3/search/jql?", EPIC_SEARCH_RESPONSE)])
    adapter = JiraAdapter(transport=transport)

    assert adapter.find_epic("Unrelated Epic", "PROJ") is None
    assert transport.writes() == []


def test_create_epic(jira_env):
    transport = FakeTransport([("POST", "/rest/api/3/issue", CREATED_RESPONSE)])
    adapter = JiraAdapter(transport=transport)

    assert adapter.create_epic("Sprint Goals", "PROJ") == "PROJ-77"
    method, url, body = transport.writes()[0]
    assert url.endswith("/rest/api/3/issue")
    fields = json.loads(body)["fields"]
    assert fields["project"] == {"key": "PROJ"}
    assert fields["issuetype"] == {"name": "Epic"}
    assert fields["summary"] == "Sprint Goals"


def test_create_issue_with_parent_and_adf_description(jira_env):
    transport = FakeTransport([("POST", "/rest/api/3/issue", CREATED_RESPONSE)])
    adapter = JiraAdapter(transport=transport)

    key, warning = adapter.create_issue("Do thing", "line one\nline two", "PROJ", "PROJ-42")

    assert (key, warning) == ("PROJ-77", None)
    fields = json.loads(transport.writes()[0][2])["fields"]
    assert fields["parent"] == {"key": "PROJ-42"}
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["description"] == {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "line one"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "line two"}]},
        ],
    }


def test_create_issue_parent_rejected_retries_without_and_warns(jira_env):
    transport = ParentRejectingTransport([])
    adapter = JiraAdapter(transport=transport)

    key, warning = adapter.create_issue("Do thing", "desc", "PROJ", epic_key="PROJ-42")

    assert key == "PROJ-77"
    assert warning is not None and "PROJ-42" in warning and "epic link" in warning
    first, second = transport.writes()
    assert "parent" in json.loads(first[2])["fields"]
    assert "parent" not in json.loads(second[2])["fields"]


def test_create_issue_non_parent_400_raises(jira_env):
    transport = FakeTransport([])  # 404 with errorMessages
    adapter = JiraAdapter(transport=transport)

    with pytest.raises(JiraError, match="no fixture"):
        adapter.create_issue("Do thing", "desc", "PROJ", epic_key="PROJ-42")
    assert len(transport.writes()) == 1  # no blind retry on non-parent errors


def test_active_sprint(jira_env):
    transport = FakeTransport([("GET", "/rest/agile/1.0/board/5/sprint", ACTIVE_SPRINT_RESPONSE)])
    adapter = JiraAdapter(transport=transport)

    assert adapter.active_sprint("5") == {"id": 7, "name": "Sprint 12"}
    method, url, _ = transport.calls[0]
    assert method == "GET" and url.endswith("/rest/agile/1.0/board/5/sprint?state=active")


def test_active_sprint_none(jira_env):
    transport = FakeTransport([("GET", "/rest/agile/1.0/board/5/sprint", {"values": []})])
    adapter = JiraAdapter(transport=transport)

    assert adapter.active_sprint("5") is None


def test_move_to_sprint_payload(jira_env):
    transport = FakeTransport([("POST", "/rest/agile/1.0/sprint/7/issue", {})])
    adapter = JiraAdapter(transport=transport)

    adapter.move_to_sprint(7, ["PROJ-1", "PROJ-2"])

    method, url, body = transport.writes()[0]
    assert url.endswith("/rest/agile/1.0/sprint/7/issue")
    assert json.loads(body) == {"issues": ["PROJ-1", "PROJ-2"]}


def test_future_sprints(jira_env):
    transport = FakeTransport([("GET", "/rest/agile/1.0/board/5/sprint", FUTURE_SPRINTS_RESPONSE)])
    adapter = JiraAdapter(transport=transport)

    sprints = adapter.future_sprints("5")

    assert sprints == [{"id": 9, "name": "Sprint 14"}, {"id": 8, "name": "Sprint 13"}]
    method, url, _ = transport.calls[0]
    assert method == "GET" and url.endswith("/rest/agile/1.0/board/5/sprint?state=future")


def test_future_sprints_empty(jira_env):
    transport = FakeTransport([("GET", "/rest/agile/1.0/board/5/sprint", {"values": []})])
    adapter = JiraAdapter(transport=transport)

    assert adapter.future_sprints("5") == []


def test_create_sprint_payload(jira_env):
    transport = FakeTransport(
        [("POST", "/rest/agile/1.0/sprint", {"id": 21, "name": "Sprint 15", "state": "future"})]
    )
    adapter = JiraAdapter(transport=transport)

    assert adapter.create_sprint("5", "Sprint 15") == {"id": 21, "name": "Sprint 15"}
    method, url, body = transport.writes()[0]
    assert method == "POST" and url.endswith("/rest/agile/1.0/sprint")
    assert json.loads(body) == {"name": "Sprint 15", "originBoardId": 5}


def test_create_sprint_non_numeric_board_is_clean_error(jira_env):
    transport = FakeTransport([])
    adapter = JiraAdapter(transport=transport)

    with pytest.raises(JiraError, match="not numeric"):
        adapter.create_sprint("board-5", "Sprint 15")
    assert transport.calls == []


def _parse_jira_date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")


def test_start_sprint_payload_state_active_with_both_dates(jira_env):
    transport = FakeTransport(
        [
            ("GET", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint 13", "state": "future"}),
            ("PUT", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint 13", "state": "active"}),
        ]
    )
    adapter = JiraAdapter(transport=transport)

    doc = adapter.start_sprint(8)

    assert doc["state"] == "active"
    method, url, body = transport.writes()[0]
    assert method == "PUT" and url.endswith("/rest/agile/1.0/sprint/8")
    payload = json.loads(body)
    assert payload["state"] == "active"
    assert payload["name"] == "Sprint 13"  # Jira requires name on the activate PUT
    assert "goal" not in payload
    start = _parse_jira_date(payload["startDate"])
    end = _parse_jira_date(payload["endDate"])
    assert end - start == timedelta(days=14)  # default duration honored
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(now - start) < timedelta(minutes=5)  # startDate is now-UTC


def test_start_sprint_duration_and_goal(jira_env):
    transport = FakeTransport(
        [
            ("GET", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint X"}),
            ("PUT", "/rest/agile/1.0/sprint/8", {}),
        ]
    )
    adapter = JiraAdapter(transport=transport)

    adapter.start_sprint(8, duration_days=7, goal="Ship the sync")

    payload = json.loads(transport.writes()[0][2])
    assert payload["goal"] == "Ship the sync"
    delta = _parse_jira_date(payload["endDate"]) - _parse_jira_date(payload["startDate"])
    assert delta == timedelta(days=7)


def test_start_sprint_jira_refusal_surfaced(jira_env):
    def already_active(method, url, headers, body):
        if method == "GET":  # the name-fetch succeeds; the activate PUT is refused
            return 200, json.dumps({"id": 8, "name": "Sprint 12"}).encode("utf-8")
        detail = {"errorMessages": ["Sprint 'Sprint 12' is already active on this board."]}
        return 400, json.dumps(detail).encode("utf-8")

    adapter = JiraAdapter(transport=already_active)

    with pytest.raises(JiraError, match="already active on this board") as excinfo:
        adapter.start_sprint(8)
    assert excinfo.value.status == 400


def test_complete_sprint_checks_active_then_closes(jira_env):
    active_sprint = {
        "id": 8,
        "name": "Sprint 13",
        "state": "active",
        "startDate": "2026-06-01T00:00:00.000Z",
        "endDate": "2026-06-15T00:00:00.000Z",
    }
    transport = FakeTransport(
        [
            ("GET", "/rest/agile/1.0/sprint/8", active_sprint),
            ("PUT", "/rest/agile/1.0/sprint/8", {**active_sprint, "state": "closed"}),
        ]
    )
    adapter = JiraAdapter(transport=transport)

    closed = adapter.complete_sprint(8)

    assert closed["state"] == "closed"
    method, url, body = transport.writes()[0]
    assert method == "PUT" and url.endswith("/rest/agile/1.0/sprint/8")
    # Jira requires name + startDate + endDate on the close PUT — all preserved.
    assert json.loads(body) == {
        "state": "closed",
        "name": "Sprint 13",
        "startDate": "2026-06-01T00:00:00.000Z",
        "endDate": "2026-06-15T00:00:00.000Z",
    }


def test_complete_sprint_not_active_is_clean_error_no_write(jira_env):
    transport = FakeTransport(
        [("GET", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint 13", "state": "future"})]
    )
    adapter = JiraAdapter(transport=transport)

    with pytest.raises(JiraError, match=r"sprint 8 \(Sprint 13\) is not active \(state: future\)"):
        adapter.complete_sprint(8)
    assert transport.writes() == []  # the close PUT never happens


def test_complete_epic_transitions_to_done_category(jira_env):
    transport = FakeTransport(
        [
            ("GET", "fields=status", {"fields": {"status": {"statusCategory": {"key": "new"}}}}),
            (
                "GET",
                "/transitions",
                {
                    "transitions": [
                        {
                            "id": "11",
                            "to": {
                                "name": "In Progress",
                                "statusCategory": {"key": "indeterminate"},
                            },
                        },
                        {"id": "31", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
                    ]
                },
            ),
            ("POST", "/transitions", {}),
        ]
    )
    adapter = JiraAdapter(transport=transport)

    # matches by statusCategory 'done', not the literal name "Done"
    assert adapter.complete_epic("SCRUM-117") == "Done"
    method, url, body = transport.writes()[0]
    assert method == "POST" and url.endswith("/rest/api/3/issue/SCRUM-117/transitions")
    assert json.loads(body) == {"transition": {"id": "31"}}


def test_complete_epic_already_done_is_noop(jira_env):
    transport = FakeTransport(
        [
            (
                "GET",
                "fields=status",
                {"fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}}},
            )
        ]
    )
    adapter = JiraAdapter(transport=transport)

    assert adapter.complete_epic("SCRUM-117") == "Done"
    assert transport.writes() == []  # no transition POST when already done


def test_complete_epic_no_done_transition_errors(jira_env):
    transport = FakeTransport(
        [
            ("GET", "fields=status", {"fields": {"status": {"statusCategory": {"key": "new"}}}}),
            (
                "GET",
                "/transitions",
                {
                    "transitions": [
                        {
                            "id": "11",
                            "to": {
                                "name": "In Progress",
                                "statusCategory": {"key": "indeterminate"},
                            },
                        }
                    ]
                },
            ),
        ]
    )
    adapter = JiraAdapter(transport=transport)

    with pytest.raises(JiraError, match="no transition to a Done-category status"):
        adapter.complete_epic("SCRUM-117")
    assert transport.writes() == []


def test_add_comment_minimal_adf(jira_env):
    transport = FakeTransport([("POST", "/comment", {})])
    adapter = JiraAdapter(transport=transport)

    adapter.add_comment("PROJ-1", "went well\n\nimprove X")

    method, url, body = transport.writes()[0]
    assert url.endswith("/rest/api/3/issue/PROJ-1/comment")
    assert json.loads(body) == {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "went well"}]},
                {"type": "paragraph", "content": []},
                {"type": "paragraph", "content": [{"type": "text", "text": "improve X"}]},
            ],
        }
    }


# ── push: creation of local-only tasks ───────────────────────────────────────


def test_push_creates_issue_and_writes_key_back(jira_env, project: Path, monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")
    path = project / "tasks" / "T0002-build-the-sync.md"
    path.write_text(TASK_NO_JIRA, encoding="utf-8")
    body_before = taskfiles.split_task(path)[1]
    transport = FakeTransport([("POST", "/rest/api/3/issue", CREATED_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks")

    assert result.created == ["PROJ-77 from T0002"]
    assert result.errors == [] and result.warnings == []
    frontmatter, body_after = taskfiles.split_task(path)
    assert frontmatter["jira"] == "PROJ-77"
    assert frontmatter["title"] == "Build the Sync" and frontmatter["status"] == "open"
    assert body_after == body_before  # body untouched, byte-for-byte
    assert "sync | PROJ-77 created from T0002" in _log_text(project)
    fields = json.loads(transport.writes()[0][2])["fields"]
    assert fields["summary"] == "Build the Sync"
    assert fields["project"] == {"key": "PROJ"}
    assert "parent" not in fields  # no --epic given
    # description = the Objective section text
    assert fields["description"]["content"][0]["content"][0]["text"] == (
        "Mirror loop work into Jira."
    )


def test_push_blank_jira_string_creates_and_backfills_no_reconcile(
    jira_env, project: Path, monkeypatch
):
    # F8 (T0030): a PRESENT-but-blank jira: "" field is issueless work — it must
    # take the CREATE path (create + backfill the true key + link epic), never a
    # reconcile GET that 404s on a non-existent key.
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")
    path = project / "tasks" / "T0002-build-the-sync.md"
    path.write_text(TASK_BLANK_JIRA, encoding="utf-8")
    assert taskfiles.parse_frontmatter(path)["jira"] == ""  # seeded blank, present
    transport = FakeTransport([("POST", "/rest/api/3/issue", CREATED_RESPONSE)])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks", epic="PROJ-42")

    assert result.created == ["PROJ-77 from T0002"]
    assert result.errors == []
    assert taskfiles.parse_frontmatter(path)["jira"] == "PROJ-77"  # backfilled
    # the epic was linked at creation
    fields = json.loads(transport.writes()[0][2])["fields"]
    assert fields["parent"] == {"key": "PROJ-42"}
    # NO reconcile happened: not a single GET against an issue status endpoint
    assert not any("?fields=status" in url for _, url, _ in transport.calls)


def test_push_set_jira_reconciles_and_never_creates(jira_env, project: Path):
    # F8 invariant other side: a non-empty jira: means "reconcile this issue" —
    # the push must take the transition path and NEVER hit the bare create POST.
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

    assert result.updated == ["PROJ-1"] and result.created == []
    # every write is a transition — the bare create endpoint is never POSTed to
    assert all(url.rstrip("/").endswith("/transitions") for _, url, _ in transport.writes())


def test_agents_md_documents_blank_jira_seeding():
    # F8 root fix is a convention: the task-authoring guidance must tell the
    # coordinator to seed jira BLANK and never guess a key. Guard it so the rule
    # cannot silently regress.
    agents_md = Path(__file__).resolve().parents[1] / "AGENTS.md"
    text = agents_md.read_text(encoding="utf-8").lower()
    assert "leave it blank for issueless work" in text
    assert "never guess or increment a key" in text


def test_push_create_under_epic_with_parent_fallback_warning(jira_env, project: Path):
    path = project / "tasks" / "T0002-build-the-sync.md"
    path.write_text(TASK_NO_JIRA, encoding="utf-8")
    transport = ParentRejectingTransport([])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks", project="PROJ", epic="PROJ-42")

    assert result.created == ["PROJ-77 from T0002"]
    assert len(result.warnings) == 1 and "PROJ-42" in result.warnings[0]
    assert result.errors == []
    assert taskfiles.parse_frontmatter(path)["jira"] == "PROJ-77"  # created despite no link


def test_push_create_dry_run_writes_nothing(jira_env, project: Path, monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")
    path = project / "tasks" / "T0002-build-the-sync.md"
    path.write_text(TASK_NO_JIRA, encoding="utf-8")
    before = path.read_bytes()
    transport = FakeTransport([])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks", dry_run=True)

    assert result.dry_run is True
    assert result.created == ["would-create T0002: Build the Sync (project PROJ)"]
    assert path.read_bytes() == before
    assert transport.calls == []  # not even a GET is needed to plan creation
    assert _log_text(project) == ""


def test_push_create_without_project_key_is_warning_not_error(jira_env, project: Path):
    path = project / "tasks" / "T0002-build-the-sync.md"
    path.write_text(TASK_NO_JIRA, encoding="utf-8")
    before = path.read_bytes()
    transport = FakeTransport([])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks")

    assert result.created == [] and result.errors == []
    assert len(result.warnings) == 1 and "JIRA_PROJECT_KEY" in result.warnings[0]
    assert transport.calls == [] and path.read_bytes() == before


def test_push_done_local_task_without_key_is_not_created(jira_env, project: Path, monkeypatch):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "PROJ")
    (project / "tasks" / "archive" / "T0002-build-the-sync.md").write_text(
        TASK_NO_JIRA.replace("status: open", "status: done"), encoding="utf-8"
    )
    transport = FakeTransport([])
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks")

    assert result.created == [] and result.warnings == [] and result.errors == []
    assert transport.calls == []


def test_push_created_issues_move_to_sprint(jira_env, project: Path):
    path = project / "tasks" / "T0002-build-the-sync.md"
    path.write_text(TASK_NO_JIRA, encoding="utf-8")
    transport = FakeTransport(
        [
            ("POST", "/rest/api/3/issue", CREATED_RESPONSE),
            ("POST", "/rest/agile/1.0/sprint/7/issue", {}),
        ]
    )
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks", project="PROJ", sprint="7")

    assert result.created == ["PROJ-77 from T0002"]
    assert result.updated == ["PROJ-77 -> sprint 7"]
    move = transport.writes()[1]
    assert move[1].endswith("/rest/agile/1.0/sprint/7/issue")
    assert json.loads(move[2]) == {"issues": ["PROJ-77"]}


def test_push_sprint_active_resolves_via_board(jira_env, project: Path, monkeypatch):
    monkeypatch.setenv("JIRA_BOARD_ID", "5")
    path = project / "tasks" / "T0002-build-the-sync.md"
    path.write_text(TASK_NO_JIRA, encoding="utf-8")
    transport = FakeTransport(
        [
            ("POST", "/rest/api/3/issue", CREATED_RESPONSE),
            ("GET", "/rest/agile/1.0/board/5/sprint", ACTIVE_SPRINT_RESPONSE),
            ("POST", "/rest/agile/1.0/sprint/7/issue", {}),
        ]
    )
    adapter = JiraAdapter(project_root=project, transport=transport)

    result = adapter.push(project / "tasks", project="PROJ", sprint="active")

    assert result.updated == ["PROJ-77 -> sprint 7"]
    assert result.errors == []
