"""按实例和平台仓库稳定 ID 发现来源。"""

from __future__ import annotations

import fnmatch
import json
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from gitwarden.creds import check_url
from gitwarden.db import Store, now
from gitwarden.i18n import msg
from gitwarden.platform import client_from_store


@dataclass
class DiscoveryResult:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    not_visible: list[str] = field(default_factory=list)
    other_access: list[str] = field(default_factory=list)
    items: list[dict[str, str]] = field(default_factory=list)

    def add_item(self, category: str, name: str, reason: str) -> None:
        self.items.append({"category": category, "name": name, "reason": reason})


_ACCESS_SETTINGS = {
    "scope_mode",
    "owners",
    "include_forks",
    "include_archived",
    "include_public",
    "include_private",
    "rule_mode",
    "patterns",
}


def _merged_access(access: dict[str, Any], draft_settings: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(access)
    for key, value in (draft_settings or {}).items():
        if key in _ACCESS_SETTINGS:
            merged[key] = value
    return merged


def _repo_fields(repo: dict[str, Any]) -> tuple[Any, str, str]:
    if not isinstance(repo, dict):
        raise ValueError("平台仓库响应不是对象")
    repo_id = repo.get("id")
    full_name = repo.get("full_name")
    clone_url = repo.get("clone_url")
    if repo_id is None or not isinstance(full_name, str) or not full_name:
        raise ValueError("平台仓库响应缺少 id 或 full_name")
    if not isinstance(clone_url, str) or not clone_url:
        raise ValueError(f"平台仓库 {full_name} 缺少 clone_url")
    check_url(clone_url)
    return repo_id, full_name, clone_url


def _rule_match(full_name: str, access: dict[str, Any]) -> str | None:
    name = full_name.lower()
    patterns = access.get("patterns") or []
    matched = next(
        (str(pattern) for pattern in patterns if fnmatch.fnmatchcase(name, str(pattern).lower())),
        None,
    )
    mode = access.get("rule_mode", "exclude")
    if mode == "exclude" and matched is not None:
        return f"命中排除规则 {matched}"
    if mode == "include" and matched is None:
        return "未命中任何包含规则"
    return None


def _filter_reason(repo: dict[str, Any], access: dict[str, Any]) -> str | None:
    if repo.get("mirror"):
        return "平台仓库是镜像"
    if repo.get("fork") and not access.get("include_forks"):
        return "平台仓库是 fork"
    if repo.get("archived") and not access.get("include_archived"):
        return "平台仓库已归档"
    if repo.get("private"):
        if not access.get("include_private"):
            return "私有仓库未包含"
    elif not access.get("include_public"):
        return "公开仓库未包含"
    return _rule_match(repo["full_name"], access)


def _list_repos(client: Any, access: dict[str, Any]) -> list[dict[str, Any]]:
    if access.get("scope_mode") == "owners":
        return client.list_repos_for_owners(access.get("owners") or [])
    return client.list_all_repos()


def _plan(
    tasks: list[dict[str, Any]],
    repos: list[dict[str, Any]],
    access: dict[str, Any],
    instance: dict[str, Any],
    access_id: int,
) -> tuple[DiscoveryResult, list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    result = DiscoveryResult()
    actions: list[tuple[str, dict[str, Any]]] = []
    by_id = {
        task["platform_repo_id"]: task
        for task in tasks
        if task["instance_id"] == instance["id"] and task["platform_repo_id"] is not None
    }
    known_urls = {task["source_url"] for task in tasks}
    seen_ids: set[Any] = set()
    for repo in repos:
        repo_id, full_name, clone_url = _repo_fields(repo)
        if repo_id in seen_ids:
            continue
        seen_ids.add(repo_id)
        task = by_id.get(repo_id)
        if repo.get("mirror"):
            result.skipped.append(full_name)
            result.add_item("Skip", full_name, "平台仓库是镜像")
            continue
        if task is not None and task["access_id"] != access_id:
            result.other_access.append(full_name)
            result.add_item("Other access", full_name, "已由其他接入或手工任务管理")
            continue

        reason = _filter_reason(repo, access)
        if reason is not None:
            if task is None:
                result.skipped.append(full_name)
                result.add_item("Skip", full_name, reason)
            else:
                result.disabled.append(full_name)
                actions.append(
                    (
                        "update",
                        {"id": task["id"], "enabled": 0, "managed_state": "excluded"},
                    )
                )
                result.add_item("Disable", full_name, reason)
            continue

        if task is not None:
            fields: dict[str, Any] = {"managed_state": "active"}
            if task["managed_state"] == "excluded":
                # 停用来自规则，规则不再排除时按接入的“新仓库直接启用”设置恢复。
                fields["enabled"] = int(bool(access["auto_enable"]))
            if task["source_url"] != clone_url:
                fields["source_url"] = clone_url
                result.renamed.append(full_name)
                result.add_item("Keep", full_name, "按平台仓库 ID 更新仓库地址")
            else:
                result.existing.append(full_name)
                result.add_item("Keep", full_name, "任务已存在")
            actions.append(("update", {"id": task["id"], **fields}))
            continue

        if clone_url in known_urls:
            result.conflicts.append(full_name)
            result.add_item("Skip", full_name, "已有任务使用相同来源地址")
            continue

        result.created.append(full_name)
        known_urls.add(clone_url)
        actions.append(
            ("insert", {"repo_id": repo_id, "full_name": full_name, "clone_url": clone_url})
        )
        result.add_item("Add", full_name, "创建发现任务")

    for task in tasks:
        if (
            task["instance_id"] != instance["id"]
            or task["access_id"] != access_id
            or task["platform_repo_id"] is None
            or task["platform_repo_id"] in seen_ids
        ):
            continue
        if task["managed_state"] == "source_gone":
            result.not_visible.append(task["name"])
            result.add_item("Keep", task["name"], "本轮仍不可见，保持 source_gone")
            continue
        result.not_visible.append(task["name"])
        actions.append(("update", {"id": task["id"], "managed_state": "not_visible"}))
        result.add_item("Keep", task["name"], "完整发现中未看到，任务保持启用")

    summary = {
        "complete": True,
        "seen_repo_ids": sorted(seen_ids, key=str),
        "created": list(result.created),
        "existing": list(result.existing),
        "renamed": list(result.renamed),
        "conflicts": list(result.conflicts),
        "skipped": list(result.skipped),
        "disabled": list(result.disabled),
        "not_visible": list(result.not_visible),
        "other_access": list(result.other_access),
        "items": list(result.items),
    }
    return result, actions, summary


def _apply_actions(
    conn: Any,
    actions: list[tuple[str, dict[str, Any]]],
    access: dict[str, Any],
    instance: dict[str, Any],
) -> None:
    for kind, fields in actions:
        if kind == "update":
            values = {key: value for key, value in fields.items() if key != "id"}
            if not values:
                continue
            sets = ", ".join(f"{key}=?" for key in values)
            conn.execute(
                f"UPDATE tasks SET {sets} WHERE id=?",
                (*values.values(), fields["id"]),
            )
            continue
        base_name = f"{instance['name']}/{fields['full_name']}"
        name = base_name
        suffix = 1
        while conn.execute("SELECT 1 FROM tasks WHERE name=?", (name,)).fetchone():
            name = f"{base_name}#{fields['repo_id']}" + (f"-{suffix}" if suffix > 1 else "")
            suffix += 1
        conn.execute(
            "INSERT INTO tasks(name,source_url,source_cred_id,interval_minutes,enabled,"
            "archive_uuid,instance_id,access_id,platform_repo_id,managed_state,detailed_logging,"
            "recent_durations) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                fields["clone_url"],
                access["cred_id"],
                access["default_sync_interval_minutes"],
                int(bool(access["auto_enable"])),
                uuid.uuid4().hex,
                instance["id"],
                access["id"],
                fields["repo_id"],
                "active",
                int(bool(access["detailed_default"])),
                "[]",
            ),
        )


def _tasks_in_connection(conn: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY id")]


def discover(store: Store, access_id: int) -> DiscoveryResult:
    access = store.get_access(access_id)
    if not access:
        raise ValueError(msg("error.access_missing"))
    instance = store.get_instance(access["instance_id"])
    if not instance:
        raise ValueError(msg("error.instance_missing"))
    client = client_from_store(store, access_id)
    # 只有完整枚举成功后才打开写事务；平台错误因此不会留下摘要或半轮状态。
    repos = _list_repos(client, access)
    with closing(store._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            tasks = _tasks_in_connection(conn)
            result, actions, summary = _plan(tasks, repos, access, instance, access_id)
            _apply_actions(conn, actions, access, instance)
            conn.execute(
                "UPDATE accesses SET last_discovery_at=?, last_discovery_summary=? WHERE id=?",
                (now(), json.dumps(summary, ensure_ascii=False, separators=(",", ":")), access_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return result


def preview(
    store: Store, access_id: int, draft_settings: dict[str, Any] | None = None
) -> DiscoveryResult:
    access = store.get_access(access_id)
    if not access:
        raise ValueError(msg("error.access_missing"))
    instance = store.get_instance(access["instance_id"])
    if not instance:
        raise ValueError(msg("error.instance_missing"))
    effective = _merged_access(access, draft_settings)
    client = client_from_store(store, access_id)
    repos = _list_repos(client, effective)
    result, _, _ = _plan(store.list_tasks(), repos, effective, instance, access_id)
    return result
