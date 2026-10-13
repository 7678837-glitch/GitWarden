"""任务执行器：采集来源，再逐个推送并核对已绑定的目标。"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gitwarden import logs, telemetry
from gitwarden.archive import ArchiveBusy, _locked, capture, init_archive
from gitwarden.creds import git_env
from gitwarden.db import Store, now
from gitwarden.external import scan_external
from gitwarden.git import GitError, execution_context, run
from gitwarden.i18n import msg
from gitwarden.target import Target, audit, bind, forget, push_target

log = logging.getLogger("gitwarden")
_QUIET_KINDS = ("create", "fast-forward")


def archive_path(data_dir: Path, task: dict) -> Path:
    return data_dir / "archives" / f"{task['archive_uuid']}.git"


def _target(row: dict) -> Target:
    return Target(str(row["id"]), row["main_url"], row["history_url"])


def _redact(text: str, secrets: list[str]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "***")
    return text


def bind_target(store: Store, data_dir: Path, target_id: int) -> None:
    row = store.get_target(target_id)
    task = store.get_task(row["task_id"])
    archive = archive_path(data_dir, task)
    init_archive(archive)
    with git_env(_cred(store, row["cred_id"]), data_dir / "work") as env:
        bind(archive, _target(row), env=env)
    store.set_bound(target_id, True)


def forget_target(store: Store, data_dir: Path, target_id: int) -> None:
    row = store.get_target(target_id)
    forget(archive_path(data_dir, store.get_task(row["task_id"])), target_id)


def _cred(store: Store, cred_id: int | None) -> dict | None:
    return store.get_cred(cred_id) if cred_id else None


def _push_one(store: Store, data_dir: Path, archive: Path, row: dict) -> tuple[str, str]:
    target = _target(row)
    with git_env(_cred(store, row["cred_id"]), data_dir / "work") as env:
        res = push_target(archive, target, env=env)
        if res.history_conflicts:
            return "history_conflict", msg(
                "run.history_conflict", items=", ".join(res.history_conflicts)
            )
        if res.gate_blocked:
            return (
                "gate_blocked",
                msg("run.gate_blocked", items=", ".join(res.gate_blocked)),
            )
        if not res.main_ok:
            return "conflict", msg("run.main_conflict", items=", ".join(res.main_conflicts))
        with _locked(archive):
            report = audit(archive, target, env=env)
        store._exec("UPDATE targets SET last_audit_at=? WHERE id=?", (now(), row["id"]))
    if report.missing or report.modified:
        return "audit_alert", msg(
            "run.audit_alert", missing=report.missing, modified=report.modified
        )
    return "ok", msg("run.sync_done")


def _exception_raw(exc: BaseException) -> str:
    parts = []
    for name in ("stderr", "stdout"):
        value = getattr(exc, name, None)
        if isinstance(value, str) and value:
            parts.append(value)
    if parts:
        return "".join(parts)
    return str(exc)


def _record_steps(
    store: Store,
    run_id: int,
    records: list[telemetry.StepRecord],
    *,
    step_name: str,
    target_id: int | None,
    detailed: bool,
    error: BaseException | None = None,
    error_level: str = "error",
) -> None:
    error_raw = _exception_raw(error) if error is not None else None
    if not records:
        logs.add_step(
            store,
            run_id,
            step_name,
            str(error) if error is not None else None,
            level=error_level if error is not None else "info",
            target_id=target_id,
            raw=error_raw if error is not None else None,
        )
        return

    for record in records:
        if error is None and not record.failed:
            level = "info"
            message = None
            raw = record.raw if detailed else None
        elif error is None:
            level = "error"
            message = record.error_message or msg("run.git_command_failed")
            raw = record.raw
        else:
            level = error_level
            message = str(error)
            raw = error_raw or record.raw
            if error_raw and record.raw and record.raw != error_raw:
                raw = f"{error_raw}\n{record.raw}"
        logs.add_step(
            store,
            run_id,
            record.name,
            message,
            level=level,
            target_id=target_id,
            duration_ms=record.duration_ms,
            raw=raw,
        )


def _observed(
    store: Store,
    run_id: int,
    func: Callable[[], Any],
    *,
    step_name: str,
    target_id: int | None,
    detailed: bool,
    error_level: str = "error",
) -> Any:
    records: list[telemetry.StepRecord]
    try:
        with telemetry.collect() as records:
            result = func()
    except Exception as exc:
        _record_steps(
            store,
            run_id,
            records,
            step_name=step_name,
            target_id=target_id,
            detailed=detailed,
            error=exc,
            error_level=error_level,
        )
        raise
    _record_steps(
        store,
        run_id,
        records,
        step_name=step_name,
        target_id=target_id,
        detailed=detailed,
    )
    return result


def _scan(archive: Path) -> list[str]:
    with telemetry.step("scan"):
        return scan_external(archive)


def _has_mirror_refs(archive: Path) -> bool:
    if not archive.exists():
        return False
    return bool(run(archive, "for-each-ref", "refs/heads", "refs/tags").stdout.strip())


def _archive_size(archive: Path) -> int:
    total = 0
    if not archive.exists():
        return total
    for path in archive.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def _progress_text(
    store: Store, parts: list[str], archive: Path, initial_backup: bool
) -> str | None:
    if parts:
        text = logs.redact_text(store, "".join(parts)) or ""
        # 进度只看最新一行，保留末尾；16000 字符在 UTF-8 下不超过 64KB 上限。
        return text[-16000:]
    if initial_backup:
        return f"received {_archive_size(archive)} bytes"
    return None


def _execute(
    store: Store,
    data_dir: Path,
    task: dict,
    run_id: int,
    *,
    detailed: bool,
) -> str:
    archive = archive_path(data_dir, task)
    init_archive(archive)
    access = store.get_access(task["access_id"]) if task["access_id"] else None
    source_cred = access["cred_id"] if access else task["source_cred_id"]
    capture_error = None
    result = None
    try:
        with git_env(_cred(store, source_cred), data_dir / "work") as env:
            result = _observed(
                store,
                run_id,
                lambda: capture(archive, task["source_url"], task["name"], env=env),
                step_name="capture",
                target_id=None,
                detailed=detailed,
            )
    except ArchiveBusy:
        raise
    except Exception as exc:
        capture_error = exc
        summary = access["last_discovery_summary"] if access else None
        missing = re.search(
            r"returned error: 404|repository .+ not found|repository not found|"
            r"repository .+ does not exist|does not appear to be a git repository",
            exc.stderr if isinstance(exc, GitError) else "",
            re.IGNORECASE,
        )
        if (
            missing
            and summary
            and summary.get("complete")
            and task["platform_repo_id"] not in summary.get("seen_repo_ids", [])
        ):
            store.update_task(task["id"], managed_state="source_gone")
    for event in result.events if result is not None else []:
        logs.add_ref(store, run_id, event.ref, event.kind, event.old, event.new)
        if event.kind not in _QUIET_KINDS:
            store.add_event(
                task["id"], event.ref, event.kind, event.old, event.new, event.preserved_ref
            )

    try:
        notes = _observed(
            store,
            run_id,
            lambda: _scan(archive),
            step_name="scan",
            target_id=None,
            detailed=detailed,
            error_level="warn",
        )
        store.set_external_note(task["id"], "; ".join(notes) or None)
    except Exception as exc:
        log.warning(
            "任务 %s 外部内容扫描失败: %s",
            task["name"],
            logs.redact_text(store, str(exc)),
        )

    successes = 0
    failures = 0
    for row in store.list_targets(task["id"]):
        if not row["bound"]:
            continue
        try:
            t_status, detail = _observed(
                store,
                run_id,
                lambda row=row: _push_one(store, data_dir, archive, row),
                step_name="target",
                target_id=row["id"],
                detailed=detailed,
            )
        except ArchiveBusy:
            raise
        except Exception as exc:
            t_status, detail = (
                "error",
                logs.redact_text(store, str(exc)) or msg("run.target_failed"),
            )
            log.error("目标 %s 推送失败: %s", row["name"], detail)
        store.set_target_status(row["id"], t_status, detail)
        if t_status == "ok":
            successes += 1
        else:
            failures += 1
            logs.add_step(
                store,
                run_id,
                "target",
                detail,
                level="error",
                target_id=row["id"],
            )

    if capture_error is not None:
        raise capture_error
    if failures and successes:
        return "partial"
    if failures:
        return "failed"
    return "ok"


def _execution_context(progress: Callable[[str], None]):
    def observer(args: list[str], proc: object, _duration_ms: int) -> None:
        telemetry.command_output(proc, args)

    def on_output(text: str) -> None:
        telemetry.output(text)
        progress(text)

    return execution_context(observer=observer, on_output=on_output)


def _maintain_archive(store: Store, run_id: int, archive: Path) -> None:
    try:
        with _locked(archive):
            marker = archive / "gitwarden-maintenance"
            if marker.exists() and time.time() - marker.stat().st_mtime < 7 * 86400:
                return
            run(archive, "gc", "--no-prune")
            marker.touch()
    except Exception as exc:
        logs.add_step(store, run_id, "maintenance", str(exc), level="warn")


def run_task(store: Store, data_dir: Path, task_id: int) -> bool | None:
    """返回来源采集是否成功；锁忙返回 None，不计入失败退避。"""
    task = store.get_task(task_id)
    archive = archive_path(data_dir, task)
    init_archive(archive)
    initial_backup = not _has_mirror_refs(archive)
    run_id = store.start_run(task_id, initial_backup=initial_backup)
    detailed = bool(task["detailed_logging"])
    progress_parts: list[str] = []
    message = None
    status = "failed"
    captured = False
    last_progress = 0.0

    def progress(text: str) -> None:
        nonlocal last_progress
        progress_parts.append(text)
        if time.monotonic() - last_progress >= 1:
            store._exec(
                "UPDATE runs SET progress=? WHERE id=?",
                (_progress_text(store, progress_parts, archive, initial_backup), run_id),
            )
            last_progress = time.monotonic()

    try:
        with _execution_context(progress):
            status = _execute(store, data_dir, task, run_id, detailed=detailed)
            captured = True
    except ArchiveBusy:
        captured = None
        status, message = "busy", msg("run.archive_busy")
        logs.add_step(store, run_id, "capture", message, level="warn")
    except Exception as exc:
        message = logs.redact_text(store, str(exc))
        log.error("任务 %s 失败: %s", task["name"], message)
    if captured is not None:
        _maintain_archive(store, run_id, archive)
    progress = _progress_text(store, progress_parts, archive, initial_backup)
    store.finish_run(run_id, status, message, progress=progress)
    store.finish_task(task_id, status)
    return captured


def retry_target(store: Store, data_dir: Path, target_id: int) -> str:
    row = store.get_target(target_id)
    task = store.get_task(row["task_id"])
    archive = archive_path(data_dir, task)
    run_id = store.start_run(task["id"])
    detailed = bool(task["detailed_logging"])
    status = "failed"
    detail = msg("run.target_failed")
    try:
        with _execution_context(lambda _text: None):
            status, detail = _observed(
                store,
                run_id,
                lambda: _push_one(store, data_dir, archive, row),
                step_name="target",
                target_id=target_id,
                detailed=detailed,
            )
    except ArchiveBusy:
        status, detail = "busy", msg("run.archive_busy")
    except Exception as exc:
        status, detail = "error", logs.redact_text(store, str(exc)) or msg("run.target_failed")
    store.set_target_status(target_id, status, detail)
    store.finish_run(run_id, "ok" if status == "ok" else "failed", detail)
    return status


def audit_target(store: Store, data_dir: Path, target_id: int):
    row = store.get_target(target_id)
    task = store.get_task(row["task_id"])
    archive = archive_path(data_dir, task)
    run_id = store.start_run(task["id"])
    detailed = bool(task["detailed_logging"])
    try:
        with git_env(_cred(store, row["cred_id"]), data_dir / "work") as env:
            with _execution_context(lambda _text: None):
                with _locked(archive):
                    report = _observed(
                        store,
                        run_id,
                        lambda: audit(archive, _target(row), env=env),
                        step_name="audit",
                        target_id=target_id,
                        detailed=detailed,
                    )
    except Exception as exc:
        detail = logs.redact_text(store, str(exc)) or msg("run.audit_failed")
        store.finish_run(run_id, "failed", detail)
        raise
    store._exec(  # noqa: SLF001 - db schema 已有 last_audit_at，保持 Store API 范围不变
        "UPDATE targets SET last_audit_at=? WHERE id=?", (now(), target_id)
    )
    detail = (
        msg("run.audit_alert", missing=report.missing, modified=report.modified)
        if report.missing or report.modified
        else msg("run.audit_done")
    )
    store.finish_run(run_id, "ok", detail)
    return report
