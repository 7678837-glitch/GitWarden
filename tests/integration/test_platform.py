import re
import uuid

import pytest

from gitwarden.creds import git_env
from gitwarden.discovery import discover
from gitwarden.git import run
from gitwarden.i18n import render
from gitwarden.platform import PlatformClient, PlatformError
from gitwarden.provision import provision_target
from gitwarden.runner import run_task
from tests.conftest import commit, make_source
from tests.integration.conftest import api, token_for


def unique(prefix):
    return prefix + "-" + uuid.uuid4().hex[:10]


def test_admin_discovers_five_private_repos_in_pages_of_two(forge):
    before = {repo["id"] for repo in forge.client.list_all_repos()}
    repos = [forge.alice.create_repo("alice", unique("private")) for _ in range(5)]
    listing = forge.client.list_all_repos()
    assert {repo["id"] for repo in listing} == before | {repo["id"] for repo in repos}
    new_rows = [repo for repo in listing if repo["id"] not in before]
    assert len(new_rows) == 5
    assert all(repo["private"] and repo["owner"]["login"] == "alice" for repo in new_rows)
    result = discover(forge.store, forge.access_id)
    assert {repo["full_name"] for repo in repos} <= set(result.created)
    assert all(task["enabled"] for task in forge.store.list_tasks())
    assert all(task["source_cred_id"] for task in forge.store.list_tasks())


def test_discovery_preserves_identity_on_rename_and_reports_recreated_url(forge):
    name = unique("identity")
    repo = forge.alice.create_repo("alice", name)
    discover(forge.store, forge.access_id)
    task = next(t for t in forge.store.list_tasks() if t["platform_repo_id"] == repo["id"])
    again = discover(forge.store, forge.access_id)
    assert not again.created
    renamed = unique("renamed")
    forge.api("PATCH", f"/repos/alice/{name}", {"name": renamed})
    result = discover(forge.store, forge.access_id)
    assert f"alice/{renamed}" in result.renamed
    assert (
        forge.store.get_task(task["id"])["source_url"]
        == forge.client.get_repo("alice", renamed)["clone_url"]
    )
    forge.api("DELETE", f"/repos/alice/{renamed}")
    new = forge.alice.create_repo("alice", renamed)
    assert new["id"] != repo["id"]
    result = discover(forge.store, forge.access_id)
    assert f"alice/{renamed}" in result.conflicts
    assert forge.store.get_task(task["id"])["platform_repo_id"] == repo["id"]


def test_pull_mirror_is_skipped(forge):
    source = unique("mirror-source")
    forge.api("POST", "/user/repos", {"name": source, "private": False, "auto_init": True})
    name = unique("pull-mirror")
    repo = forge.api(
        "POST",
        "/repos/migrate",
        {
            "clone_addr": f"http://127.0.0.1:3000/gwadmin/{source}.git",
            "repo_name": name,
            "repo_owner": "gwadmin",
            "service": "git",
            "mirror": True,
            "private": True,
        },
    )
    assert repo["mirror"]
    result = discover(forge.store, forge.access_id)
    assert f"gwadmin/{name}" in result.skipped
    assert not any(t["platform_repo_id"] == repo["id"] for t in forge.store.list_tasks())


def test_provision_hardens_repos_and_rejects_non_whitelisted_writer(forge, tmp_path):
    task = forge.store.add_task("source", str(tmp_path / "source.git"))
    name = unique("protected")
    target_id = provision_target(forge.store, forge.data, task, forge.access_id, "backups", name)
    target = forge.store.get_target(target_id)
    assert target["bound"]
    assert "webhook" in target["last_detail"]
    assert forge.token not in target["last_detail"]
    for repo_name in (name, name + "-history"):
        repo = forge.client.get_repo("backups", repo_name)
        assert repo["private"] is True and repo["mirror"] is False
        for field in PlatformClient._HARDEN_FIELDS:
            assert not repo.get(field), field
    rules = forge.api("GET", f"/repos/backups/{name}-history/tag_protections")
    assert any(
        r["name_pattern"] == "gw/*" and r["whitelist_usernames"] == ["gwadmin"] for r in rules
    )
    forge.api("PUT", f"/repos/backups/{name}-history/collaborators/alice", {"permission": "write"})
    source = make_source(tmp_path / "writer")
    commit(source, "writer")
    cred = {"kind": "https", "username": "alice", "secret": forge.alice_token}
    with git_env(cred, tmp_path / "auth") as env:
        allowed = run(
            source,
            "push",
            target["history_url"],
            "HEAD:refs/tags/unprotected",
            env=env,
            check=False,
        )
        assert allowed.returncode == 0, allowed.stderr
        denied = run(
            source, "push", target["history_url"], "HEAD:refs/tags/gw/x", env=env, check=False
        )
    assert denied.returncode != 0
    assert "protected" in denied.stderr.lower(), denied.stderr


