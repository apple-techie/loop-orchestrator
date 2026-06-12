"""loop-pm CLI over the registry override seam (no installed metadata needed);
jira scrum verbs over the injected-adapter seam (fixture transport, no HTTP)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_pm_jira import (
    ACTIVE_SPRINT_RESPONSE,
    CREATED_RESPONSE,
    EPIC_SEARCH_RESPONSE,
    FUTURE_SPRINTS_RESPONSE,
    FakeTransport,
)

from loop_orchestrator.pm import registry
from loop_orchestrator.pm.base import PMAdapter, PMSyncResult
from loop_orchestrator.pm.cli import main
from loop_orchestrator.pm.jira import JiraAdapter


class StubAdapter(PMAdapter):
    name = "stub"
    calls: list[tuple[str, Path, bool]] = []

    def validate_env(self) -> list[str]:
        return []

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        type(self).calls.append(("pull", tasks_dir, dry_run))
        return PMSyncResult(
            created=["tasks/T0001-fix-login.md"],
            conflicts=["PROJ-9: local status 'open' vs remote 'done'"],
            dry_run=dry_run,
        )

    def push(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        type(self).calls.append(("push", tasks_dir, dry_run))
        return PMSyncResult(updated=["PROJ-9"], dry_run=dry_run)


class MissingEnvAdapter(PMAdapter):
    name = "missing"

    def validate_env(self) -> list[str]:
        return ["STUB_BASE_URL", "STUB_TOKEN"]

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        raise AssertionError("must not be called when unavailable")

    push = pull


class FailingAdapter(PMAdapter):
    name = "failing"

    def validate_env(self) -> list[str]:
        return []

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        return PMSyncResult(errors=["search exploded"], dry_run=dry_run)

    push = pull


REGISTRY = {"stub": StubAdapter, "missing": MissingEnvAdapter, "failing": FailingAdapter}


@pytest.fixture(autouse=True)
def _reset_stub_calls():
    StubAdapter.calls = []


def test_registry_override_seam():
    assert registry.discover(override=[("stub", StubAdapter)]) == {"stub": StubAdapter}
    assert registry.discover(override=[]) == {}


def test_list_adapters_with_override(capsys):
    assert main(["list-adapters"], registry=REGISTRY) == 0
    out = capsys.readouterr().out
    assert "stub  available" in out
    assert "missing  unavailable (missing: STUB_BASE_URL, STUB_TOKEN)" in out


def test_list_adapters_empty_registry(capsys):
    assert main(["list-adapters"], registry={}) == 0
    assert "(no PM adapters installed)" in capsys.readouterr().out


def test_sync_unknown_adapter_exits_64(capsys):
    rc = main(["sync", "--adapter", "nope"], registry=REGISTRY)
    assert rc == 64
    err = capsys.readouterr().err
    assert "unknown adapter 'nope'" in err
    assert "failing, missing, stub" in err  # known list


def test_sync_unavailable_adapter_exits_64_with_missing_vars(capsys):
    rc = main(["sync", "--adapter", "missing"], registry=REGISTRY)
    assert rc == 64
    err = capsys.readouterr().err
    assert "STUB_BASE_URL, STUB_TOKEN" in err


def test_sync_happy_path_both(tmp_path: Path, capsys):
    rc = main(
        ["sync", "--adapter", "stub", "both", "--project-root", str(tmp_path)],
        registry=REGISTRY,
    )
    assert rc == 0  # conflicts are not failures
    captured = capsys.readouterr()
    assert "pull: 1 created, 0 updated, 1 conflict(s), 0 error(s)" in captured.out
    assert "push: 0 created, 1 updated, 0 conflict(s), 0 error(s)" in captured.out
    assert "created tasks/T0001-fix-login.md" in captured.out
    assert "local status 'open'" not in captured.out  # conflict details: stderr only
    assert "conflict (file wins): PROJ-9" in captured.err
    assert StubAdapter.calls == [
        ("pull", tmp_path.resolve() / "tasks", False),
        ("push", tmp_path.resolve() / "tasks", False),
    ]


def test_sync_pull_only_with_dry_run_and_tasks_dir(tmp_path: Path, capsys):
    tasks_dir = tmp_path / "elsewhere"
    rc = main(
        ["sync", "--adapter", "stub", "pull", "--dry-run", "--tasks-dir", str(tasks_dir)],
        registry=REGISTRY,
    )
    assert rc == 0
    assert "[dry-run]" in capsys.readouterr().out
    assert StubAdapter.calls == [("pull", tasks_dir, True)]


def test_sync_errors_exit_1(capsys):
    rc = main(["sync", "--adapter", "failing", "pull"], registry=REGISTRY)
    assert rc == 1
    assert "error: search exploded" in capsys.readouterr().err


# ── sync scrum-flag passthrough ──────────────────────────────────────────────


class ScrumStubAdapter(StubAdapter):
    name = "scrum"
    pushes: list[dict] = []

    def push(self, tasks_dir, dry_run=False, *, project=None, epic=None, sprint=None, board=None):
        type(self).pushes.append(
            {"project": project, "epic": epic, "sprint": sprint, "board": board}
        )
        return PMSyncResult(updated=["PROJ-9"], warnings=["epic link skipped"], dry_run=dry_run)


def test_sync_push_passes_scrum_flags_through(tmp_path: Path, capsys):
    ScrumStubAdapter.pushes = []
    rc = main(
        [
            "sync",
            "--adapter",
            "scrum",
            "push",
            "--project-root",
            str(tmp_path),
            "--project",
            "PROJ",
            "--epic",
            "PROJ-42",
            "--sprint",
            "active",
            "--board",
            "5",
        ],
        registry={"scrum": ScrumStubAdapter},
    )
    assert rc == 0
    assert ScrumStubAdapter.pushes == [
        {"project": "PROJ", "epic": "PROJ-42", "sprint": "active", "board": "5"}
    ]
    assert "warning: epic link skipped" in capsys.readouterr().err  # warnings: stderr, exit 0


def test_sync_scrum_flags_against_plain_adapter_exit_1(tmp_path: Path, capsys):
    rc = main(
        ["sync", "--adapter", "stub", "push", "--epic", "PROJ-42"],
        registry=REGISTRY,
    )
    assert rc == 1
    assert "does not support --epic" in capsys.readouterr().err


def test_sync_without_scrum_flags_keeps_plain_push_signature(tmp_path: Path):
    rc = main(
        ["sync", "--adapter", "stub", "push", "--project-root", str(tmp_path)],
        registry=REGISTRY,
    )
    assert rc == 0
    assert StubAdapter.calls == [("push", tmp_path.resolve() / "tasks", False)]


# ── jira scrum verbs ─────────────────────────────────────────────────────────


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "dev@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    monkeypatch.delenv("JIRA_BOARD_ID", raising=False)


def _adapter(fixtures) -> tuple[JiraAdapter, FakeTransport]:
    transport = FakeTransport(fixtures)
    return JiraAdapter(transport=transport), transport


def test_jira_verbs_missing_creds_exit_64(monkeypatch: pytest.MonkeyPatch, capsys):
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    rc = main(["jira", "sprint-status", "--board", "5"], registry={})
    assert rc == 64
    err = capsys.readouterr().err
    assert "JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN" in err


def test_jira_ensure_epic_found_prints_key(jira_env, capsys):
    adapter, transport = _adapter([("GET", "/rest/api/3/search/jql?", EPIC_SEARCH_RESPONSE)])
    rc = main(
        ["jira", "ensure-epic", "--name", "Sprint Goals", "--project", "PROJ"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    assert capsys.readouterr().out == "PROJ-42\n"
    assert transport.writes() == []  # found, not created


def test_jira_ensure_epic_creates_on_miss(jira_env, capsys):
    adapter, transport = _adapter(
        [
            ("GET", "/rest/api/3/search/jql?", {"issues": []}),
            ("POST", "/rest/api/3/issue", CREATED_RESPONSE),
        ]
    )
    rc = main(
        ["jira", "ensure-epic", "--name", "Sprint Goals", "--project", "PROJ"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    assert capsys.readouterr().out == "PROJ-77\n"
    fields = json.loads(transport.writes()[0][2])["fields"]
    assert fields["issuetype"] == {"name": "Epic"} and fields["summary"] == "Sprint Goals"


def test_jira_ensure_epic_missing_project_exit_64(jira_env, capsys):
    adapter, _ = _adapter([])
    with pytest.raises(SystemExit) as excinfo:
        main(["jira", "ensure-epic", "--name", "Sprint Goals"], registry={}, jira_adapter=adapter)
    assert excinfo.value.code == 64
    assert "JIRA_PROJECT_KEY" in capsys.readouterr().err


def test_jira_sprint_status_active(jira_env, capsys):
    adapter, _ = _adapter([("GET", "/rest/agile/1.0/board/5/sprint", ACTIVE_SPRINT_RESPONSE)])
    rc = main(["jira", "sprint-status", "--board", "5"], registry={}, jira_adapter=adapter)
    assert rc == 0
    assert capsys.readouterr().out == "7 Sprint 12\n"


def test_jira_sprint_status_none_via_env_board(jira_env, monkeypatch, capsys):
    monkeypatch.setenv("JIRA_BOARD_ID", "9")
    adapter, transport = _adapter([("GET", "/rest/agile/1.0/board/9/sprint", {"values": []})])
    rc = main(["jira", "sprint-status"], registry={}, jira_adapter=adapter)
    assert rc == 0
    assert capsys.readouterr().out == "no active sprint\n"
    assert "/board/9/sprint" in transport.calls[0][1]


def test_jira_sprint_status_missing_board_exit_64(jira_env, capsys):
    adapter, _ = _adapter([])
    with pytest.raises(SystemExit) as excinfo:
        main(["jira", "sprint-status"], registry={}, jira_adapter=adapter)
    assert excinfo.value.code == 64
    assert "JIRA_BOARD_ID" in capsys.readouterr().err


def test_jira_move_to_sprint_active_resolution(jira_env, capsys):
    adapter, transport = _adapter(
        [
            ("GET", "/rest/agile/1.0/board/5/sprint", ACTIVE_SPRINT_RESPONSE),
            ("POST", "/rest/agile/1.0/sprint/7/issue", {}),
        ]
    )
    rc = main(
        ["jira", "move-to-sprint", "--active", "--board", "5", "PROJ-1", "PROJ-2"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    assert "moved 2 issue(s) to sprint 7" in capsys.readouterr().out
    method, url, body = transport.writes()[0]
    assert url.endswith("/rest/agile/1.0/sprint/7/issue")
    assert json.loads(body) == {"issues": ["PROJ-1", "PROJ-2"]}


def test_jira_move_to_sprint_explicit_id(jira_env, capsys):
    adapter, transport = _adapter([("POST", "/rest/agile/1.0/sprint/9/issue", {})])
    rc = main(
        ["jira", "move-to-sprint", "--sprint", "9", "PROJ-1"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    assert json.loads(transport.writes()[0][2]) == {"issues": ["PROJ-1"]}


def test_jira_start_sprint_by_id(jira_env, capsys):
    adapter, transport = _adapter(
        [
            ("GET", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint 13", "state": "future"}),
            ("PUT", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint 13", "state": "active"}),
        ]
    )
    rc = main(["jira", "start-sprint", "--sprint", "8"], registry={}, jira_adapter=adapter)
    assert rc == 0
    assert "started sprint 8 (14 days)" in capsys.readouterr().out
    method, url, body = transport.writes()[0]
    assert method == "PUT" and url.endswith("/rest/agile/1.0/sprint/8")
    payload = json.loads(body)
    assert payload["state"] == "active"
    assert "startDate" in payload and "endDate" in payload and "goal" not in payload


def test_jira_start_sprint_next_resolves_earliest_future(jira_env, capsys):
    adapter, transport = _adapter(
        [
            ("GET", "/rest/agile/1.0/board/5/sprint", FUTURE_SPRINTS_RESPONSE),
            ("GET", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint 14"}),
            ("PUT", "/rest/agile/1.0/sprint/8", {}),
        ]
    )
    rc = main(["jira", "start-sprint", "--next", "--board", "5"], registry={}, jira_adapter=adapter)
    assert rc == 0
    assert "started sprint 8" in capsys.readouterr().out
    # fixture lists id 9 first: --next picks the earliest (8), not the first
    assert transport.writes()[0][1].endswith("/rest/agile/1.0/sprint/8")
    assert "state=future" in transport.calls[0][1]


def test_jira_start_sprint_with_goal_and_duration(jira_env, capsys):
    adapter, transport = _adapter(
        [
            ("GET", "/rest/agile/1.0/sprint/8", {"id": 8, "name": "Sprint 14"}),
            ("PUT", "/rest/agile/1.0/sprint/8", {}),
        ]
    )
    rc = main(
        ["jira", "start-sprint", "--sprint", "8", "--duration-days", "7", "--goal", "Ship it"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    assert "started sprint 8 (7 days) — goal: Ship it" in capsys.readouterr().out
    payload = json.loads(transport.writes()[0][2])
    assert payload["goal"] == "Ship it"


def test_jira_start_sprint_create_then_start(jira_env, capsys):
    adapter, transport = _adapter(
        [
            ("POST", "/rest/agile/1.0/sprint", {"id": 21, "name": "Sprint 15", "state": "future"}),
            (
                "GET",
                "/rest/agile/1.0/sprint/21",
                {"id": 21, "name": "Sprint 15", "state": "future"},
            ),
            ("PUT", "/rest/agile/1.0/sprint/21", {}),
        ]
    )
    rc = main(
        ["jira", "start-sprint", "--create", "Sprint 15", "--board", "5"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "created sprint 21 Sprint 15" in out
    assert "started sprint 21" in out
    create, start = transport.writes()
    assert json.loads(create[2]) == {"name": "Sprint 15", "originBoardId": 5}
    assert start[0] == "PUT" and start[1].endswith("/rest/agile/1.0/sprint/21")
    assert json.loads(start[2])["state"] == "active"


def test_jira_start_sprint_next_missing_board_exit_64(jira_env, capsys):
    adapter, transport = _adapter([])
    with pytest.raises(SystemExit) as excinfo:
        main(["jira", "start-sprint", "--next"], registry={}, jira_adapter=adapter)
    assert excinfo.value.code == 64
    assert "JIRA_BOARD_ID" in capsys.readouterr().err
    assert transport.calls == []


def test_jira_start_sprint_missing_creds_exit_64(monkeypatch, capsys):
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    rc = main(["jira", "start-sprint", "--sprint", "8"], registry={})
    assert rc == 64
    assert "JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN" in capsys.readouterr().err


def test_jira_start_sprint_already_active_exit_1(jira_env, capsys):
    def already_active(method, url, headers, body):
        detail = {"errorMessages": ["Sprint 'Sprint 12' is already active on this board."]}
        return 400, json.dumps(detail).encode("utf-8")

    adapter = JiraAdapter(transport=already_active)
    rc = main(["jira", "start-sprint", "--sprint", "8"], registry={}, jira_adapter=adapter)
    assert rc == 1
    err = capsys.readouterr().err
    assert "already active on this board" in err  # Jira's refusal surfaced verbatim


def test_jira_complete_sprint_active_resolves_then_closes(jira_env, capsys):
    adapter, transport = _adapter(
        [
            ("GET", "/rest/agile/1.0/board/5/sprint", ACTIVE_SPRINT_RESPONSE),
            ("GET", "/rest/agile/1.0/sprint/7", {"id": 7, "name": "Sprint 12", "state": "active"}),
            ("PUT", "/rest/agile/1.0/sprint/7", {"id": 7, "name": "Sprint 12", "state": "closed"}),
        ]
    )
    rc = main(
        ["jira", "complete-sprint", "--active", "--board", "5"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "closed sprint 7 Sprint 12" in out
    assert "incomplete issues back to the backlog" in out  # operator is told what Jira did
    method, url, body = transport.writes()[0]
    assert method == "PUT" and url.endswith("/rest/agile/1.0/sprint/7")
    assert json.loads(body) == {"state": "closed"}


def test_jira_complete_sprint_not_active_exit_1_no_close(jira_env, capsys):
    adapter, transport = _adapter(
        [("GET", "/rest/agile/1.0/sprint/9", {"id": 9, "name": "Sprint 14", "state": "closed"})]
    )
    rc = main(["jira", "complete-sprint", "--sprint", "9"], registry={}, jira_adapter=adapter)
    assert rc == 1
    err = capsys.readouterr().err
    assert "sprint 9 (Sprint 14) is not active (state: closed)" in err
    assert transport.writes() == []


def test_jira_complete_sprint_active_missing_board_exit_64(jira_env, capsys):
    adapter, _ = _adapter([])
    with pytest.raises(SystemExit) as excinfo:
        main(["jira", "complete-sprint", "--active"], registry={}, jira_adapter=adapter)
    assert excinfo.value.code == 64
    assert "JIRA_BOARD_ID" in capsys.readouterr().err


def test_jira_complete_sprint_missing_creds_exit_64(monkeypatch, capsys):
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    rc = main(["jira", "complete-sprint", "--sprint", "9"], registry={})
    assert rc == 64
    assert "JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN" in capsys.readouterr().err


def test_jira_retro_comment_default(jira_env, capsys):
    adapter, transport = _adapter([("POST", "/comment", {})])
    rc = main(
        ["jira", "retro", "--epic", "PROJ-42", "--body", "went well\nimprove X"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    assert "retro comment added to PROJ-42" in capsys.readouterr().out
    method, url, body = transport.writes()[0]
    assert url.endswith("/rest/api/3/issue/PROJ-42/comment")
    doc = json.loads(body)["body"]
    assert doc["type"] == "doc"
    paragraphs = [node["content"][0]["text"] for node in doc["content"]]
    assert paragraphs[0].startswith("Retrospective ")  # default title line
    assert paragraphs[1:] == ["went well", "improve X"]


def test_jira_retro_as_issue_from_body_file(jira_env, tmp_path: Path, capsys):
    body_file = tmp_path / "retro.md"
    body_file.write_text("what went well", encoding="utf-8")
    adapter, transport = _adapter([("POST", "/rest/api/3/issue", CREATED_RESPONSE)])
    rc = main(
        [
            "jira",
            "retro",
            "--epic",
            "PROJ-42",
            "--title",
            "Sprint 12 retro",
            "--body-file",
            str(body_file),
            "--as-issue",
        ],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 0
    assert capsys.readouterr().out == "PROJ-77\n"
    fields = json.loads(transport.writes()[0][2])["fields"]
    assert fields["summary"] == "Sprint 12 retro"
    assert fields["labels"] == ["retrospective"]
    assert fields["parent"] == {"key": "PROJ-42"}
    assert fields["project"] == {"key": "PROJ"}  # derived from the epic key
    assert fields["issuetype"] == {"name": "Task"}


def test_jira_api_error_exit_1_with_messages(jira_env, capsys):
    adapter, _ = _adapter([])  # every call 404s with errorMessages
    rc = main(
        ["jira", "ensure-epic", "--name", "X", "--project", "PROJ"],
        registry={},
        jira_adapter=adapter,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no fixture for this call" in err  # response body's error messages surfaced
