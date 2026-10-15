"""WebUI 与 HTTP 接口：登录会话、CSRF、任务/目标/凭据管理、恢复包下载、健康检查。"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.background import BackgroundTask

from gitwarden.archive import ArchiveBusy
from gitwarden.config import Config
from gitwarden.db import Store, now
from gitwarden.git import GitError, run
from gitwarden.i18n import (
    CSRF_COOKIE,
    DEFAULT_LANG,
    LANG_COOKIE,
    language_for_cookie,
    msg,
    normalize_lang,
    render,
    reset_language,
    set_language,
    t,
    translate,
)
from gitwarden.platform import PlatformError
from gitwarden.provision import provision_target
from gitwarden.runner import archive_path, bind_target, forget_target
from gitwarden.scheduler import Scheduler
from gitwarden.target import TargetMismatch, TargetNotEmpty

log = logging.getLogger("gitwarden")

SESSION_COOKIE = "gw_session"
TEMPLATES_DIR = Path(__file__).with_name("templates")
_ALL_ROWS = 2**31 - 1

STATUS_LABELS = {
    "ok": "common.status.success",
    "failed": "common.status.failed",
    "busy": "common.status.busy",
    "gate_blocked": "common.status.gate_blocked",
    "conflict": "common.status.conflict",
    "history_conflict": "common.status.history_conflict",
    "audit_alert": "common.status.audit_alert",
    "error": "common.status.error",
}
KIND_LABELS = {
    "create": "common.kind.create",
    "fast-forward": "common.kind.fast_forward",
    "force": "common.kind.force",
    "tag-rewrite": "common.kind.tag_rewrite",
    "delete": "common.kind.delete",
}
CRED_KINDS = {"https": "common.credential.https", "ssh": "common.credential.ssh"}
PLATFORM_KINDS = {"gitea": "Gitea", "forgejo": "Forgejo"}


def _ts(value: str | None) -> str:
    if not value:
        return "—"
    return datetime.fromisoformat(value).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@pass_context
def _ago(context, value: str | None) -> str:
    lang = context.get("lang", DEFAULT_LANG)
    if not value:
        return translate("common.status.never_run", lang)
    seconds = int((datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds())
    if seconds < 60:
        return translate("common.time.just_now", lang)
    if seconds < 3600:
        return translate("common.time.minutes_ago", lang, value=seconds // 60)
    if seconds < 86400:
        return translate("common.time.hours_ago", lang, value=seconds // 3600)
    return translate("common.time.days_ago", lang, value=seconds // 86400)


def _label(mapping: dict[str, str], empty: str):
    @pass_context
    def render(context, value: str | None) -> str:
        lang = context.get("lang", DEFAULT_LANG)
        if value is None:
            return translate(empty, lang) if empty else ""
        key = mapping.get(value)
        return translate(key, lang) if key else value

    return render


@pass_context
def _template_msg(context, value: str | None) -> str:
    return render(value, context.get("lang", DEFAULT_LANG))


@pass_context
def _template_t(context, key: str, **values: object) -> str:
    return translate(key, context.get("lang", DEFAULT_LANG), **values)


def _short(oid: str | None) -> str:
    return "—" if not oid else oid[:10]


def _progress_summary(raw: str | None) -> str:
    lines = [line.strip() for line in (raw or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if re.search(r"Receiving objects:|Writing objects:|received \d+", line, re.I):
            return line
    return lines[-1] if lines else ""


templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["t"] = _template_t
templates.env.filters.update(
    ts=_ts,
    ago=_ago,
    short=_short,
    progress_summary=_progress_summary,
    msg=_template_msg,
    status=_label(STATUS_LABELS, "common.status.never_run"),
    target_status=_label(STATUS_LABELS, "common.status.not_synced"),
    kind=_label(KIND_LABELS, "common.empty"),
    cred_kind=_label(CRED_KINDS, "common.empty"),
)


class _NotLoggedIn(Exception):
    pass


class _CsrfError(Exception):
    pass


def _version() -> str:
    return (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()


def _store(request: Request) -> Store:
    return request.app.state.store


def _session(request: Request) -> str:
    """校验登录会话，返回该会话的 CSRF 令牌。"""
    token = request.cookies.get(SESSION_COOKIE)
    sessions: dict[str, str] = request.app.state.sessions
    if token is None or token not in sessions:
        raise _NotLoggedIn
    return sessions[token]


async def _verified(request: Request) -> str:
    """POST 路由的依赖：要求登录且表单带有效 csrf 字段。"""
    csrf = _session(request)
    form = await request.form()
    submitted = str(form.get("csrf", ""))
    if not hmac.compare_digest(submitted.encode(), csrf.encode()):
        raise _CsrfError
    return csrf


async def _anonymous_verified(request: Request) -> str:
    """验证匿名语言切换使用的双提交 CSRF 令牌。"""
    cookie = request.cookies.get(CSRF_COOKIE)
    form = await request.form()
    submitted = str(form.get("csrf", ""))
    if not cookie or not hmac.compare_digest(submitted.encode(), cookie.encode()):
        raise _CsrfError
    return cookie


CsrfToken = Annotated[str, Depends(_session)]
VerifiedToken = Annotated[str, Depends(_verified)]
AnonymousCsrfToken = Annotated[str, Depends(_anonymous_verified)]


def _page(request: Request, name: str, csrf: str | None, status: int = 200, **context: object):
    lang = language_for_cookie(request.cookies.get(LANG_COOKIE))
    lang_csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
    scheduler_status = request.app.state.scheduler.status()

    def request_t(key: str, **values: object) -> str:
        return translate(key, lang, **values)

    payload = {
        "csrf": csrf,
        "lang_csrf": lang_csrf,
        "lang": lang,
        "version": getattr(request.app.state, "version", "unknown"),
        "scheduler_status": scheduler_status,
        "t": request_t,
        **context,
    }
    response = templates.TemplateResponse(request, name, payload, status_code=status)
    if request.cookies.get(CSRF_COOKIE) is None:
        response.set_cookie(CSRF_COOKIE, lang_csrf, httponly=False, samesite="strict", path="/")
    if request.cookies.get(LANG_COOKIE) is None:
        response.set_cookie(LANG_COOKIE, lang, httponly=False, samesite="strict", path="/")
    return response


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _redact(text: str, secrets_: list[str]) -> str:
    # 先翻译再脱敏：消息参数在库里是 JSON 转义后的形式，转义可能让凭据匹配不上。
    text = render(text)
    for s in sorted(set(secrets_), key=len, reverse=True):
        if s:
            text = text.replace(s, "***")
    # Do not echo credentials embedded in a URL even if the value was not stored yet.
    text = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@",
        r"\1***:***@",
        text,
    )
    return text


def _db_error(exc: Exception) -> str:
    if isinstance(exc, sqlite3.IntegrityError):
        return t("error.data_conflict")
    message = str(exc)
    if "不能低于" in message:
        match = re.search(r"(\d+) 分钟", message)
        return t("error.minimum_interval", minutes=match.group(1) if match else 1)
    if "引用，不能删除" in message:
        return t("error.in_use")
    for fragment, key in (
        ("URL 不得内嵌凭据", "error.url_credentials"),
        ("URL 协议", "error.url_protocol"),
        ("URL 缺少主机", "error.url_host"),
        ("主镜像与历史仓地址必须不同", "error.target_urls_same"),
        ("平台地址必须", "error.platform_url"),
        ("接入凭据必须", "error.access_credential"),
        ("接入范围必须", "error.scope"),
        ("接入规则必须", "error.rule"),
        ("owners 范围至少", "error.owners_required"),
    ):
        if fragment in message:
            return t(key)
    return message


_FIELD_KEYS = {
    "任务名称": "name",
    "任务": "task",
    "来源地址": "source",
    "同步间隔": "interval",
    "实例": "instance",
    "接入名称": "access",
    "平台名称": "instance",
    "平台地址": "platform_url",
    "目标名称": "target",
    "所有者": "owner",
    "凭据名称": "credential",
    "discover_interval_minutes": "discovery_interval",
    "default_sync_interval_minutes": "interval",
}


def _text(raw: str, label: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError(t("error.required", field=t("field." + _FIELD_KEYS.get(label, label))))
    return value


def _positive_int(raw: str, label: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(
            t("error.integer", field=t("field." + _FIELD_KEYS.get(label, label)))
        ) from None
    if value < 1:
        raise ValueError(t("error.positive", field=t("field." + _FIELD_KEYS.get(label, label))))
    return value


def _cred_id(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(t("error.credential_invalid")) from None


def _access_id(raw: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(t("error.access_missing")) from None


def _form_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes", "enable", "enabled"}


def _form_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[,\n]", str(value)) if item.strip()]


def _access_settings(form: Any, current: dict | None = None) -> dict[str, object]:
    current = current or {}

    def text(name: str, default: str = "") -> str:
        raw = form.get(name)
        return default if raw is None else str(raw).strip()

    def integer(name: str, default: int) -> int:
        raw = form.get(name)
        return default if raw is None or str(raw).strip() == "" else _positive_int(str(raw), name)

    settings: dict[str, object] = {
        "cred_id": _cred_id(text("cred_id", str(current.get("cred_id", "")))),
        "scope_mode": text("scope_mode", str(current.get("scope_mode", "all"))),
        "owners": _form_list(
            form.get("owners") if form.get("owners") is not None else current.get("owners", [])
        ),
        "include_forks": _form_bool(
            form.get("include_forks"), bool(current.get("include_forks", 0))
        ),
        "include_archived": _form_bool(
            form.get("include_archived"), bool(current.get("include_archived", 1))
        ),
        "include_public": _form_bool(
            form.get("include_public"), bool(current.get("include_public", 1))
        ),
        "include_private": _form_bool(
            form.get("include_private"), bool(current.get("include_private", 1))
        ),
        "rule_mode": text("rule_mode", str(current.get("rule_mode", "exclude"))),
        "patterns": _form_list(
            form.get("patterns")
            if form.get("patterns") is not None
            else current.get("patterns", [])
        ),
        "auto_enable": _form_bool(form.get("auto_enable"), bool(current.get("auto_enable", 1))),
        "detailed_default": _form_bool(
            form.get("detailed_default"), bool(current.get("detailed_default", 0))
        ),
        "default_sync_interval_minutes": integer(
            "default_sync_interval_minutes",
            int(current.get("default_sync_interval_minutes", 60)),
        ),
        "discover_interval_minutes": integer(
            "discover_interval_minutes", int(current.get("discover_interval_minutes", 60))
        ),
    }
    return settings


def _discovery_context(result: object) -> dict[str, object]:
    """将发现分类和摘要传入平台页面。"""
    if isinstance(result, dict):
        return result
    context: dict[str, object] = {
        key: list(getattr(result, key, []))
        for key in (
            "created",
            "existing",
            "renamed",
            "conflicts",
            "skipped",
            "disabled",
            "not_visible",
            "other_access",
        )
    }
    items = list(getattr(result, "items", []))
    context["items"] = items
    buckets = {
        "Add": "created",
        "Keep": "existing",
        "Disable": "disabled",
        "Skip": "skipped",
        "Other access": "other_access",
    }
    for item in items:
        category = str(item.get("category", "")) if isinstance(item, dict) else ""
        bucket = buckets.get(category)
        if bucket and item.get("name") not in context[bucket]:
            context[bucket].append(item["name"])
    return context


def preview(store: Store, access_id: int, draft_settings: dict[str, object]):
    """按当前草稿计算只读预览。"""
    from gitwarden.discovery import preview as discover_preview

    return discover_preview(store, access_id, draft_settings)


def discover(store: Store, access_id: int):
    """执行一次接入发现。"""
    from gitwarden.discovery import discover as discover_access

    return discover_access(store, access_id)


def retry_target(store: Store, data_dir: Path, target_id: int):
    from gitwarden.runner import retry_target as run_retry_target

    return run_retry_target(store, data_dir, target_id)


def audit_target(store: Store, data_dir: Path, target_id: int):
    from gitwarden.runner import audit_target as run_audit_target

    return run_audit_target(store, data_dir, target_id)


def _activity_name(path: str) -> str:
    if path == "/login":
        return "login"
    if path == "/logout":
        return "logout"
    if path == "/instances":
        return "instance.create"
    if path.startswith("/instances/"):
        return "instance.update"
    if path == "/accesses":
        return "access.create"
    if path.startswith("/accesses/"):
        if path.endswith("/discover"):
            return "access.discover"
        if path.endswith("/delete"):
            return "access.remove"
        if path.endswith("/preview"):
            return "access.preview"
        return "access.save"
    if path == "/tasks/new":
        return "task.create"
    if path == "/tasks/bulk":
        return "task.bulk"
    if path.startswith("/tasks/"):
        if path.endswith("/toggle"):
            return "task.toggle"
        if path.endswith("/detailed"):
            return "task.detailed"
        if path.endswith("/run"):
            return "task.run"
        if "/targets" in path:
            return "target.update"
        return "task.update"
    if path.startswith("/targets/"):
        if path.endswith("/retry"):
            return "target.retry"
        if path.endswith("/audit"):
            return "target.audit"
        if path.endswith("/delete"):
            return "target.delete"
        return "target.update"
    if path == "/creds":
        return "credential.create"
    if path.startswith("/creds/"):
        return "credential.update" if path.endswith("/edit") else "credential.delete"
    return "post"


def _record_activity(request: Request, action: str, status_code: int) -> None:
    try:
        from gitwarden.logs import add_activity

        store = getattr(request.app.state, "store", None)
        if store is None:
            return
        client = request.client
        ip = client.host if client else None
        result = "success" if status_code < 400 else f"status={status_code}"
        extra = getattr(request.state, "activity_detail", None)
        if extra:
            result = f"{result} · {extra}"
        add_activity(store, action, request.url.path, result, ip)
    except Exception:
        log.exception("写入 Web 操作记录失败")


def _task_values(name, source_url, source_cred_id, interval_minutes, enabled) -> dict:
    return {
        "name": name,
        "source_url": source_url,
        "source_cred_id": source_cred_id,
        "interval_minutes": interval_minutes,
        "enabled": enabled,
    }


def _blank_task() -> dict:
    return _task_values("", "", "", "60", True)


def _task_values_from_row(task: dict) -> dict:
    return _task_values(
        task["name"],
        task["source_url"],
        "" if task["source_cred_id"] is None else str(task["source_cred_id"]),
        str(task["interval_minutes"]),
        bool(task["enabled"]),
    )


def _parse_task(values: dict) -> dict:
    return {
        "name": _text(values["name"], "任务名称"),
        "source_url": values["source_url"].strip(),
        "source_cred_id": _cred_id(values["source_cred_id"]),
        "interval_minutes": _positive_int(values["interval_minutes"], "间隔分钟"),
        "enabled": bool(values["enabled"]),
    }


def _cred_summaries(store: Store) -> list[dict]:
    """只返回可展示字段，secret、private_key、known_hosts 永不进入模板上下文。"""
    return [
        {"id": c["id"], "name": c["name"], "kind": c["kind"], "username": c["username"]}
        for c in store.list_creds()
    ]


def _localized_labels(request: Request, mapping: dict[str, str]) -> dict[str, str]:
    lang = language_for_cookie(request.cookies.get(LANG_COOKIE))
    return {
        key: translate(value, lang) if value.startswith("common.") else value
        for key, value in mapping.items()
    }


def _platform_summaries(store: Store) -> list[dict]:
    """只返回平台和凭据名称，平台凭据的 secret 永不进入模板上下文。"""
    summaries = []
    for access in store.list_accesses():
        instance = store.get_instance(access["instance_id"])
        row = {**instance, **access}
        cred = store.get_cred(access["cred_id"])
        row["cred_name"] = cred["name"] if cred else None
        summaries.append(row)
    return summaries


def _require_task(store: Store, task_id: int) -> dict:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(404, t("error.task_missing"))
    return task


def _require_target(store: Store, target_id: int) -> dict:
    target = store.get_target(target_id)
    if target is None:
        raise HTTPException(404, t("error.target_missing"))
    return target


def _task_page(
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
    from gitwarden.ui_task import detail_page

    return detail_page(
        request,
        csrf,
        task_id,
        status=status,
        error=error,
        task_values=task_values,
        target_values=target_values,
        target_edit=target_edit,
        provision_values=provision_values,
        edit_open=edit_open,
    )


def _creds_page(
    request: Request,
    csrf: str,
    status: int = 200,
    *,
    error: str | None = None,
    values: dict | None = None,
    edit_id: int | None = None,
):
    from gitwarden.ui_platforms import creds_page

    return creds_page(request, csrf, status, error=error, values=values, edit_id=edit_id)


def _platform_page(
    request: Request,
    csrf: str,
    status: int = 200,
    *,
    error: str | None = None,
    values: dict | None = None,
    discovery: object | None = None,
    preview_access_id: int | None = None,
    draft_settings: dict | None = None,
):
    from gitwarden.ui_platforms import platform_page

    return platform_page(
        request,
        csrf,
        status,
        error=error,
        values=values,
        discovery=discovery,
        preview_access_id=preview_access_id,
        draft_settings=draft_settings,
    )


def _find_event(store: Store, task_id: int, event_id: int) -> dict | None:
    for event in store.list_events(task_id, limit=_ALL_ROWS):
        if event["id"] == event_id:
            return event
    return None


def create_app(config: Config, start_scheduler: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(
            config.data_dir,
            min_sync_minutes=config.min_sync_minutes,
            min_discovery_minutes=config.min_discovery_minutes,
        )
        store._exec(
            "UPDATE runs SET finished_at=?, status='failed', message=? WHERE finished_at IS NULL",
            (now(), msg("run.interrupted")),
        )
        scheduler = Scheduler(
            store,
            config.data_dir,
            config.tick_seconds,
            workers=config.workers,
            stall_minutes=config.stall_minutes,
            retention_days=config.log_retention_days,
        )
        app.state.store = store
        app.state.scheduler = scheduler
        if start_scheduler:
            scheduler.start()
        try:
            yield
        finally:
            if start_scheduler:
                scheduler.stop()

    app = FastAPI(
        title="GitWarden",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.data_dir = config.data_dir
    app.state.sessions = {}
    app.state.version = _version()

    @app.middleware("http")
    async def record_post_activity(request: Request, call_next):
        token = set_language(language_for_cookie(request.cookies.get(LANG_COOKIE)))
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if request.method == "POST" and request.url.path != "/lang":
                _record_activity(request, _activity_name(request.url.path), status)
            reset_language(token)

    @app.exception_handler(_NotLoggedIn)
    async def not_logged_in(request: Request, exc: _NotLoggedIn) -> Response:
        if request.method == "GET":
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(
            translate(
                "common.not_logged_in", language_for_cookie(request.cookies.get(LANG_COOKIE))
            ),
            status_code=401,
        )

    @app.exception_handler(_CsrfError)
    async def csrf_failed(request: Request, exc: _CsrfError) -> Response:
        return HTMLResponse(
            translate("common.csrf_error", language_for_cookie(request.cookies.get(LANG_COOKIE))),
            status_code=403,
        )

    @app.exception_handler(OverflowError)
    async def number_out_of_range(request: Request, exc: OverflowError) -> Response:
        # 超过 SQLite 64 位整数范围的 ID 或数字，来自用户输入。
        return HTMLResponse(
            translate("error.number_range", language_for_cookie(request.cookies.get(LANG_COOKIE))),
            status_code=400,
        )

    @app.get("/health")
    def health(request: Request) -> dict[str, str]:
        return {
            "version": request.app.state.version,
            "git_commit": os.environ.get("GW_GIT_COMMIT") or "unknown",
        }

    # 登录与退出
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return _page(request, "login.html", None)

    @app.post("/lang")
    async def change_language(request: Request, _csrf: AnonymousCsrfToken):
        form = await request.form()
        lang = normalize_lang(str(form.get("lang", DEFAULT_LANG)))
        target = str(form.get("next") or "/login")
        if not target.startswith("/") or target.startswith("//") or "\\" in target:
            target = "/login"
        response = _redirect(target)
        response.set_cookie(LANG_COOKIE, lang, httponly=False, samesite="strict", path="/")
        return response

    @app.post("/login")
    def login(request: Request, password: Annotated[str, Form()] = ""):
        if not hmac.compare_digest(password.encode(), config.admin_password.encode()):
            time.sleep(1)
            return _page(
                request,
                "login.html",
                None,
                status=401,
                error=translate(
                    "login.invalid_password", language_for_cookie(request.cookies.get(LANG_COOKIE))
                ),
            )
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        # Single-administrator service: a new login replaces any previous sessions so
        # the in-memory map cannot grow without bound across repeated sign-ins.
        request.app.state.sessions.clear()
        request.app.state.sessions[token] = csrf
        response = _redirect("/")
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    @app.post("/logout")
    def logout(request: Request, _csrf: VerifiedToken):
        request.app.state.sessions.pop(request.cookies.get(SESSION_COOKIE), None)
        response = _redirect("/login")
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # 任务

    @app.post("/tasks/bulk")
    async def task_bulk(request: Request, _csrf: VerifiedToken):
        form = await request.form()
        raw_ids = form.getlist("task_ids")
        if not raw_ids:
            raw_ids = form.getlist("task_id")
        if not raw_ids and form.get("ids") is not None:
            raw_ids = _form_list(form.get("ids"))
        task_ids: list[int] = []
        try:
            task_ids = [_positive_int(str(raw), "任务") for raw in raw_ids]
            action = str(form.get("action", "")).strip().lower().replace(" ", "_").replace("-", "_")
            if action in {"detailed", "detailed_logging"}:
                action = "detailed_on" if _form_bool(form.get("enabled"), True) else "detailed_off"
            if action not in {"run", "enable", "disable", "detailed_on", "detailed_off"}:
                raise ValueError(t("error.bulk_invalid"))
            store = _store(request)
            for task_id in task_ids:
                _require_task(store, task_id)
            request.state.activity_detail = f"action={action} tasks={len(task_ids)}"
            for task_id in task_ids:
                if action == "run":
                    request.app.state.scheduler.trigger(task_id)
                elif action == "enable":
                    store.update_task(task_id, enabled=True)
                elif action == "disable":
                    store.update_task(task_id, enabled=False)
                else:
                    store.update_task(task_id, detailed_logging=action == "detailed_on")
        except (ValueError, sqlite3.IntegrityError) as exc:
            return HTMLResponse(
                escape(_redact(_db_error(exc), _store(request).secret_values())), status_code=400
            )
        return _redirect("/tasks")

    @app.post("/tasks/{task_id}/edit")
    def task_edit(
        request: Request,
        task_id: int,
        csrf: VerifiedToken,
        name: Annotated[str, Form()] = "",
        source_url: Annotated[str, Form()] = "",
        source_cred_id: Annotated[str, Form()] = "",
        interval_minutes: Annotated[str, Form()] = "60",
        enabled: Annotated[str | None, Form()] = None,
    ):
        store = _store(request)
        task = _require_task(store, task_id)
        values = _task_values(
            name, source_url, source_cred_id, interval_minutes, _form_bool(enabled)
        )
        try:
            fields = _parse_task(values)
            if fields["interval_minutes"] != task["interval_minutes"]:
                fields["next_run_at"] = None
            store.update_task(task_id, **fields)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _task_page(
                request,
                csrf,
                task_id,
                400,
                error=_db_error(exc),
                task_values=values,
                edit_open=True,
            )
        return _redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/run")
    def task_run(request: Request, task_id: int, _csrf: VerifiedToken):
        _require_task(_store(request), task_id)
        request.app.state.scheduler.trigger(task_id)
        return _redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/toggle")
    async def task_toggle(request: Request, task_id: int, _csrf: VerifiedToken):
        store = _store(request)
        task = _require_task(store, task_id)
        form = await request.form()
        enabled = (
            not bool(task["enabled"])
            if form.get("enabled") is None
            else _form_bool(form.get("enabled"))
        )
        store.update_task(task_id, enabled=enabled)
        return _redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/detailed")
    async def task_detailed(request: Request, task_id: int, _csrf: VerifiedToken):
        store = _store(request)
        task = _require_task(store, task_id)
        form = await request.form()
        enabled = (
            not bool(task["detailed_logging"])
            if form.get("enabled") is None
            else _form_bool(form.get("enabled"))
        )
        store.update_task(task_id, detailed_logging=enabled)
        return _redirect(f"/tasks/{task_id}")

    # 平台

    @app.post("/instances")
    def platform_create(
        request: Request,
        csrf: VerifiedToken,
        name: Annotated[str, Form()] = "",
        kind: Annotated[str, Form()] = "gitea",
        base_url: Annotated[str, Form()] = "",
        cred_id: Annotated[str, Form()] = "",
    ):
        values = {"name": name, "kind": kind, "base_url": base_url, "cred_id": cred_id}
        try:
            if kind not in PLATFORM_KINDS:
                raise ValueError(t("error.platform_kind"))
            _store(request).add_instance(
                _text(name, "平台名称"),
                kind,
                _text(base_url, "平台地址"),
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            values["base_url"] = ""
            return _platform_page(request, csrf, 400, error=_db_error(exc), values=values)
        return _redirect("/platforms")

    @app.post("/accesses")
    async def access_create(request: Request, csrf: VerifiedToken):
        form = await request.form()
        store = _store(request)
        try:
            instance_id = _positive_int(str(form.get("instance_id", "")), "实例")
            settings = _access_settings(form)
            store.add_access(instance_id, _text(str(form.get("name", "")), "接入名称"), **settings)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _platform_page(
                request,
                csrf,
                400,
                error=_redact(_db_error(exc), store.secret_values()),
            )
        return _redirect("/platforms")

    @app.post("/instances/{instance_id}/delete")
    def instance_delete(request: Request, instance_id: int, csrf: VerifiedToken):
        store = _store(request)
        try:
            store.delete_instance(instance_id)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _platform_page(
                request,
                csrf,
                400,
                error=_redact(_db_error(exc), store.secret_values()),
            )
        return _redirect("/platforms")

    @app.post("/accesses/{access_id}/preview")
    async def access_preview(request: Request, access_id: int, csrf: VerifiedToken):
        store = _store(request)
        access = store.get_access(access_id)
        if access is None:
            raise HTTPException(
                404,
                translate(
                    "error.access_missing", language_for_cookie(request.cookies.get(LANG_COOKIE))
                ),
            )
        form = await request.form()
        try:
            draft = _access_settings(form, access)
            store.validate_access_fields(access_id, draft)
            draft["name"] = str(form.get("name", access["name"]))
            result = preview(store, access_id, draft)
        except (PlatformError, ValueError, sqlite3.IntegrityError) as exc:
            detail = _redact(_db_error(exc), store.secret_values())
            return _platform_page(
                request,
                csrf,
                400,
                error=translate(
                    "error.discovery_failed",
                    language_for_cookie(request.cookies.get(LANG_COOKIE)),
                    detail=detail,
                ),
            )
        return _platform_page(
            request,
            csrf,
            discovery=_discovery_context(result),
            preview_access_id=access_id,
            draft_settings=draft,
        )

    @app.post("/accesses/{access_id}")
    async def access_save_apply(request: Request, access_id: int, csrf: VerifiedToken):
        store = _store(request)
        access = store.get_access(access_id)
        if access is None:
            raise HTTPException(
                404,
                translate(
                    "error.access_missing", language_for_cookie(request.cookies.get(LANG_COOKIE))
                ),
            )
        form = await request.form()
        try:
            fields = _access_settings(form, access)
            if form.get("name") is not None:
                fields["name"] = _text(str(form.get("name")), "接入名称")
            store.update_access(access_id, **fields)
            request.app.state.scheduler.trigger_discovery(access_id)
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _platform_page(
                request,
                csrf,
                400,
                error=_redact(_db_error(exc), store.secret_values()),
            )
        return _redirect("/platforms")

    @app.post("/accesses/{access_id}/delete")
    def access_delete(request: Request, access_id: int, csrf: VerifiedToken):
        _store(request).delete_access(access_id)
        return _redirect("/platforms")

    @app.post("/accesses/{access_id}/discover")
    def access_discover(request: Request, access_id: int, csrf: VerifiedToken):
        store = _store(request)
        if store.get_access(access_id) is None:
            raise HTTPException(
                404,
                translate(
                    "error.access_missing", language_for_cookie(request.cookies.get(LANG_COOKIE))
                ),
            )
        store.get_access(access_id)
        request.app.state.scheduler.trigger_discovery(access_id)
        return _redirect("/platforms")

    # 目标
    @app.post("/tasks/{task_id}/targets")
    def target_create(
        request: Request,
        task_id: int,
        csrf: VerifiedToken,
        name: Annotated[str, Form()] = "",
        main_url: Annotated[str, Form()] = "",
        history_url: Annotated[str, Form()] = "",
        cred_id: Annotated[str, Form()] = "",
    ):
        _require_task(_store(request), task_id)
        values = {
            "name": name,
            "main_url": main_url,
            "history_url": history_url,
            "cred_id": cred_id,
        }
        try:
            _store(request).add_target(
                task_id,
                _text(name, "目标名称"),
                main_url.strip(),
                history_url.strip(),
                _cred_id(cred_id),
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _task_page(
                request, csrf, task_id, 400, error=_db_error(exc), target_values=values
            )
        return _redirect(f"/tasks/{task_id}")

    @app.post("/tasks/{task_id}/targets/provision")
    def target_provision(
        request: Request,
        task_id: int,
        csrf: VerifiedToken,
        access_id: Annotated[str, Form()] = "",
        owner: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
    ):
        store = _store(request)
        _require_task(store, task_id)
        values = {"access_id": access_id, "owner": owner, "name": name}
        try:
            selected_access = _access_id(access_id)
            if store.get_access(selected_access) is None:
                raise ValueError(t("error.access_missing"))
            provision_target(
                store,
                request.app.state.data_dir,
                task_id,
                selected_access,
                _text(owner, "所有者"),
                _text(name, "目标名称"),
            )
        except (ArchiveBusy, GitError, PlatformError, ValueError) as exc:
            detail = _redact(str(exc), store.secret_values())
            return _task_page(
                request,
                csrf,
                task_id,
                400,
                error=t("error.provision_failed", detail=detail),
                provision_values=values,
            )
        return _redirect(f"/tasks/{task_id}")

    @app.post("/targets/{target_id}/retry")
    def target_retry(request: Request, target_id: int, csrf: VerifiedToken):
        store = _store(request)
        target = _require_target(store, target_id)
        try:
            retry_target(store, request.app.state.data_dir, target_id)
        except (ArchiveBusy, GitError, PlatformError, ValueError) as exc:
            detail = _redact(str(exc), store.secret_values())
            return _task_page(request, csrf, target["task_id"], 400, error=detail)
        return _redirect(f"/tasks/{target['task_id']}")

    @app.post("/targets/{target_id}/audit")
    def target_audit(request: Request, target_id: int, csrf: VerifiedToken):
        store = _store(request)
        target = _require_target(store, target_id)
        try:
            audit_target(store, request.app.state.data_dir, target_id)
            if not store.get_target(target_id).get("last_audit_at"):
                store._exec(
                    "UPDATE targets SET last_audit_at=? WHERE id=?",
                    (datetime.now(UTC).isoformat(timespec="seconds"), target_id),
                )
        except (ArchiveBusy, GitError, PlatformError, ValueError) as exc:
            detail = _redact(str(exc), store.secret_values())
            return _task_page(request, csrf, target["task_id"], 400, error=detail)
        return _redirect(f"/tasks/{target['task_id']}")

    @app.post("/targets/{target_id}/bind")
    def target_bind(request: Request, target_id: int, csrf: VerifiedToken):
        store = _store(request)
        target = _require_target(store, target_id)
        try:
            bind_target(store, request.app.state.data_dir, target_id)
        except TargetNotEmpty as exc:
            message = t(
                "error.target_not_empty", url=_redact(str(exc.args[0]), store.secret_values())
            )
        except TargetMismatch:
            message = t("error.binding_mismatch")
        except ArchiveBusy:
            message = t("error.archive_busy")
        except GitError as exc:
            detail = _redact(exc.stderr.strip()[:300], store.secret_values())
            message = t("error.bind_failed", detail=detail)
        except ValueError as exc:
            message = t("error.bind_failed", detail=_db_error(exc))
        else:
            return _redirect(f"/tasks/{target['task_id']}")
        return _task_page(request, csrf, target["task_id"], 400, error=message)

    @app.post("/targets/{target_id}/edit")
    def target_edit(
        request: Request,
        target_id: int,
        csrf: VerifiedToken,
        name: Annotated[str, Form()] = "",
        main_url: Annotated[str, Form()] = "",
        history_url: Annotated[str, Form()] = "",
        cred_id: Annotated[str, Form()] = "",
    ):
        store = _store(request)
        target = _require_target(store, target_id)
        values = {
            "id": target_id,
            "name": name,
            "main_url": main_url,
            "history_url": history_url,
            "cred_id": cred_id,
        }
        try:
            cred = _cred_id(cred_id)
            if cred is not None and store.get_cred(cred) is None:
                raise ValueError(t("error.credential_invalid"))
            main_url, history_url = main_url.strip(), history_url.strip()
            store.update_target(
                target_id,
                name=_text(name, "目标名称"),
                main_url=main_url,
                history_url=history_url,
                cred_id=cred,
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _task_page(
                request,
                csrf,
                target["task_id"],
                400,
                error=_redact(_db_error(exc), store.secret_values()),
                target_edit=values,
            )
        if (main_url, history_url) != (target["main_url"], target["history_url"]):
            # 旧地址的推送记录不对应新远端，写库成功后再清，避免失败的编辑损坏原目标的记录。
            try:
                forget_target(store, request.app.state.data_dir, target_id)
            except ArchiveBusy:
                store.update_target(
                    target_id,
                    name=target["name"],
                    main_url=target["main_url"],
                    history_url=target["history_url"],
                    cred_id=target["cred_id"],
                )
                store.set_bound(target_id, bool(target["bound"]))
                return _task_page(
                    request, csrf, target["task_id"], 409, error=t("error.archive_busy")
                )
        return _redirect(f"/tasks/{target['task_id']}")

    @app.post("/targets/{target_id}/delete")
    def target_delete(request: Request, target_id: int, csrf: VerifiedToken):
        store = _store(request)
        target = _require_target(store, target_id)
        try:
            # 目标 ID 可能被复用，先清掉推送记录，避免新目标继承旧远端的状态。
            forget_target(store, request.app.state.data_dir, target_id)
        except ArchiveBusy:
            return _task_page(request, csrf, target["task_id"], 409, error=t("error.archive_busy"))
        store.delete_target(target_id)
        return _redirect(f"/tasks/{target['task_id']}")

    # 恢复包
    @app.get("/tasks/{task_id}/events/{event_id}/bundle")
    def event_bundle(request: Request, task_id: int, event_id: int, _csrf: CsrfToken):
        store = _store(request)
        task = _require_task(store, task_id)
        event = _find_event(store, task_id, event_id)
        if event is None or not event["preserved_ref"]:
            raise HTTPException(404, t("error.no_bundle"))
        data_dir: Path = request.app.state.data_dir
        work = data_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="bundle-", dir=work))
        try:
            out = tmp / "restore.bundle"
            run(
                archive_path(data_dir, task),
                "bundle",
                "create",
                str(out),
                event["preserved_ref"],
            )
        except GitError:
            shutil.rmtree(tmp, ignore_errors=True)
            log.exception("生成恢复包失败: 任务 %s 事件 %s", task_id, event_id)
            raise HTTPException(500, t("error.bundle_failed")) from None
        filename = f"{re.sub(r'[^A-Za-z0-9_-]', '_', task['name'])}-{event_id}.bundle"
        return FileResponse(
            out,
            media_type="application/octet-stream",
            filename=filename,
            background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
        )

    # 凭据

    @app.post("/creds")
    def cred_create(
        request: Request,
        csrf: VerifiedToken,
        name: Annotated[str, Form()] = "",
        kind: Annotated[str, Form()] = "https",
        username: Annotated[str, Form()] = "",
        secret: Annotated[str, Form()] = "",
        private_key: Annotated[str, Form()] = "",
        known_hosts: Annotated[str, Form()] = "",
    ):
        values = {"name": name, "kind": kind, "username": username}
        private_key = private_key.replace("\r\n", "\n")
        known_hosts = known_hosts.replace("\r\n", "\n")
        try:
            if kind not in CRED_KINDS:
                raise ValueError(t("error.credential_kind"))
            if kind == "ssh" and not private_key.strip():
                raise ValueError(t("error.ssh_key"))
            _store(request).add_cred(
                _text(name, "凭据名称"),
                kind,
                username.strip() or None,
                secret or None,
                private_key if private_key.strip() else None,
                known_hosts if known_hosts.strip() else None,
            )
        except (ValueError, sqlite3.IntegrityError) as exc:
            return _creds_page(request, csrf, 400, error=_db_error(exc), values=values)
        return _redirect("/creds")

    @app.post("/creds/{cred_id}/edit")
    def cred_edit(
        request: Request,
        cred_id: int,
        csrf: VerifiedToken,
        name: Annotated[str, Form()] = "",
        kind: Annotated[str | None, Form()] = None,
        username: Annotated[str, Form()] = "",
        secret: Annotated[str, Form()] = "",
        private_key: Annotated[str, Form()] = "",
        known_hosts: Annotated[str | None, Form()] = None,
    ):
        store = _store(request)
        credential = store.get_cred(cred_id)
        if credential is None:
            raise HTTPException(404, t("error.credential_invalid"))
        private_key = private_key.replace("\r\n", "\n")
        # 更新后数据库只返回新秘密，错误页仍需同时遮盖旧值和本次提交值。
        secrets_ = [*store.secret_values(), secret, private_key]
        try:
            if kind is not None and kind != credential["kind"]:
                raise ValueError(t("error.credential_type_immutable"))
            fields = {"name": _text(name, "凭据名称"), "username": username.strip() or None}
            if credential["kind"] == "https" and secret:
                fields["secret"] = secret
            if credential["kind"] == "ssh":
                if private_key.strip():
                    fields["private_key"] = private_key
                if known_hosts is not None:
                    fields["known_hosts"] = known_hosts.replace("\r\n", "\n")
            store.update_cred(cred_id, **fields)
        except (ValueError, sqlite3.IntegrityError) as exc:
            values = {"name": name, "username": username}
            if known_hosts is not None:
                values["known_hosts"] = known_hosts
            return _creds_page(
                request,
                csrf,
                400,
                error=_redact(_db_error(exc), secrets_),
                values={key: _redact(value, secrets_) for key, value in values.items()},
                edit_id=cred_id,
            )
        return _redirect("/creds")

    @app.post("/creds/{cred_id}/delete")
    def cred_delete(request: Request, cred_id: int, csrf: VerifiedToken):
        try:
            _store(request).delete_cred(cred_id)
        except ValueError as exc:
            return _creds_page(request, csrf, 400, error=_db_error(exc))
        return _redirect("/creds")

    from gitwarden import ui_logs, ui_overview, ui_platforms, ui_task

    ui_overview.register(app)
    ui_task.register(app)
    ui_logs.register(app)
    ui_platforms.register(app)
    return app
