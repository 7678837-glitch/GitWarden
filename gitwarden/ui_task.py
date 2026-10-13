"""任务详情和单仓库三步向导。"""

from __future__ import annotations

import math
import shlex
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from gitwarden.archive import ArchiveBusy
from gitwarden.creds import check_target_urls, check_url
from gitwarden.git import GitError
from gitwarden.git import run as git_run
from gitwarden.i18n import t
from gitwarden.provision import provision_target
from gitwarden.runner import archive_path, bind_target
from gitwarden.target import TargetNotEmpty
from gitwarden.web import (
    CsrfToken,
    VerifiedToken,
    _access_id,
    _blank_task,
    _cred_id,
    _cred_summaries,
    _db_error,
    _form_bool,
    _page,
    _platform_summaries,
    _positive_int,
    _redact,
    _redirect,
    _require_task,
    _store,
    _task_values,
    _task_values_from_row,
    _text,
)

_STEPS = ("source", "targets", "review")
_MODES = ("manual", "platform")


def _step(value: object) -> str:
    raw = str(value or "source").strip().lower()
    return {"1": "source", "2": "targets", "3": "review"}.get(raw, raw)


def _mode(value: object) -> str:
    raw = str(value or "manual").strip().lower()
    return raw if raw in _MODES else "manual"


