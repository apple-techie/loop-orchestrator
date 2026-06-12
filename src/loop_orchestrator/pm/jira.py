"""Jira reference adapter — REST v3 + Agile 1.0 over urllib.request, no
extra deps.

Env (basic auth, environment only — never the repo): JIRA_BASE_URL,
JIRA_EMAIL, JIRA_API_TOKEN. Optional scrum context: JIRA_PROJECT_KEY (default
project for issue creation/epic search) and JIRA_BOARD_ID (sprint lookups) —
absence only affects the verbs that need them. The transport is injectable so
tests run against recorded fixtures instead of the network.

pull: assigned not-Done issues -> new task files; an existing file with a
matching `jira:` key is NEVER modified — any status divergence is a file-wins
conflict (logged to ops-wiki/log.md). push: task-file status drives issue
transitions (open->'To Do', in-progress->'In Progress', done->'Done';
'dropped' has no remote mapping and is logged as a conflict); open or
in-progress tasks WITHOUT a jira: key are created remotely and the new key is
written back into the task frontmatter (the one sanctioned file write).
dry_run plans everything, performs no HTTP writes and no file/log writes
(GETs allowed).

Epic linking: new issues link to an epic via the team-managed 'parent' field.
Company-managed projects reject that with a 400 — the issue is then created
WITHOUT the link and a warning is surfaced (the epic-link customfield id
varies per site; we never guess field ids).
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import taskfiles
from .base import PMAdapter, PMSyncResult

ENV_VARS = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
ENV_PROJECT = "JIRA_PROJECT_KEY"
ENV_BOARD = "JIRA_BOARD_ID"
# Escape hatch for localhost dev only: when set to "1", an http:// base URL is
# permitted so the Basic-auth token can be sent to a non-TLS dev instance.
ENV_ALLOW_INSECURE = "JIRA_ALLOW_INSECURE_BASE_URL"

JQL = "assignee = currentUser() AND statusCategory != Done"
SEARCH_FIELDS = "summary,description,status"

# Jira statusCategory key -> task-file status.
_CATEGORY_TO_STATUS = {"new": "open", "indeterminate": "in-progress", "done": "done"}
# task-file status -> target transition name (matched case-insensitively).
_STATUS_TO_TRANSITION = {"open": "To Do", "in-progress": "In Progress", "done": "Done"}

# (method, url, headers, body) -> (http status, response body)
Transport = Callable[[str, str, dict[str, str], "bytes | None"], "tuple[int, bytes]"]


class JiraError(RuntimeError):
    """An HTTP call failed or returned an unusable payload."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _require_safe_base_url(base: str) -> None:
    """Guard the Basic-auth token: the base URL must be https:// and carry no
    embedded credentials, so the token never reaches a non-TLS or poisoned
    host. JIRA_ALLOW_INSECURE_BASE_URL=1 (localhost dev only) waives the https
    requirement; embedded userinfo is rejected unconditionally."""
    parsed = urllib.parse.urlsplit(base)
    if "@" in parsed.netloc:
        raise JiraError(
            f"{ENV_VARS[0]} {base!r} embeds credentials in the authority (userinfo '@') "
            "— remove them; the API token is the only credential"
        )
    if parsed.scheme != "https" and os.environ.get(ENV_ALLOW_INSECURE) != "1":
        raise JiraError(
            f"{ENV_VARS[0]} {base!r} is not https:// — refusing to send the API token over "
            f"an unencrypted connection (set {ENV_ALLOW_INSECURE}=1 for localhost dev only)"
        )


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _adf_doc(text: str) -> dict:
    """Minimal Atlassian Document Format doc: one paragraph per input line."""
    paragraphs = []
    for line in text.splitlines() or [""]:
        content = [{"type": "text", "text": line}] if line else []
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _jql_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _utc_iso(moment: datetime) -> str:
    """Jira's date-time format: UTC, millisecond precision, 'Z' suffix."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _error_detail(raw: bytes) -> str:
    """Human-readable error from a Jira error body (errorMessages/errors when
    the body is JSON, the raw text otherwise)."""
    text = raw.decode("utf-8", "replace")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return text[:200]
    if isinstance(doc, dict):
        messages = [str(item) for item in doc.get("errorMessages") or []]
        errors = doc.get("errors")
        if isinstance(errors, dict):
            messages += [f"{field}: {detail}" for field, detail in errors.items()]
        if messages:
            return "; ".join(messages)[:300]
    return text[:200]


def _objective_text(body: str) -> str | None:
    """Text of the task body's '## Objective' section, or None."""
    match = re.search(r"^## Objective\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    if match is None:
        return None
    return match.group(1).strip() or None


def _adf_text(node: object) -> str:
    """Plain text of an Atlassian Document Format tree (or a bare string)."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(item) for item in node)
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            return node["text"]
        text = _adf_text(node.get("content") or [])
        return text + "\n" if node.get("type") == "paragraph" else text
    return ""


def _remote_status(fields: dict) -> str | None:
    """Mapped task-file status for an issue's statusCategory, or None."""
    status = fields.get("status") or {}
    category = status.get("statusCategory") or {}
    mapped = _CATEGORY_TO_STATUS.get(category.get("key"))
    if mapped:
        return mapped
    name = (category.get("name") or "").strip().lower()
    return {"to do": "open", "in progress": "in-progress", "done": "done"}.get(name)


def _new_task_body(task_id: str, issue_key: str, summary: str, context: str) -> str:
    stub = f"(stub — fill in before dispatching; imported from Jira {issue_key})"
    return (
        f"\n# {task_id} — {summary}\n\n"
        f"## Objective\n{summary}\n\n"
        f"## Context you need\n{context}\n\n"
        f"## Deliverables\n{stub}\n\n"
        f"## Acceptance criteria\n{stub}\n\n"
        f"## Verification\n{stub}\n\n"
        f"## Out of scope\n{stub}\n"
    )


class JiraAdapter(PMAdapter):
    name = "jira"

    def __init__(self, project_root: str | Path = ".", transport: Transport | None = None):
        super().__init__(project_root)
        self.transport = transport or _urllib_transport

    def validate_env(self) -> list[str]:
        return [var for var in ENV_VARS if not os.environ.get(var)]

    # ── HTTP ──────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        _require_safe_base_url(base)  # before the token is built/sent (MEDIUM-4)
        credentials = f"{os.environ.get('JIRA_EMAIL', '')}:{os.environ.get('JIRA_API_TOKEN', '')}"
        headers = {
            "Authorization": "Basic " + base64.b64encode(credentials.encode()).decode("ascii"),
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self.transport(method, base + path, headers, data)
        except OSError as exc:  # urllib.error.URLError subclasses OSError
            raise JiraError(f"{method} {path}: {exc}") from exc
        if status >= 400:
            raise JiraError(f"{method} {path} -> HTTP {status}: {_error_detail(raw)}", status)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JiraError(f"{method} {path}: response is not JSON: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {}

    # ── scrum: epics / sprints / comments ─────────────────────────────────

    def find_epic(self, name: str, project: str) -> str | None:
        """Key of the epic in `project` whose summary EXACTLY equals `name`
        (JQL `~` is fuzzy, so the summary is re-checked client-side)."""
        jql = f"project = {project} AND issuetype = Epic AND summary ~ {_jql_quote(name)}"
        query = urllib.parse.urlencode({"jql": jql, "fields": "summary", "maxResults": 50})
        doc = self._request("GET", f"/rest/api/3/search/jql?{query}")
        for issue in doc.get("issues") or []:
            fields = issue.get("fields") or {}
            if (fields.get("summary") or "").strip() == name.strip() and issue.get("key"):
                return issue["key"]
        return None

    def create_epic(self, name: str, project: str, description: str | None = None) -> str:
        fields: dict = {
            "project": {"key": project},
            "issuetype": {"name": "Epic"},
            "summary": name,
        }
        if description:
            fields["description"] = _adf_doc(description)
        doc = self._request("POST", "/rest/api/3/issue", body={"fields": fields})
        key = doc.get("key")
        if not key:
            raise JiraError("POST /rest/api/3/issue: response carries no issue key")
        return key

    def create_issue(
        self,
        summary: str,
        description_text: str,
        project: str,
        epic_key: str | None = None,
        issue_type: str = "Task",
        labels: list[str] | None = None,
    ) -> tuple[str, str | None]:
        """Create an issue; returns (key, warning|None). The epic link uses
        the team-managed 'parent' field; when the site rejects it (HTTP 400 —
        company-managed projects need a per-site epic-link customfield we
        refuse to guess) the issue is created WITHOUT the link and the
        limitation is returned as the warning."""
        fields: dict = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
        }
        if description_text:
            fields["description"] = _adf_doc(description_text)
        if labels:
            fields["labels"] = list(labels)
        if epic_key:
            fields["parent"] = {"key": epic_key}
        try:
            doc = self._request("POST", "/rest/api/3/issue", body={"fields": fields})
        except JiraError as exc:
            if not (epic_key and exc.status == 400 and "parent" in str(exc).lower()):
                raise
            del fields["parent"]
            doc = self._request("POST", "/rest/api/3/issue", body={"fields": fields})
            key = doc.get("key")
            if not key:
                raise JiraError("POST /rest/api/3/issue: response carries no issue key") from exc
            warning = (
                f"{key}: created without epic link to {epic_key} — this project rejects the "
                "'parent' field (company-managed sites use a site-specific epic-link "
                "customfield; link the issue manually or from the board)"
            )
            return key, warning
        key = doc.get("key")
        if not key:
            raise JiraError("POST /rest/api/3/issue: response carries no issue key")
        return key, None

    def active_sprint(self, board_id: str | int) -> dict | None:
        """{'id': ..., 'name': ...} of the board's active sprint, or None."""
        doc = self._request("GET", f"/rest/agile/1.0/board/{board_id}/sprint?state=active")
        values = doc.get("values") or []
        if not values:
            return None
        sprint = values[0]
        return {"id": sprint.get("id"), "name": sprint.get("name")}

    def move_to_sprint(self, sprint_id: str | int, keys: list[str]) -> None:
        self._request("POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", body={"issues": keys})

    def future_sprints(self, board_id: str | int) -> list[dict]:
        """[{'id': ..., 'name': ...}, ...] for the board's future sprints."""
        doc = self._request("GET", f"/rest/agile/1.0/board/{board_id}/sprint?state=future")
        return [
            {"id": sprint.get("id"), "name": sprint.get("name")}
            for sprint in doc.get("values") or []
        ]

    def create_sprint(self, board_id: str | int, name: str) -> dict:
        """Create a future sprint on the board; returns {'id': ..., 'name': ...}."""
        try:
            origin = int(board_id)
        except (TypeError, ValueError) as exc:
            raise JiraError(f"create-sprint: board id {board_id!r} is not numeric") from exc
        doc = self._request(
            "POST", "/rest/agile/1.0/sprint", body={"name": name, "originBoardId": origin}
        )
        return {"id": doc.get("id"), "name": doc.get("name")}

    def start_sprint(
        self, sprint_id: str | int, duration_days: int = 14, goal: str | None = None
    ) -> dict:
        """Activate a sprint: state=active, startDate=now (UTC), endDate=now+
        duration. Jira's own refusals (e.g. another sprint already active on
        the board) surface as JiraError with the response's error messages."""
        # Jira's sprint-update endpoint requires `name` even when only changing
        # state, so fetch the current name and preserve it in the PUT (a bare
        # state/date PUT 400s with "name is required").
        sprint = self._request("GET", f"/rest/agile/1.0/sprint/{sprint_id}")
        now = datetime.now(timezone.utc)
        body: dict = {
            "name": sprint.get("name"),
            "state": "active",
            "startDate": _utc_iso(now),
            "endDate": _utc_iso(now + timedelta(days=duration_days)),
        }
        if goal:
            body["goal"] = goal
        return self._request("PUT", f"/rest/agile/1.0/sprint/{sprint_id}", body=body)

    def complete_sprint(self, sprint_id: str | int) -> dict:
        """Close an ACTIVE sprint (the state is checked first — closing is
        irreversible and Jira moves incomplete issues to the backlog)."""
        sprint = self._request("GET", f"/rest/agile/1.0/sprint/{sprint_id}")
        state = sprint.get("state")
        if state != "active":
            name = sprint.get("name") or "unnamed"
            raise JiraError(
                f"sprint {sprint_id} ({name}) is not active (state: {state or 'unknown'}) "
                "— only an active sprint can be completed"
            )
        # Jira requires `name` on any sprint-update PUT, including the close —
        # preserve the name we already fetched (a bare {state} PUT 400s).
        return self._request(
            "PUT",
            f"/rest/agile/1.0/sprint/{sprint_id}",
            body={"name": sprint.get("name"), "state": "closed"},
        )

    def add_comment(self, issue_key: str, text: str) -> None:
        self._request(
            "POST", f"/rest/api/3/issue/{issue_key}/comment", body={"body": _adf_doc(text)}
        )

    # ── pull ──────────────────────────────────────────────────────────────

    def pull(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        result = PMSyncResult(dry_run=dry_run)
        log_md = taskfiles.log_path(self.project_root)
        query = urllib.parse.urlencode({"jql": JQL, "fields": SEARCH_FIELDS, "maxResults": 100})
        try:
            doc = self._request("GET", f"/rest/api/3/search/jql?{query}")
        except JiraError as exc:
            result.errors.append(str(exc))
            return result

        next_num = int(taskfiles.next_task_id(tasks_dir)[1:])
        for issue in doc.get("issues") or []:
            key = issue.get("key")
            if not key:
                continue
            fields = issue.get("fields") or {}
            summary = (fields.get("summary") or "").strip() or key
            existing = taskfiles.find_by_jira(tasks_dir, key)
            if existing is not None:
                self._pull_existing(existing, key, fields, result, log_md, dry_run)
                continue
            task_id = f"T{next_num:04d}"
            next_num += 1
            path = Path(tasks_dir) / f"{task_id}-{taskfiles.slugify(summary)}.md"
            result.created.append(str(path))
            if dry_run:
                continue
            context = _adf_text(fields.get("description")).strip() or "(from Jira)"
            frontmatter = {
                "id": task_id,
                "title": summary,
                "status": "open",
                "depends_on": [],
                "jira": key,
                "scope": f"imported from Jira {key}; refine before dispatching",
            }
            taskfiles.write_task(path, frontmatter, _new_task_body(task_id, key, summary, context))
            taskfiles.record_sync(log_md, key)
        return result

    def _pull_existing(
        self,
        path: Path,
        key: str,
        fields: dict,
        result: PMSyncResult,
        log_md: Path,
        dry_run: bool,
    ) -> None:
        """File wins on ANY divergence: the file is never modified; a status
        mismatch is recorded as a conflict and logged."""
        try:
            frontmatter = taskfiles.parse_frontmatter(path)
        except ValueError as exc:
            result.errors.append(f"{key}: {exc}")
            return
        remote = _remote_status(fields)
        if remote is None:
            result.errors.append(f"{key}: unrecognized remote status category")
            return
        local = frontmatter.get("status")
        if local == remote:
            return
        detail = f"local status '{local}' vs remote '{remote}'"
        result.conflicts.append(f"{key}: {detail}")
        if not dry_run:
            taskfiles.record_conflict(log_md, key, detail)

    # ── push ──────────────────────────────────────────────────────────────

    def push(
        self,
        tasks_dir: Path,
        dry_run: bool = False,
        *,
        project: str | None = None,
        epic: str | None = None,
        sprint: str | None = None,
        board: str | None = None,
    ) -> PMSyncResult:
        result = PMSyncResult(dry_run=dry_run)
        log_md = taskfiles.log_path(self.project_root)
        project = project or os.environ.get(ENV_PROJECT)
        skipped_creations = 0
        created_keys: list[str] = []
        for path in taskfiles.list_tasks(tasks_dir):
            try:
                frontmatter = taskfiles.parse_frontmatter(path)
            except ValueError as exc:
                result.errors.append(str(exc))
                continue
            key = frontmatter.get("jira")
            status = frontmatter.get("status")
            if not key:
                if status not in ("open", "in-progress"):
                    continue
                if not project:
                    skipped_creations += 1
                    continue
                try:
                    created = self._create_from_task(
                        path, frontmatter, project, epic, result, log_md, dry_run
                    )
                except JiraError as exc:
                    result.errors.append(f"{path.name}: {exc}")
                    continue
                if created:
                    created_keys.append(created)
                continue
            if status == "dropped":
                detail = "status 'dropped' has no remote mapping"
                result.conflicts.append(f"{key}: {detail}")
                if not dry_run:
                    taskfiles.record_conflict(log_md, key, detail)
                continue
            target = _STATUS_TO_TRANSITION.get(status)
            if target is None:
                result.errors.append(f"{key}: unknown task status {status!r} in {path.name}")
                continue
            try:
                self._push_one(key, status, target, result, log_md, dry_run)
            except JiraError as exc:
                result.errors.append(f"{key}: {exc}")
        if skipped_creations:
            result.warnings.append(
                f"skipped creating {skipped_creations} task(s) without a jira key: "
                f"{ENV_PROJECT} is not set (set it or pass --project)"
            )
        if sprint and created_keys:
            self._move_created(sprint, board, created_keys, result)
        return result

    def _create_from_task(
        self,
        path: Path,
        frontmatter: dict,
        project: str,
        epic: str | None,
        result: PMSyncResult,
        log_md: Path,
        dry_run: bool,
    ) -> str | None:
        """Create a remote issue for a local task with no jira: key; write the
        new key back into the frontmatter (the one sanctioned file write)."""
        task_id = str(frontmatter.get("id") or path.stem)
        summary = str(frontmatter.get("title") or task_id)
        if dry_run:
            result.created.append(f"would-create {task_id}: {summary} (project {project})")
            return None
        try:
            body = taskfiles.split_task(path)[1]
        except ValueError:
            body = ""
        description = _objective_text(body) or summary
        key, warning = self.create_issue(summary, description, project, epic_key=epic)
        if warning:
            result.warnings.append(warning)
        result.created.append(f"{key} from {task_id}")
        taskfiles.update_frontmatter(path, jira=key)
        taskfiles.record_created(log_md, key, task_id)
        return key

    def _move_created(
        self, sprint: str, board: str | None, keys: list[str], result: PMSyncResult
    ) -> None:
        sprint_id: str | int = sprint
        if sprint == "active":
            board = board or os.environ.get(ENV_BOARD)
            if not board:
                result.errors.append(
                    f"--sprint active needs a board: {ENV_BOARD} is not set (or pass --board)"
                )
                return
            try:
                active = self.active_sprint(board)
            except JiraError as exc:
                result.errors.append(str(exc))
                return
            if active is None:
                result.warnings.append(
                    f"board {board} has no active sprint — created issue(s) not moved"
                )
                return
            sprint_id = active["id"]
        try:
            self.move_to_sprint(sprint_id, keys)
        except JiraError as exc:
            result.errors.append(str(exc))
            return
        result.updated.extend(f"{key} -> sprint {sprint_id}" for key in keys)

    def _push_one(
        self,
        key: str,
        status: str,
        target: str,
        result: PMSyncResult,
        log_md: Path,
        dry_run: bool,
    ) -> None:
        issue = self._request("GET", f"/rest/api/3/issue/{key}?fields=status")
        if _remote_status(issue.get("fields") or {}) == status:
            return
        doc = self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        transition = next(
            (
                t
                for t in doc.get("transitions") or []
                if (t.get("name") or "").strip().lower() == target.lower()
            ),
            None,
        )
        if transition is None:
            result.errors.append(f"{key}: no transition named '{target}'")
            return
        result.updated.append(key)
        if dry_run:
            return
        self._request(
            "POST",
            f"/rest/api/3/issue/{key}/transitions",
            body={"transition": {"id": str(transition.get("id"))}},
        )
        taskfiles.record_sync(log_md, key)
