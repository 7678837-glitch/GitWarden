"""Logs page route and read-only view model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse

from gitwarden import logs
from gitwarden.web import CsrfToken

PAGE_SIZE = 20

_RUN_STATUS_KEYS = {
    "ok": "logs.status.ok",
    "partial": "logs.status.partial",
    "failed": "logs.status.failed",
    "busy": "logs.status.busy",
    "gate_blocked": "logs.status.gate_blocked",
    "conflict": "logs.status.conflict",
    "history_conflict": "logs.status.history_conflict",
    "audit_alert": "logs.status.audit_alert",
    "error": "logs.status.error",
}

_ACTIVITY_KEYS = {
    "login": "logs.activity.login",
    "logout": "logs.activity.logout",
    "task.create": "logs.activity.task_create",
    "task.bulk": "logs.activity.task_bulk",
    "task.run": "logs.activity.task_run",
    "task.update": "logs.activity.task_update",
    "task.toggle": "logs.activity.task_toggle",
    "task.detailed": "logs.activity.task_detailed",
    "target.retry": "logs.activity.target_retry",
    "discovery.failed": "logs.activity.discovery_failed",
    "target.audit": "logs.activity.target_audit",
    "target.update": "logs.activity.target_update",
    "target.delete": "logs.activity.target_delete",
    "credential.create": "logs.activity.credential_create",
    "credential.delete": "logs.activity.credential_delete",
    "access.create": "logs.activity.access_create",
    "access.save": "logs.activity.access_save",
    "access.preview": "logs.activity.access_preview",
    "access.discover": "logs.activity.access_discover",
    "access.remove": "logs.activity.access_remove",
    "instance.create": "logs.activity.instance_create",
    "instance.update": "logs.activity.instance_update",
}


def _number(value: str | int | None, default: int | None = None) -> int | None:
    if value is None or not str(value).strip():
        return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _positive(value: str | int | None, default: int = 1) -> int:
    parsed = _number(value, default)
    return max(1, parsed or default)


def _url(request: Request, **updates: object) -> str:
    values = dict(request.query_params)
    for key, value in updates.items():
        if value is None or value == "":
            values.pop(key, None)
        else:
            values[key] = str(value)
    query = urlencode(values)
    return f"{request.url.path}?{query}" if query else request.url.path


def _duration(started_at: str | None, finished_at: str | None) -> str:
    if not started_at or not finished_at:
        return "—"
    try:
        seconds = max(
            0.0,
            (
                datetime.fromisoformat(finished_at).astimezone(UTC)
                - datetime.fromisoformat(started_at).astimezone(UTC)
            ).total_seconds(),
        )
    except (TypeError, ValueError):
        return "—"
    return f"{seconds:.1f}"


def _activity_key(action: str | None) -> str | None:
    return _ACTIVITY_KEYS.get(action or "")


def _safe(value: Any, secrets: list[str], redact) -> Any:
    if value is None:
        return None
    return redact(str(value), secrets)


def _run_view(store, row: dict[str, Any], secrets: list[str], redact) -> dict[str, Any]:
    run = dict(row)
    run["task_name"] = _safe(run.get("task_name"), secrets, redact) or "—"
    run["message"] = _safe(run.get("message"), secrets, redact)
    run["status_key"] = _RUN_STATUS_KEYS.get(
        run.get("status"),
        "logs.status.running" if run.get("status") is None else "logs.status.unknown",
    )
    run["duration"] = _duration(run.get("started_at"), run.get("finished_at"))
    targets = {item["id"]: item["name"] for item in store.list_targets(run["task_id"])}
    run["steps"] = []
    for step in logs.list_steps(store, int(run["id"])):
        item = dict(step)
        item["target_name"] = _safe(targets.get(item["target_id"]), secrets, redact)
        item["step"] = _safe(item.get("step"), secrets, redact)
        item["message"] = _safe(item.get("message"), secrets, redact)
        item["raw"] = _safe(item.get("raw"), secrets, redact)
        if item.get("duration_ms") is not None:
            item["duration"] = f"{int(item['duration_ms']) / 1000:.2f}"
        else:
            item["duration"] = None
        run["steps"].append(item)
    run["refs"] = []
    for ref in logs.list_refs(store, int(run["id"])):
        item = dict(ref)
        for key in ("ref", "kind", "old", "new"):
            item[key] = _safe(item.get(key), secrets, redact)
        run["refs"].append(item)
    return run


def _activity_view(row: dict[str, Any], secrets: list[str], redact) -> dict[str, Any]:
    activity = dict(row)
    for key in ("action", "object", "detail", "ip"):
        activity[key] = _safe(activity.get(key), secrets, redact)
    activity["action_key"] = _activity_key(row.get("action"))
    return activity


def logs_page(
    request: Request,
    csrf: str,
    *,
    tab: str = "runs",
    task_id: str | None = None,
    task: str | None = None,
    status: str | None = None,
    days: str | None = None,
    search: str = "",
    category: str | None = None,
    page: str = "1",
    run_id: str | None = None,
):
    """Render the logs page using only the logs query/read helpers."""
    from gitwarden.web import _page, _redact, _store

    store = _store(request)
    active_tab = "activity" if tab == "activity" else "runs"
    page_number = _positive(page)
    days_number = _number(days)
    if days_number is not None and days_number < 1:
        days_number = None
    selected_task = _number(task_id if task_id is not None else task)
    selected_status = (status or "").strip()
    selected_category = (category or "").strip()
    selected_search = search or ""
    open_run_id = _number(run_id)
    secrets = store.secret_values()

    tasks = store.list_tasks()
    for item in tasks:
        item["name"] = _safe(item.get("name"), secrets, _redact)

    context: dict[str, Any] = {
        "tab": active_tab,
        "tasks": tasks,
        "filters": {
            "task_id": selected_task,
            "status": selected_status,
            "days": days_number,
            "search": selected_search,
            "category": selected_category,
        },
        "open_run_id": open_run_id,
        "page": page_number,
        "page_size": PAGE_SIZE,
        "retention_days": getattr(request.app.state.scheduler, "retention_days", 90),
    }

    if active_tab == "runs":
        rows, total = logs.query_runs(
            store,
            task_id=selected_task,
            status=selected_status or None,
            days=days_number,
            search=selected_search,
            page=page_number,
            page_size=PAGE_SIZE,
        )
        context["runs"] = [_run_view(store, row, secrets, _redact) for row in rows]
        context["total"] = total
        context["kind_key"] = "logs.runs"
        context["previous_url"] = (
            _url(request, tab="runs", page=page_number - 1) if page_number > 1 else None
        )
        context["next_url"] = (
            _url(request, tab="runs", page=page_number + 1)
            if page_number * PAGE_SIZE < total
            else None
        )
    else:
        rows, total = logs.query_activity(
            store,
            category=selected_category or None,
            days=days_number,
            page=page_number,
            page_size=PAGE_SIZE,
        )
        context["activity"] = [_activity_view(row, secrets, _redact) for row in rows]
        context["total"] = total
        context["kind_key"] = "logs.activity"
        context["previous_url"] = (
            _url(request, tab="activity", page=page_number - 1) if page_number > 1 else None
        )
        context["next_url"] = (
            _url(request, tab="activity", page=page_number + 1)
            if page_number * PAGE_SIZE < total
            else None
        )

    start = (page_number - 1) * PAGE_SIZE + 1 if context["total"] else 0
    end = min(page_number * PAGE_SIZE, context["total"])
    context["range_start"] = start
    context["range_end"] = end
    context["runs_url"] = _url(request, tab="runs", page=1)
    context["activity_url"] = _url(request, tab="activity", page=1)
    return _page(request, "logs.html", csrf, **context)


def register(app):
    """Register the server-rendered logs page on the existing FastAPI app."""

    @app.get("/logs", response_class=HTMLResponse)
    def logs_route(
        request: Request,
        csrf: CsrfToken,
        tab: str = "runs",
        task_id: str | None = None,
        task: str | None = None,
        status: str | None = None,
        days: str | None = None,
        search: str = "",
        category: str | None = None,
        page: str = "1",
        run_id: str | None = None,
    ):
        return logs_page(
            request,
            csrf,
            tab=tab,
            task_id=task_id,
            task=task,
            status=status,
            days=days,
            search=search,
            category=category,
            page=page,
            run_id=run_id,
        )

    return app