def _safe_url(raw: object, secrets: list[str]) -> str:
    """Keep a useful value for error recovery without echoing URL credentials."""
    value = str(raw or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.password is not None or (
        parts.scheme.lower() in {"http", "https"} and parts.username is not None
    ):
        return ""
    return _redact(value, secrets)


def _form_values(form: Any, store) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    secrets = store.secret_values()
    task_values = _task_values(
        str(form.get("name", "")),
        _safe_url(form.get("source_url", ""), secrets),
        str(form.get("source_cred_id", "")),
        str(form.get("interval_minutes", "60")),
        _form_bool(form.get("enabled"), True),
    )
    target_values = {
        "name": str(form.get("target_name", "")),
        "main_url": _safe_url(form.get("main_url", ""), secrets),
        "history_url": _safe_url(form.get("history_url", ""), secrets),
        "cred_id": str(form.get("target_cred_id", form.get("cred_id", ""))),
    }
    provision_values = {
        "access_id": str(form.get("access_id", form.get("provision_access_id", ""))),
        "owner": str(form.get("owner", form.get("provision_owner", ""))),
        "name": str(form.get("target_name", form.get("provision_name", ""))),
    }
    return task_values, target_values, provision_values


def _platform_choices(store) -> list[dict[str, Any]]:
    choices = []
    for row in _platform_summaries(store):
        choices.append(
            {
                "id": row["id"],
                "instance_id": row["instance_id"],
                "instance_name": row.get("name"),
                "access_name": row.get("name"),
                "kind": row.get("kind", ""),
                "label": f"{row.get('name', '')} · {row.get('kind', '')}",
            }
        )
    # _platform_summaries merges instance and access rows, so recover the access
    # label from the authoritative access row when both names happen to match.
    for choice in choices:
        access = store.get_access(choice["id"])
        instance = store.get_instance(choice["instance_id"])
        if access:
            choice["access_name"] = access["name"]
        if instance:
            choice["instance_name"] = instance["name"]
            choice["kind"] = instance["kind"]
            choice["label"] = f"{instance['name']} · {access['name'] if access else ''}"
    return choices


def _wizard_page(
    request: Request,
    csrf: str,
    *,
    step: str = "source",
    mode: str = "manual",
    error: str | None = None,
    task_values: dict[str, Any] | None = None,
    target_values: dict[str, Any] | None = None,
    provision_values: dict[str, Any] | None = None,
    confirm_values: dict[str, bool] | None = None,
    status: int = 200,
):
    store = _store(request)
    values = task_values or _blank_task()
    targets = target_values or {"name": "", "main_url": "", "history_url": "", "cred_id": ""}
    provision = provision_values or {"access_id": "", "owner": "", "name": ""}
    return _page(
        request,
        "task_form.html",
        csrf,
        status=status,
        error=error,
        step=step if step in _STEPS else "source",
        mode=mode if mode in _MODES else "manual",
        values=values,
        target_values=targets,
        provision_values=provision,
        confirm_values=confirm_values or {"writer": False, "private": False, "no_ci": False},
        creds=_cred_summaries(store),
        platforms=_platform_choices(store),
        min_sync_minutes=store.min_sync_minutes,
    )


def _validate_source(request: Request, values: dict[str, Any]) -> dict[str, Any]:
    parsed = {
        "name": _text(str(values.get("name", "")), "任务名称"),
        "source_url": str(values.get("source_url", "")).strip(),
        "source_cred_id": _cred_id(str(values.get("source_cred_id", ""))),
        "interval_minutes": _positive_int(str(values.get("interval_minutes", "")), "同步间隔"),
        "enabled": bool(values.get("enabled")),
    }
    parsed["source_url"] = _text(parsed["source_url"], "来源地址")
    check_url(parsed["source_url"])
    minimum = int(getattr(_store(request), "min_sync_minutes", 1))
    if parsed["interval_minutes"] < minimum:
        raise ValueError(t("error.minimum_interval", minutes=minimum))
    return parsed


def _validate_target(
    request: Request, mode: str, targets: dict[str, Any], provision: dict[str, Any]
):
    store = _store(request)
    if mode == "platform":
        access_id = _access_id(str(provision.get("access_id", "")))
        if store.get_access(access_id) is None:
            raise ValueError(t("error.access_missing"))
        owner = _text(str(provision.get("owner", "")), "所有者")
        name = _text(str(provision.get("name", "")), "目标名称")
        return {"access_id": access_id, "owner": owner, "name": name}
    name = str(targets.get("name", "")).strip() or "manual"
    main_url = str(targets.get("main_url", "")).strip()
    history_url = str(targets.get("history_url", "")).strip()
    if not main_url:
        raise ValueError(t("error.required", field=t("wizard.mirror_url")))
    if not history_url:
        raise ValueError(t("error.required", field=t("wizard.history_url")))
    check_target_urls(main_url, history_url)
    cred_id = _cred_id(str(targets.get("cred_id", "")))
    if cred_id is not None and store.get_cred(cred_id) is None:
        raise ValueError(t("error.credential_invalid"))
    return {"name": name, "main_url": main_url, "history_url": history_url, "cred_id": cred_id}


def _prerequisites_ok(form: Any) -> bool:
    aliases = (
        ("confirm_writer", "prereq_writer", "prerequisite_writer"),
        ("confirm_private", "prereq_private", "prerequisite_private"),
        ("confirm_no_ci", "prereq_no_ci", "prerequisite_no_ci"),
    )
    return all(any(_form_bool(form.get(name)) for name in names) for names in aliases)


def _form_error(request: Request, exc: Exception) -> str:
    store = _store(request)
    if isinstance(exc, TargetNotEmpty):
        return _redact(t("error.target_not_empty", url=str(exc.args[0])), store.secret_values())
    return _redact(_db_error(exc), store.secret_values())


def _ref_count(archive: Path, prefix: str) -> int | None:
    if not archive.exists():
        return None
    try:
        output = git_run(archive, "for-each-ref", prefix, "--format=%(refname)").stdout
    except (GitError, OSError):
        return None
    return len(output.splitlines())


def _target_view(data_dir: Path, task: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    archive = archive_path(data_dir, task)
    total = _ref_count(archive, "refs/preserved")
    confirmed = _ref_count(archive, f"refs/pushed/{target['id']}/history/")
    if total is None or confirmed is None:
        history_count = "—"
    else:
        history_count = f"{confirmed} / {total}"
    if not target.get("bound"):
        status_key, status_class = "task.target.unbound", "warn"
    elif target.get("last_status") == "ok":
        status_key, status_class = "task.target.healthy", "ok"
    elif target.get("last_status"):
        status_key, status_class = "task.target.failed", "error"
    else:
        status_key, status_class = "task.target.not_synced", "warn"
    return {
        **target,
        "status_key": status_key,
        "status_class": status_class,
        "history_count": history_count,
        "history_total": total,
        "history_confirmed": confirmed,
        "attention": status_key != "task.target.healthy",
    }


def _event_view(event: dict[str, Any], targets: list[dict[str, Any]]) -> dict[str, Any]:
    label = str(event.get("preserved_ref") or "").removeprefix("refs/preserved/")
    commands = []
    if label:
        for target in targets:
            history_url = target["history_url"]
            commands.append(
                {
                    "history_url": history_url,
                    "history_arg": shlex.quote(history_url),
                    "clone_dir": shlex.quote(
                        history_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
                    ),
                    "tag": shlex.quote(f"gw/{label}"),
                }
            )
    return {**event, "tag": f"gw/{label}" if label else "", "commands": commands}


def _managed_by(store, task: dict[str, Any]) -> dict[str, Any]:
    if not task.get("access_id"):
        return {"manual": True, "instance": None, "access": None}
    access = store.get_access(task["access_id"])
    instance = store.get_instance(access["instance_id"]) if access else None
    return {
        "manual": False,
        "instance": instance,
        "access": access,
    }


def detail_page(
    request: Request,
    csrf: str,
    task_id: int,
    status: int = 200,
    *,
    error: str | None = None,
    task_values: dict | None = None,
    target_values: dict | None = None,
    target_edit: dict | None = None,
    provision_values: dict | None = None,
    edit_open: bool = False,
):
    store = _store(request)
    task = _require_task(store, task_id)
    raw_targets = store.list_targets(task_id)
    secrets = store.secret_values()
    if target_edit is not None:
        target_edit = {
            **target_edit,
            "name": _redact(target_edit["name"], secrets),
            "main_url": _safe_url(target_edit["main_url"], secrets),
            "history_url": _safe_url(target_edit["history_url"], secrets),
        }
    safe_targets = [
        {
            **row,
            "main_url": _safe_url(row.get("main_url"), secrets),
            "history_url": _safe_url(row.get("history_url"), secrets),
        }
        for row in raw_targets
    ]
    targets = [_target_view(request.app.state.data_dir, task, row) for row in safe_targets]
    events = [_event_view(row, safe_targets) for row in store.list_events(task_id)]
    runs = store.list_runs(task_id)
    effective = float(request.app.state.scheduler.effective_interval(task))
    configured = float(task.get("interval_minutes") or 0)
    failure_count = int(task.get("consecutive_failures") or 0)
    if effective > configured + 0.01:
        if failure_count > 1:
            interval_reason = "task.interval_backoff"
        else:
            interval_reason = "task.interval_duration"
    else:
        interval_reason = None
    source_cred_id = task.get("source_cred_id")
    if task.get("access_id"):
        managed_access = store.get_access(task["access_id"])
        if managed_access:
            source_cred_id = managed_access.get("cred_id")
    source_cred_row = store.get_cred(source_cred_id) if source_cred_id else None
    source_cred = (
        {
            "name": source_cred_row["name"],
            "kind": source_cred_row["kind"],
            "username": source_cred_row.get("username"),
        }
        if source_cred_row
        else None
    )
    managed = _managed_by(store, task)
    task_context = {
        **task,
        "source_url": _safe_url(task.get("source_url"), secrets),
        "source_credential": source_cred,
        "managed": managed,
        "effective_interval": max(1, math.ceil(effective)),
        "interval_reason": interval_reason,
    }
    return _page(
        request,
        "task_detail.html",
        csrf,
        status=status,
        error=error,
        task=task_context,
        targets=targets,
        events=events,
        runs=runs,
        creds=_cred_summaries(store),
        platforms=_platform_summaries(store),
        task_values=task_values or _task_values_from_row(task),
        target_values=target_values
        or {"name": "", "main_url": "", "history_url": "", "cred_id": ""},
        target_edit=target_edit,
        provision_values=provision_values or {"access_id": "", "owner": "", "name": ""},
        edit_open=edit_open,
        min_sync_minutes=store.min_sync_minutes,
    )


def _create_manual(
    request: Request,
    task_data: dict[str, Any],
    target_data: dict[str, Any],
) -> int:
    store = _store(request)
    task_id = store.add_task(**task_data)
    archive = archive_path(request.app.state.data_dir, store.get_task(task_id))
    try:
        target_id = store.add_target(
            task_id,
            target_data["name"],
            target_data["main_url"],
            target_data["history_url"],
            target_data["cred_id"],
        )
        bind_target(store, request.app.state.data_dir, target_id)
    except BaseException:
        # 撤销本次提交写入的任务和目标；档案仓是本次新建的，只在还没有任何引用时删除。
        store.delete_task(task_id)
        if archive.exists() and not git_run(archive, "for-each-ref", check=False).stdout.strip():
            shutil.rmtree(archive, ignore_errors=True)
        raise
    request.app.state.scheduler.trigger(task_id)
    return task_id


def _create_platform(
    request: Request,
    task_data: dict[str, Any],
    target_data: dict[str, Any],
) -> int:
    store = _store(request)
    task_id = store.add_task(**task_data)
    provision_target(
        store,
        request.app.state.data_dir,
        task_id,
        target_data["access_id"],
        target_data["owner"],
        target_data["name"],
    )
    request.app.state.scheduler.trigger(task_id)
    return task_id


def register(app: FastAPI) -> None:
    """Register task wizard and detail GET routes."""

    @app.get("/tasks/new", response_class=HTMLResponse)
    def task_new_form(request: Request, csrf: CsrfToken):
        query = request.query_params
        return _wizard_page(
            request,
            csrf,
            step=_step(query.get("step")),
            mode=_mode(query.get("mode")),
        )

    @app.post("/tasks/new", response_class=HTMLResponse)
    async def task_new_submit(request: Request, csrf: VerifiedToken):
        form = await request.form()
        store = _store(request)
        step = _step(form.get("step"))
        mode = _mode(form.get("mode"))
        task_values, target_values, provision_values = _form_values(form, store)
        confirm_values = {
            "writer": any(
                _form_bool(form.get(name))
                for name in ("confirm_writer", "prereq_writer", "prerequisite_writer")
            ),
            "private": any(
                _form_bool(form.get(name))
                for name in ("confirm_private", "prereq_private", "prerequisite_private")
            ),
            "no_ci": any(
                _form_bool(form.get(name))
                for name in ("confirm_no_ci", "prereq_no_ci", "prerequisite_no_ci")
            ),
        }
        if form.get("back") is not None:
            previous = "source" if step == "targets" else "targets"
            return _wizard_page(
                request,
                csrf,
                step=previous,
                mode=mode,
                task_values=task_values,
                target_values=target_values,
                provision_values=provision_values,
                confirm_values=confirm_values,
            )
        if form.get("change_mode") is not None:
            return _wizard_page(
                request,
                csrf,
                step="targets",
                mode=_mode(form.get("change_mode")),
                task_values=task_values,
                target_values=target_values,
                provision_values=provision_values,
                confirm_values=confirm_values,
            )
        try:
            if step not in _STEPS:
                raise ValueError(t("wizard.invalid_step"))
            task_data = _validate_source(request, task_values)
            if step == "source":
                return _wizard_page(
                    request,
                    csrf,
                    step="targets",
                    mode=mode,
                    task_values=task_values,
                    target_values=target_values,
                    provision_values=provision_values,
                    confirm_values=confirm_values,
                )
            target_data = _validate_target(request, mode, target_values, provision_values)
            if step == "targets":
                return _wizard_page(
                    request,
                    csrf,
                    step="review",
                    mode=mode,
                    task_values=task_values,
                    target_values=target_values,
                    provision_values=provision_values,
                    confirm_values=confirm_values,
                )
            if not _prerequisites_ok(form):
                raise ValueError(t("wizard.prerequisites_required"))
            if mode == "platform":
                task_id = _create_platform(request, task_data, target_data)
            else:
                task_id = _create_manual(request, task_data, target_data)
        except (ArchiveBusy, GitError, TargetNotEmpty, sqlite3.IntegrityError, ValueError) as exc:
            detail = _form_error(request, exc)
            return _wizard_page(
                request,
                csrf,
                status=400,
                step=step,
                mode=mode,
                error=detail,
                task_values=task_values,
                target_values=target_values,
                provision_values=provision_values,
                confirm_values=confirm_values,
            )
        except Exception as exc:
            # Provisioning may have created a task before a platform API failure;
            # preserve that task and let the user resume from its detail page.
            detail = t(
                "wizard.creation_failed",
                detail=_redact(str(exc), store.secret_values()),
            )
            return _wizard_page(
                request,
                csrf,
                status=400,
                step=step,
                mode=mode,
                error=detail,
                task_values=task_values,
                target_values=target_values,
                provision_values=provision_values,
                confirm_values=confirm_values,
            )
        return _redirect(f"/tasks/{task_id}")

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(request: Request, task_id: int, csrf: CsrfToken):
        return detail_page(request, csrf, task_id)
