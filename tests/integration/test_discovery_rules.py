import uuid

from gitwarden.creds import git_env
from gitwarden.discovery import discover
from gitwarden.git import run
from gitwarden.runner import archive_path, bind_target, run_task
from tests.conftest import commit, make_source


def unique(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def task_for_repo(store, repo_id):
    return next(task for task in store.list_tasks() if task["platform_repo_id"] == repo_id)


def item_for(result, name):
    return next(item for item in result.items if item["name"] == name)


def test_owners_scope_reads_org_and_user_and_applies_filters(forge):
    org = unique("owners")
    forge.api("POST", "/orgs", {"username": org, "visibility": "private"})
    org_private = forge.client.create_repo(org, unique("private"), private=True)
    org_public = forge.client.create_repo(org, unique("public"), private=False)
    org_archived = forge.client.create_repo(org, unique("archived"), private=True)
    forge.api("PATCH", f"/repos/{org}/{org_archived['name']}", {"archived": True})
    user_private = forge.alice.create_repo("alice", unique("user-private"), private=True)
    fork_source = forge.alice.create_repo("alice", unique("fork-source"), private=True)
    fork = forge.api(
        "POST",
        f"/repos/alice/{fork_source['name']}/forks",
        {"organization": org, "name": unique("fork")},
    )
    mirror_source = forge.alice.create_repo("alice", unique("mirror-source"), private=False)
    mirror = forge.api(
        "POST",
        "/repos/migrate",
        {
            "clone_addr": f"http://127.0.0.1:3000/alice/{mirror_source['name']}.git",
            "repo_name": unique("pull-mirror"),
            "repo_owner": org,
            "service": "git",
            "mirror": True,
            "private": True,
        },
    )
    org_listing = forge.client.list_owner_repos(org)
    user_listing = forge.client.list_owner_repos("alice")
    assert org_private["id"] in {repo["id"] for repo in org_listing}
    assert user_private["id"] in {repo["id"] for repo in user_listing}

    admin_access = forge.store.get_access(forge.access_id)
    access = forge.store.add_access(
        forge.instance_id,
        unique("owners-access"),
        admin_access["cred_id"],
        scope_mode="owners",
        owners=[org, "alice"],
        include_forks=False,
        include_archived=False,
        include_public=False,
        include_private=True,
    )

    result = discover(forge.store, access)

    assert task_for_repo(forge.store, org_private["id"])["access_id"] == access
    assert task_for_repo(forge.store, user_private["id"])["access_id"] == access
    assert not any(t["platform_repo_id"] == org_public["id"] for t in forge.store.list_tasks())
    assert not any(t["platform_repo_id"] == org_archived["id"] for t in forge.store.list_tasks())
    assert not any(t["platform_repo_id"] == fork["id"] for t in forge.store.list_tasks())
    assert not any(t["platform_repo_id"] == mirror["id"] for t in forge.store.list_tasks())
    assert item_for(result, mirror["full_name"])["category"] == "Skip"
    assert item_for(result, fork["full_name"])["category"] == "Skip"


def test_two_accesses_keep_first_owner_and_count_other_access_on_real_platform(forge):
    repo = forge.alice.create_repo("alice", unique("shared"), private=True)
    admin_access = forge.store.get_access(forge.access_id)
    alice_cred = forge.store.add_cred(
        unique("alice-token"),
        "https",
        username="alice",
        secret=forge.alice_token,
    )
    alice_access = forge.store.add_access(
        forge.instance_id,
        unique("alice-access"),
        alice_cred,
        scope_mode="owners",
        owners=["alice"],
    )

    forge.store.update_access(forge.access_id, scope_mode="owners", owners=["alice", "gwadmin"])
    first = discover(forge.store, forge.access_id)
    task = task_for_repo(forge.store, repo["id"])
    second = discover(forge.store, alice_access)

    assert repo["full_name"] in first.created
    assert repo["full_name"] in second.other_access
    assert task["access_id"] == forge.access_id
    assert task["source_cred_id"] == admin_access["cred_id"]
    assert forge.store.get_task(task["id"])["source_cred_id"] == admin_access["cred_id"]
    stale = forge.store.add_cred(
        unique("stale"), "https", username="nobody", secret="invalid-test-token"
    )
    forge.store.update_task(task["id"], source_cred_id=stale)
    run_task(forge.store, forge.data, task["id"])
    assert forge.store.get_task(task["id"])["last_status"] == "ok"


def test_permission_loss_marks_not_visible_and_deleted_source_becomes_source_gone(forge, tmp_path):
    org = unique("restricted")
    forge.api("POST", "/orgs", {"username": org, "visibility": "private"})
    repo = forge.client.create_repo(org, unique("source"), private=True)
    forge.api("PUT", f"/repos/{org}/{repo['name']}/collaborators/alice", {"permission": "read"})
    alice_cred = forge.store.add_cred(
        unique("alice-source-token"),
        "https",
        username="alice",
        secret=forge.alice_token,
    )
    access = forge.store.add_access(
        forge.instance_id,
        unique("restricted-access"),
        alice_cred,
        scope_mode="all",
    )
    source = make_source(tmp_path / "source")
    commit(source, "initial")
    with git_env(
        {"kind": "https", "username": "gwadmin", "secret": forge.token},
        tmp_path / "auth",
    ) as env:
        run(source, "push", repo["clone_url"], "main", env=env)

    discover(forge.store, access)
    task = task_for_repo(forge.store, repo["id"])
    main = tmp_path / "target-main.git"
    history = tmp_path / "target-history.git"
    run(None, "init", "--bare", str(main))
    run(None, "init", "--bare", str(history))
    target_id = forge.store.add_target(task["id"], "local", str(main), str(history))
    bind_target(forge.store, forge.data, target_id)
    run_task(forge.store, forge.data, task["id"])

    forge.api("DELETE", f"/repos/{org}/{repo['name']}/collaborators/alice")
    discover(forge.store, access)
    not_visible = forge.store.get_task(task["id"])
    assert not_visible["managed_state"] == "not_visible"
    assert not_visible["enabled"] == 1

    forge.api("DELETE", f"/repos/{org}/{repo['name']}")
    run_task(forge.store, forge.data, task["id"])
    assert forge.store.get_task(task["id"])["managed_state"] == "source_gone"
    discover(forge.store, access)
    assert forge.store.get_task(task["id"])["managed_state"] == "source_gone"
    bundle = tmp_path / "archive.bundle"
    run(archive_path(forge.data, task), "bundle", "create", str(bundle), "--all")
    assert bundle.exists()
