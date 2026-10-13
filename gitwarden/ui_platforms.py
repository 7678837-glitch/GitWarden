"""Platforms and credentials pages.

The write endpoints remain in :mod:`gitwarden.web`; this module owns the
server-rendered GET pages and their presentation context.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from fastapi import HTTPException, Request

from gitwarden.web import CsrfToken

_CATEGORY_KEYS = {
    "Add": "add",
    "Keep": "keep",
    "Disable": "disable",
    "Skip": "skip",
    "Other access": "other_access",
}
_CATEGORY_ORDER = ("add", "keep", "disable", "skip", "other_access")

_REASON_KEYS = {
    "平台仓库是镜像": "platforms.reason.mirror",
    "平台仓库是 fork": "platforms.reason.fork",
    "平台仓库已归档": "platforms.reason.archived",
    "私有仓库未包含": "platforms.reason.private_disabled",
    "公开仓库未包含": "platforms.reason.public_disabled",
    "未命中任何包含规则": "platforms.reason.include_missing",
    "已由其他接入或手工任务管理": "platforms.reason.other_access",
    "按平台仓库 ID 更新仓库地址": "platforms.reason.renamed",
    "任务已存在": "platforms.reason.existing",
    "已有任务使用相同来源地址": "platforms.reason.same_source",
    "创建发现任务": "platforms.reason.created",
    "本轮仍不可见，保持 source_gone": "platforms.reason.source_gone",
    "完整发现中未看到，任务保持启用": "platforms.reason.not_visible",
    "平台仓库是镜像。": "platforms.reason.mirror",
    "平台仓库是 fork。": "platforms.reason.fork",
    "平台仓库已归档。": "platforms.reason.archived",
    "new on platform": "platforms.reason.created",
    "already backed up": "platforms.reason.existing",
    "managed by another access": "platforms.reason.other_access",
    "managed by admin-all": "platforms.reason.other_access",
    "no matching pattern": "platforms.reason.include_missing",
}
_PATTERN_REASON = re.compile(r"^(?:命中排除规则|matches)\s+(.+)$")


def _web():
    """Load web helpers lazily so this module can be registered by web.py."""

    from gitwarden import web

    return web


def _store(request: Request):
    return request.app.state.store


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, (list, tuple)):
        if not value:
            return default
        value = value[-1]
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes", "enable", "enabled"}


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in re.split(r"[,\n]", str(value)) if item.strip()]


def _lines(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(_text_list(value))


def _selected_id(request: Request, name: str) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 < value < 2**63 else None


def _credential_rows(store) -> list[dict[str, Any]]:
    """Return display-only credential rows and real reference counts."""

    return store._rows(
        "SELECT c.id, c.name, c.kind, c.username, "
        "(SELECT count(*) FROM tasks WHERE source_cred_id=c.id) AS task_count, "
        "(SELECT count(*) FROM targets WHERE cred_id=c.id) AS target_count, "
        "(SELECT count(*) FROM accesses WHERE cred_id=c.id) AS access_count "
        "FROM creds AS c ORDER BY c.id"
    )


def _access_form(access: dict[str, Any] | None, draft: dict[str, Any] | None) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "",
        "cred_id": "",
        "scope_mode": "all",
        "owners_text": "",
        "include_forks": False,
        "include_archived": True,
        "include_public": True,
        "include_private": True,
        "rule_mode": "exclude",
        "patterns_text": "",
        "auto_enable": True,
        "detailed_default": False,
        "default_sync_interval_minutes": "60",
        "discover_interval_minutes": "60",
    }
    if access:
        values.update(access)
    if draft:
        values.update(draft)
    values["cred_id"] = "" if values.get("cred_id") is None else str(values["cred_id"])
    values["owners_text"] = _lines(values.get("owners", []))
    values["patterns_text"] = _lines(values.get("patterns", []))
    for key, default in (
        ("include_forks", False),
        ("include_archived", True),
        ("include_public", True),
        ("include_private", True),
        ("auto_enable", True),
        ("detailed_default", False),
    ):
        values[key] = _bool(values.get(key), default)
    values["scope_mode"] = str(values.get("scope_mode") or "all")
    values["rule_mode"] = str(values.get("rule_mode") or "exclude")
    for key, default in (
        ("default_sync_interval_minutes", 60),
        ("discover_interval_minutes", 60),
    ):
        raw = values.get(key, default)
        values[key] = str(default if raw is None or str(raw).strip() == "" else raw)
    return values


def _reason_context(raw_reason: Any) -> tuple[str, str | None]:
    reason = str(raw_reason or "")
    mapped = _REASON_KEYS.get(reason)
    if mapped:
        return mapped, None
    match = _PATTERN_REASON.match(reason)
    if match:
        return "platforms.reason.pattern", match.group(1)
    if reason.startswith("未命中"):
        return "platforms.reason.include_missing", None
    return "platforms.reason.unknown", reason or None


def _preview_items(discovery: Any) -> list[dict[str, Any]]:
    if not discovery:
        return []
    if isinstance(discovery, dict):
        raw_items = discovery.get("items") or []
    else:
        raw_items = getattr(discovery, "items", []) or []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category") or "Skip")
        category_key = _CATEGORY_KEYS.get(category, "skip")
        reason_key, reason_pattern = _reason_context(raw.get("reason"))
        items.append(
            {
                "name": str(raw.get("name") or ""),
                "category": category,
                "category_key": category_key,
                "reason_key": reason_key,
                "reason_pattern": reason_pattern,
            }
        )
    if items:
        return items

    buckets = (
        ("created", "Add", "platforms.reason.created"),
        ("existing", "Keep", "platforms.reason.existing"),
        ("renamed", "Keep", "platforms.reason.renamed"),
        ("disabled", "Disable", "platforms.reason.excluded"),
        ("skipped", "Skip", "platforms.reason.unknown"),
        ("conflicts", "Skip", "platforms.reason.same_source"),
        ("other_access", "Other access", "platforms.reason.other_access"),
        ("not_visible", "Keep", "platforms.reason.not_visible"),
    )
    for field, category, reason_key in buckets:
        names = (
            discovery.get(field, [])
            if isinstance(discovery, dict)
            else getattr(discovery, field, [])
        )
        for name in names or []:
            items.append(
                {
                    "name": str(name),
                    "category": category,
                    "category_key": _CATEGORY_KEYS[category],
                    "reason_key": reason_key,
                    "reason_pattern": None,
                }
            )
    return items


def _preview_context(discovery: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items = _preview_items(discovery)
    counts = Counter(item["category_key"] for item in items)
    summary = [{"key": key, "count": counts.get(key, 0)} for key in _CATEGORY_ORDER]
    return items, summary


def _instances_context(store, selected_access_id: int | None) -> list[dict[str, Any]]:
    tasks = Counter(task.get("access_id") for task in store.list_tasks())
    all_accesses = store.list_accesses()
    by_instance: dict[int, list[dict[str, Any]]] = {}
    for access in all_accesses:
        row = dict(access)
        row["task_count"] = tasks.get(access["id"], 0)
        row["owners_text"] = ", ".join(access.get("owners") or [])
        row["selected"] = access["id"] == selected_access_id
        by_instance.setdefault(access["instance_id"], []).append(row)
    result = []
    for instance in store.list_instances():
        row = dict(instance)
        row["accesses"] = by_instance.get(instance["id"], [])
        result.append(row)
    return result


def _pick_access(store, request: Request, preview_access_id: int | None) -> dict[str, Any] | None:
    selected = preview_access_id or _selected_id(request, "access_id")
    if selected is not None:
        access = store.get_access(selected)
        if access:
            return access
    instance_id = _selected_id(request, "instance_id")
    if instance_id is not None:
        accesses = store.list_accesses(instance_id)
        if accesses:
            return accesses[0]
    accesses = store.list_accesses()
    return accesses[0] if accesses else None


def _platform_instance_values(values: dict[str, Any] | None) -> dict[str, str]:
    source = values or {}
    return {
        "name": str(source.get("name", "")),
        "kind": str(source.get("kind", "gitea")),
        "base_url": str(source.get("base_url", "")),
    }


def platform_page(
    request: Request,
    csrf: str | None,
    status: int = 200,
    *,
    error: str | None = None,
    values: dict[str, Any] | None = None,
    discovery: Any | None = None,
    preview_access_id: int | None = None,
    draft_settings: dict[str, Any] | None = None,
):
    """Render the platforms page while retaining a preview's unsaved draft."""

    store = _store(request)
    selected_access = _pick_access(store, request, preview_access_id)
    if selected_access:
        selected_access = dict(selected_access)
        credential = store.get_cred(selected_access.get("cred_id"))
        selected_access["cred_name"] = credential.get("name") if credential else None
        selected_access["cred_username"] = credential.get("username") if credential else None
    selected_instance = (
        store.get_instance(selected_access["instance_id"]) if selected_access else None
    )
    access_values = _access_form(selected_access, draft_settings)
    if values and selected_access and "access_name" in values:
        access_values["name"] = str(values["access_name"])
    preview_items, preview_summary = _preview_context(discovery)
    web = _web()
    credentials = [c for c in web._cred_summaries(store) if c["kind"] == "https"]
    page = web._page(
        request,
        "platforms.html",
        csrf,
        status=status,
        error=error,
        instances=_instances_context(store, selected_access["id"] if selected_access else None),
        selected_instance=selected_instance,
        selected_access=selected_access,
        access_values=access_values,
        credentials=credentials,
        min_sync=store.min_sync_minutes,
        min_discovery=store.min_discovery_minutes,
        instance_values=_platform_instance_values(values),
        preview=discovery,
        preview_items=preview_items,
        preview_summary=preview_summary,
        preview_access_id=preview_access_id or (selected_access["id"] if discovery else None),
        last_discovery_summary=(selected_access or {}).get("last_discovery_summary"),
    )
    return page


