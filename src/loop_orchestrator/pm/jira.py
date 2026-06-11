"""Jira reference adapter — REST v3 over urllib.request, no extra deps.

Env (basic auth, environment only — never the repo): JIRA_BASE_URL,
JIRA_EMAIL, JIRA_API_TOKEN. The transport is injectable so tests run against
recorded fixtures instead of the network.

pull: assigned not-Done issues -> new task files; an existing file with a
matching `jira:` key is NEVER modified — any status divergence is a file-wins
conflict (logged to ops-wiki/log.md). push: task-file status drives issue
transitions (open->'To Do', in-progress->'In Progress', done->'Done';
'dropped' has no remote mapping and is logged as a conflict). dry_run plans
everything, performs no HTTP writes and no file/log writes (GETs allowed).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from . import taskfiles
from .base import PMAdapter, PMSyncResult

ENV_VARS = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")

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


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


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
            detail = raw.decode("utf-8", "replace")[:200]
            raise JiraError(f"{method} {path} -> HTTP {status}: {detail}")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JiraError(f"{method} {path}: response is not JSON: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {}

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

    def push(self, tasks_dir: Path, dry_run: bool = False) -> PMSyncResult:
        result = PMSyncResult(dry_run=dry_run)
        log_md = taskfiles.log_path(self.project_root)
        for path in taskfiles.list_tasks(tasks_dir):
            try:
                frontmatter = taskfiles.parse_frontmatter(path)
            except ValueError as exc:
                result.errors.append(str(exc))
                continue
            key = frontmatter.get("jira")
            if not key:
                continue
            status = frontmatter.get("status")
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
        return result

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