@pytest.mark.parametrize("existing_history", [False, True])
def test_existing_target_is_not_adopted_or_modified(forge, tmp_path, existing_history):
    task = forge.store.add_task("source", str(tmp_path / "source.git"))
    name = unique("existing")
    existing = name + "-history" if existing_history else name
    before = forge.client.create_repo("gwadmin", existing, private=False)
    with pytest.raises(PlatformError, match="provision.exists"):
        provision_target(forge.store, forge.data, task, forge.access_id, "gwadmin", name)
    after = forge.client.get_repo("gwadmin", existing)
    assert after["id"] == before["id"] and after["private"] is False
    assert after["has_actions"] == before["has_actions"]
    assert not forge.store.list_targets()
    absent = name if existing_history else name + "-history"
    with pytest.raises(PlatformError) as error:
        forge.client.get_repo("gwadmin", absent)
    assert error.value.status == 404


def test_discover_enable_provision_and_sync_preserves_force_pushed_history(forge, tmp_path):
    name = unique("end-to-end")
    repo = forge.alice.create_repo("alice", name)
    source = make_source(tmp_path / "source")
    commit(source, "one")
    old = commit(source, "two")
    cred = {"kind": "https", "username": "alice", "secret": forge.alice_token}
    with git_env(cred, tmp_path / "auth") as env:
        run(source, "push", repo["clone_url"], "main", env=env)
    discover(forge.store, forge.access_id)
    task = next(t for t in forge.store.list_tasks() if t["platform_repo_id"] == repo["id"])
    assert task["enabled"]
    forge.store.update_task(task["id"], enabled=True)
    target_id = provision_target(
        forge.store, forge.data, task["id"], forge.access_id, "backups", name
    )
    assert forge.store.get_target(target_id)["last_status"] is None
    run_task(forge.store, forge.data, task["id"])
    assert forge.store.get_target(target_id)["last_status"] == "ok"
    run(source, "reset", "--hard", "HEAD~1")
    new = commit(source, "replacement")
    with git_env(cred, tmp_path / "auth") as env:
        run(source, "push", "--force", repo["clone_url"], "main", env=env)
    run_task(forge.store, forge.data, task["id"])
    assert forge.store.get_target(target_id)["last_status"] == "ok"
    assert forge.store.get_task(task["id"])["last_status"] == "ok"
    assert forge.api("GET", f"/repos/backups/{name}/branches/main")["commit"]["id"] == new
    tags = forge.api("GET", f"/repos/backups/{name}-history/tags")
    preserved = [tag for tag in tags if re.fullmatch(r"gw/[0-9A-HJKMNP-TV-Z]{26}", tag["name"])]
    assert len(preserved) == 1
    assert preserved[0]["commit"]["sha"] == old


def test_whoami_requires_user_scope_and_http_errors_hide_token(forge):
    token = token_for(forge.kind, "gwadmin", "read:admin,write:repository,write:organization")
    client = PlatformClient(forge.url, token)
    with pytest.raises(PlatformError) as error:
        client.whoami()
    assert error.value.status == 403
    shown = render(str(error.value), "en")
    assert "GET /api/v1/user" in shown
    assert "read:user" in shown
    assert token not in shown


def test_lists_all_webhook_scopes_without_saving_secrets(forge, tmp_path):
    name = unique("hooks")
    forge.client.create_repo("backups", name)
    payload = {
        "type": "gitea",
        "active": False,
        "config": {
            "url": "http://127.0.0.1:9/hook",
            "content_type": "json",
            "secret": "hook-secret",
        },
        "events": ["push"],
    }
    repo_hook = forge.api("POST", f"/repos/backups/{name}/hooks", payload)
    org_hook = forge.api("POST", "/orgs/backups/hooks", payload)
    # 超过 page_size，验证不能只返回第一页的 webhook。
    setup_token = token_for(forge.kind, "gwadmin", "write:admin")
    global_hooks = [api(forge.url, setup_token, "POST", "/admin/hooks", payload) for _ in range(3)]
    hooks = forge.client.list_hooks("backups", name)
    assert repo_hook["id"] in {h["id"] for h in hooks["repository"]}
    assert org_hook["id"] in {h["id"] for h in hooks["organization"]}
    if forge.kind == "gitea":
        assert {h["id"] for h in global_hooks} <= {h["id"] for h in hooks["system"]}
    else:
        # Forgejo 16 的全局列表不包含这些按 ID 可读的 hook，不能据此断言没有 hook。
        assert hooks["system"] == []
        assert (
            forge.api("GET", f"/admin/hooks/{global_hooks[0]['id']}")["id"] == global_hooks[0]["id"]
        )
    token = token_for(forge.kind, "gwadmin", "write:repository,write:organization,write:user")
    restricted = PlatformClient(forge.url, token).list_hooks("backups", name)
    assert restricted["system"] == []
    task = forge.store.add_task("hooks", str(tmp_path / "source.git"))
    target = provision_target(
        forge.store, forge.data, task, forge.access_id, "backups", unique("with-hooks")
    )
    detail = forge.store.get_target(target)["last_detail"]
    assert '"organization"' in detail and '"system"' in detail
    assert "hook-secret" not in detail and forge.token not in detail
    if forge.kind == "forgejo":
        assert "Check default webhooks" in render(detail, "en")
