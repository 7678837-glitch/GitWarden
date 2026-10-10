"""创建并加固一对全新目标仓；失败时保留已创建的仓库供管理员处理。"""

import json
from pathlib import Path

from gitwarden import runner
from gitwarden.db import Store
from gitwarden.i18n import msg
from gitwarden.platform import PlatformError, client_from_store


def provision_target(
    store: Store, data_dir: Path, task_id: int, access_id: int, owner: str, name: str
) -> int:
    if store.get_task(task_id) is None:
        raise ValueError(msg("error.task_missing"))
    owner, name = owner.strip(), name.strip()
    if not owner or not name:
        raise ValueError(msg("provision.owner_name_required"))
    client = client_from_store(store, access_id)
    access = store.get_access(access_id)
    if not access:
        raise ValueError(msg("error.access_missing"))
    instance = store.get_instance(access["instance_id"])
    if not instance:
        raise ValueError(msg("error.instance_missing"))
    names = (name, f"{name}-history")
    for repo_name in names:
        try:
            client.get_repo(owner, repo_name)
        except PlatformError as exc:
            if exc.status != 404:
                raise
        else:
            raise PlatformError(msg("provision.exists", repo=f"{owner}/{repo_name}"))

    created = []
    try:
        username = client.whoami()["login"]
        for repo_name in names:
            client.create_repo(owner, repo_name, private=True)
            created.append(f"{owner}/{repo_name}")
            client.harden(owner, repo_name)
        client.protect_tags(owner, names[1], "gw/*", username)
        repos = [client.get_repo(owner, repo_name) for repo_name in names]
        for repo_name, repo in zip(names, repos, strict=True):
            if repo["private"] is not True or repo["mirror"] is not False:
                raise PlatformError(msg("provision.check_failed", repo=f"{owner}/{repo_name}"))
        # webhook 配置可能含令牌；只保存管理员确认所需的身份、类型和启用状态。
        hooks = {
            repo_name: {
                scope: [{key: hook.get(key) for key in ("id", "type", "active")} for hook in rows]
                for scope, rows in client.list_hooks(owner, repo_name).items()
            }
            for repo_name in names
        }
        target_id = store.add_target(
            task_id,
            f"{instance['name']}/{owner}/{name}",
            repos[0]["clone_url"],
            repos[1]["clone_url"],
            access["cred_id"],
        )
        runner.bind_target(store, data_dir, target_id)
        notice = msg("provision.webhook_notice")
        if instance["kind"] == "forgejo":
            notice = msg("provision.webhook_notice_forgejo") + notice
        store.set_target_status(target_id, None, notice + json.dumps(hooks, ensure_ascii=False))
        return target_id
    except Exception as exc:
        detail = str(exc)
        for secret in sorted(set(store.secret_values()), key=len, reverse=True):
            detail = detail.replace(secret, "***")
        if created:
            detail += msg("provision.created_left", repos=", ".join(created))
        raise PlatformError(detail, status=getattr(exc, "status", None)) from None