def creds_page(
    request: Request,
    csrf: str | None,
    status: int = 200,
    *,
    error: str | None = None,
    values: dict[str, Any] | None = None,
    edit_id: int | None = None,
):
    """Render credentials without placing any secret column in the context."""

    query_kind = request.query_params.get("kind", "https")
    form_values = {
        "name": request.query_params.get("name", ""),
        "kind": query_kind if query_kind in {"https", "ssh"} else "https",
        "username": request.query_params.get("username", ""),
        "known_hosts": "",
    }
    edit_id = edit_id or _selected_id(request, "edit")
    if edit_id is not None:
        credential = _store(request)._one(
            "SELECT name, kind, username, known_hosts FROM creds WHERE id=?", (edit_id,)
        )
        if credential is None:
            raise HTTPException(404, _web().t("error.credential_invalid"))
        form_values.update({key: value or "" for key, value in credential.items()})
    if values:
        form_values.update(
            {
                key: str(values[key])
                for key in ("name", "username", "known_hosts", *(() if edit_id else ("kind",)))
                if key in values and values[key] is not None
            }
        )
    web = _web()
    return web._page(
        request,
        "creds.html",
        csrf,
        status=status,
        error=error,
        credentials=_credential_rows(_store(request)),
        values=form_values,
        edit_id=edit_id,
    )


def register(app):
    """Register the two GET page routes; POST handlers stay in ``web.py``."""

    @app.get("/platforms")
    def _platforms(request: Request, csrf: CsrfToken):
        return platform_page(request, csrf)

    @app.get("/creds")
    def _creds(request: Request, csrf: CsrfToken):
        return creds_page(request, csrf)

    return app
