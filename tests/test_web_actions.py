import re
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import gitwarden.web as web
from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.web import create_app

PASSWORD = "pw-test-123"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", admin_password=PASSWORD)


@pytest.fixture
def client(config):
    app = create_app(config, start_scheduler=False)
    with TestClient(app) as test_client:
        yield test_client


def login(client):
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303


def csrf_of(client, path="/"):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} 页面没有 csrf 字段"
    return match.group(1)


def _task_and_target(client):
    store = Store(client.app.state.data_dir)
    task_id = store.add_task("demo", "https://source.example/demo.git")
    target_id = store.add_target(
        task_id,
        "backup",
        "https://target.example/demo.git",
        "https://target.example/demo-history.git",
    )
    return store, task_id, target_id


def _access(client):
    store = Store(client.app.state.data_dir)
    cred_id = store.add_cred("platform", "https", username="admin", secret="platform-secret")
    instance_id = store.add_instance("gitea", "gitea", "https://git.example.test")
    access_id = store.add_access(instance_id, "admin", cred_id)
    return store, access_id


def test_new_post_actions_require_csrf(client):
    login(client)
    for path in (
        "/tasks/bulk",
        "/tasks/1/toggle",
        "/tasks/1/detailed",
        "/targets/1/retry",
        "/targets/1/audit",
        "/accesses/1/preview",
        "/accesses/1",
        "/accesses/1/discover",
    ):
        assert client.post(path, data={}).status_code == 403, path


def test_task_toggle_bulk_and_detailed_actions_update_store_and_trigger_scheduler(
    client, monkeypatch
):
    login(client)
    store, task_id, _ = _task_and_target(client)
    calls = []
    monkeypatch.setattr(
        client.app.state.scheduler,
        "trigger",
        lambda task_id_arg: calls.append(task_id_arg),
    )

    response = client.post(
        f"/tasks/{task_id}/toggle",
        data={"csrf": csrf_of(client, f"/tasks/{task_id}"), "enabled": "0"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.get_task(task_id)["enabled"] == 0

    response = client.post(
        "/tasks/bulk",
        data={
            "csrf": csrf_of(client, "/"),
            "task_ids": str(task_id),
            "action": "enable",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.get_task(task_id)["enabled"] == 1

    response = client.post(
        f"/tasks/{task_id}/detailed",
        data={"csrf": csrf_of(client, f"/tasks/{task_id}"), "enabled": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.get_task(task_id)["detailed_logging"] == 1
    assert calls == []

    response = client.post(
        f"/tasks/{task_id}/edit",
        data={
            "csrf": csrf_of(client),
            "name": "demo",
            "source_url": "https://source.example/demo.git",
            "interval_minutes": "60",
            "enabled": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.get_task(task_id)["enabled"] == 0


def test_target_retry_and_audit_route_to_runner_and_record_audit(client, monkeypatch):
    login(client)
    store, task_id, target_id = _task_and_target(client)
    retry_calls = []
    audit_calls = []

    monkeypatch.setattr(
        web,
        "retry_target",
        lambda store_arg, data_dir, target_id_arg: retry_calls.append(
            (store_arg.path, data_dir, target_id_arg)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        web,
        "audit_target",
        lambda store_arg, data_dir, target_id_arg: audit_calls.append(
            (store_arg.path, data_dir, target_id_arg)
        ),
        raising=False,
    )

    response = client.post(
        f"/targets/{target_id}/retry",
        data={"csrf": csrf_of(client, f"/tasks/{task_id}"), "target_id": str(target_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert retry_calls == [(store.path, client.app.state.data_dir, target_id)]

    response = client.post(
        f"/targets/{target_id}/audit",
        data={"csrf": csrf_of(client, f"/tasks/{task_id}")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert audit_calls == [(store.path, client.app.state.data_dir, target_id)]
    assert store.get_target(target_id)["last_audit_at"]


def test_access_preview_is_read_only_and_discover_and_save_apply_share_queue(client, monkeypatch):
    login(client)
    store, access_id = _access(client)
    before = store.get_access(access_id)
    preview_calls = []
    discover_calls = []
    monkeypatch.setattr(
        web,
        "preview",
        lambda store_arg, access_id_arg, draft: (
            preview_calls.append((store_arg.path, access_id_arg, draft))
            or SimpleNamespace(items=[{"category": "Add", "name": "alice/demo", "reason": "new"}])
        ),
        raising=False,
    )
    monkeypatch.setattr(
        client.app.state.scheduler,
        "trigger_discovery",
        lambda access_id_arg: discover_calls.append(access_id_arg),
        raising=False,
    )

    data = {
        "csrf": csrf_of(client, "/platforms"),
        "scope_mode": "all",
        "owners": "",
        "rule_mode": "exclude",
        "patterns": "sandbox/*",
        "auto_enable": "on",
        "detailed_default": "off",
        "discover_interval_minutes": "60",
    }
    response = client.post(f"/accesses/{access_id}/preview", data=data)
    assert response.status_code == 200
    assert "alice/demo" in response.text
    assert preview_calls and preview_calls[0][1] == access_id
    assert store.get_access(access_id) == before
    assert discover_calls == []

    response = client.post(
        f"/accesses/{access_id}/discover",
        data={"csrf": csrf_of(client, "/platforms")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert discover_calls == [access_id]

    response = client.post(
        f"/accesses/{access_id}",
        data={**data, "name": "admin-updated", "cred_id": str(before["cred_id"])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.get_access(access_id)["name"] == "admin-updated"
    assert discover_calls == [access_id, access_id]


def test_all_login_logout_and_post_success_failure_activity_is_redacted(client, monkeypatch):
    monkeypatch.setattr("gitwarden.web.time.sleep", lambda _: None)
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    login(client)
    store = Store(client.app.state.data_dir)
    cred_id = store.add_cred("secret", "https", username="bot", secret="do-not-log-secret")
    csrf = csrf_of(client, "/platforms")
    response = client.post(
        "/instances",
        data={
            "csrf": csrf,
            "name": "bad",
            "kind": "gitea",
            "base_url": "https://bot:do-not-log-secret@git.example.test",
            "cred_id": str(cred_id),
        },
    )
    assert response.status_code == 400

    unused = store.add_cred("unused", "https", username="bot", secret="removed-secret")
    assert client.post(f"/creds/{unused}/delete", data={"csrf": csrf}).status_code == 200
    task_id = store.add_task("bulk", "https://source.example/bulk.git")
    assert (
        client.post(
            "/tasks/bulk", data={"csrf": csrf, "task_ids": str(task_id), "action": "disable"}
        ).status_code
        == 200
    )
    instance = store.add_instance("local", "forgejo", "http://127.0.0.1:18301")
    access = store.add_access(instance, "local-access", cred_id)
    monkeypatch.setattr(client.app.state.scheduler, "trigger_discovery", lambda _: None)
    assert (
        client.post(f"/accesses/{access}", data={"csrf": csrf, "patterns": "sandbox/*"}).status_code
        == 200
    )
    assert client.post("/logout", data={"csrf": csrf}).status_code == 200

    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT action, object, detail FROM activity").fetchall()
    assert rows
    actions = [row[0] for row in rows]
    assert actions.count("login") == 2
    assert {"instance.create", "credential.delete", "task.bulk", "access.save", "logout"}.issubset(
        actions
    )
    assert any(row[0] == "login" and "401" in row[2] for row in rows)
    assert any(row[0] == "login" and row[2] == "success" for row in rows)
    assert all("do-not-log-secret" not in " ".join(str(value) for value in row) for row in rows)


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("get", "/tasks/99999999999999999999999", None),
        ("get", "/logs?task_id=99999999999999999999999", None),
        ("post", "/tasks/99999999999999999999999/run", {}),
        ("post", "/creds/99999999999999999999999/delete", {}),
        ("post", "/instances/99999999999999999999999/delete", {}),
        ("post", "/tasks/bulk", {"action": "run", "task_ids": "99999999999999999999999"}),
        (
            "post",
            "/tasks/1/edit",
            {"name": "a", "source_url": "https://h/x.git", "interval_minutes": "9" * 30},
        ),
    ],
)
def test_integers_beyond_sqlite_range_are_rejected_without_server_error(config, method, path, data):
    app = create_app(config, start_scheduler=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client)
        Store(client.app.state.data_dir).add_task("demo", "https://source.example/demo.git")
        if data is not None:
            data = {"csrf": csrf_of(client, "/creds"), **data}
        response = getattr(client, method)(path, **({} if data is None else {"data": data}))
    assert response.status_code in {400, 404}


def _csrf(client, path="/platforms"):
    return csrf_of(client, path)


@pytest.mark.parametrize(
    "draft",
    [{"cred_id": "999"}, {"scope_mode": "bogus"}, {"rule_mode": "bogus"}],
)
def test_preview_rejects_drafts_that_save_would_reject(client, monkeypatch, draft):
    login(client)
    _, access_id = _access(client)
    monkeypatch.setattr(web, "preview", lambda *a, **k: pytest.fail("preview must not run"))

    response = client.post(f"/accesses/{access_id}/preview", data={"csrf": _csrf(client), **draft})

    assert response.status_code == 400


def test_owners_scope_requires_at_least_one_owner_on_create_and_edit(client):
    login(client)
    store, access_id = _access(client)
    access = store.get_access(access_id)
    base = {"csrf": _csrf(client), "scope_mode": "owners", "owners": " , "}

    created = client.post(
        "/accesses",
        data={
            **base,
            "instance_id": str(access["instance_id"]),
            "name": "second",
            "cred_id": str(access["cred_id"]),
        },
    )
    edited = client.post(f"/accesses/{access_id}", data=base)

    assert created.status_code == 400 and edited.status_code == 400
    assert [a["name"] for a in store.list_accesses()] == ["admin"]
    assert store.get_access(access_id)["scope_mode"] == "all"


def test_bulk_action_with_a_missing_task_changes_nothing_and_returns_404(client):
    login(client)
    store, task_id, _ = _task_and_target(client)
    store.update_task(task_id, enabled=True)

    response = client.post(
        "/tasks/bulk",
        data={"csrf": _csrf(client, "/"), "task_ids": [str(task_id), "9999"], "action": "disable"},
    )

    assert response.status_code == 404
    assert store.get_task(task_id)["enabled"] == 1


def test_bulk_activity_records_action_and_task_count(client):
    login(client)
    store, task_id, _ = _task_and_target(client)

    client.post(
        "/tasks/bulk",
        data={"csrf": _csrf(client, "/"), "task_ids": [str(task_id)], "action": "disable"},
    )

    details = [row["detail"] for row in store._rows("SELECT detail FROM activity")]
    assert any("disable" in d and "1" in d for d in details)


def test_out_of_range_access_id_in_platforms_query_is_ignored(client):
    login(client)
    _access(client)

    assert client.get("/platforms?access_id=99999999999999999999").status_code == 200


def test_target_card_has_edit_and_delete_controls_and_edit_route_works(client):
    login(client)
    store, task_id, target_id = _task_and_target(client)
    store.set_bound(target_id, True)
    page = client.get(f"/tasks/{task_id}").text
    assert f'action="/targets/{target_id}/edit"' in page
    assert f'action="/targets/{target_id}/delete"' in page

    bad = client.post(
        f"/targets/{target_id}/edit",
        data={
            "csrf": _csrf(client, f"/tasks/{task_id}"),
            "name": "renamed",
            "main_url": "ext::sh -c id",
            "history_url": "https://target.example/h.git",
            "cred_id": "",
        },
    )
    assert bad.status_code == 400
    assert store.get_target(target_id)["name"] == "backup"

    ok = client.post(
        f"/targets/{target_id}/edit",
        data={
            "csrf": _csrf(client, f"/tasks/{task_id}"),
            "name": "renamed",
            "main_url": "https://target.example/new.git",
            "history_url": "https://target.example/new-history.git",
            "cred_id": "",
        },
        follow_redirects=False,
    )
    target = store.get_target(target_id)
    assert ok.status_code == 303
    assert target["name"] == "renamed" and target["main_url"].endswith("new.git")
    assert target["bound"] == 0


def _synced_local_target(client, tmp_path):
    from gitwarden.git import run as git
    from gitwarden.runner import bind_target, run_task
    from tests.conftest import commit, make_source

    store = Store(client.app.state.data_dir)
    src = make_source(tmp_path / "src")
    commit(src, "one")
    task_id = store.add_task("local", str(src))

    def bare_pair(name):
        main, hist = tmp_path / f"{name}-main.git", tmp_path / f"{name}-hist.git"
        git(None, "init", "--bare", str(main))
        git(None, "init", "--bare", str(hist))
        return str(main), str(hist)

    main, hist = bare_pair("a")
    target_id = store.add_target(task_id, "backup", main, hist)
    bind_target(store, client.app.state.data_dir, target_id)
    run_task(store, client.app.state.data_dir, task_id)
    assert store.get_target(target_id)["last_status"] == "ok"
    return store, task_id, target_id, bare_pair, bind_target, run_task


def test_moving_a_target_to_new_repositories_syncs_without_stale_conflicts(client, tmp_path):
    login(client)
    store, task_id, target_id, bare_pair, bind_target, run_task = _synced_local_target(
        client, tmp_path
    )
    main, hist = bare_pair("b")
    response = client.post(
        f"/targets/{target_id}/edit",
        data={
            "csrf": _csrf(client, f"/tasks/{task_id}"),
            "name": "backup",
            "main_url": main,
            "history_url": hist,
            "cred_id": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    bind_target(store, client.app.state.data_dir, target_id)
    run_task(store, client.app.state.data_dir, task_id)
    assert store.get_target(target_id)["last_status"] == "ok"


def test_rotating_target_credential_preserves_binding_and_sync(client, tmp_path):
    login(client)
    store, task_id, target_id, _, _, run_task = _synced_local_target(client, tmp_path)
    target = store.get_target(target_id)
    credential = store.add_cred("rotated", "https", username="backup", secret="test-only")
    response = client.post(
        f"/targets/{target_id}/edit",
        data={
            "csrf": _csrf(client, f"/tasks/{task_id}"),
            "name": target["name"],
            "main_url": target["main_url"],
            "history_url": target["history_url"],
            "cred_id": str(credential),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert store.get_target(target_id)["bound"] == 1, "Credential rotation lost target binding"
    run_task(store, client.app.state.data_dir, task_id)
    assert store.get_target(target_id)["last_status"] == "ok"


def test_deleting_a_target_removes_its_push_records(client, tmp_path):
    from gitwarden.git import run as git
    from gitwarden.runner import archive_path

    login(client)
    store, task_id, target_id, *_ = _synced_local_target(client, tmp_path)
    archive = archive_path(client.app.state.data_dir, store.get_task(task_id))
    response = client.post(
        f"/targets/{target_id}/delete",
        data={"csrf": _csrf(client, f"/tasks/{task_id}")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert git(archive, "for-each-ref", f"refs/pushed/{target_id}/").stdout == ""
    assert git(archive, "for-each-ref", "refs/heads/").stdout != ""


@pytest.mark.parametrize("replacement", ["rotated-token", ""])
def test_credential_edit_rotates_shared_token_without_rebinding(client, replacement):
    login(client)
    store, access_id = _access(client)
    cred_id = store.get_access(access_id)["cred_id"]
    task = store.add_task("shared", "https://example.test/source.git", source_cred_id=cred_id)
    target = store.add_target(
        task, "backup", "https://example.test/main.git", "https://example.test/history.git", cred_id
    )
    store.set_bound(target, True)
    response = client.post(
        f"/creds/{cred_id}/edit",
        data={
            "csrf": csrf_of(client),
            "name": "rotated",
            "username": "new-user",
            "secret": replacement,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    cred = store.get_cred(cred_id)
    assert cred["name"] == "rotated" and cred["username"] == "new-user"
    assert cred["secret"] == (replacement or "platform-secret")
    assert store.get_task(task)["source_cred_id"] == cred_id
    assert store.get_target(target)["cred_id"] == cred_id
    assert store.get_target(target)["bound"] == 1
    assert store.get_access(access_id)["cred_id"] == cred_id
    page = client.get(f"/creds?edit={cred_id}").text
    assert f'action="/creds/{cred_id}/edit"' in page
    assert "platform-secret" not in page and "rotated-token" not in page
    activity = store._rows("SELECT * FROM activity WHERE action='credential.update'")
    assert len(activity) == 1
    assert not any(secret in str(activity) for secret in ("platform-secret", "rotated-token"))


@pytest.mark.parametrize("replacement", ["ROTATED-PRIVATE-KEY", ""])
def test_credential_edit_updates_ssh_fields_without_exposing_key(client, replacement):
    login(client)
    store = client.app.state.store
    cred_id = store.add_cred(
        "ssh",
        "ssh",
        username="git",
        private_key="ORIGINAL-PRIVATE-KEY",
        known_hosts="old-host ssh-ed25519 AAAA",
    )
    response = client.post(
        f"/creds/{cred_id}/edit",
        data={
            "csrf": csrf_of(client),
            "name": "ssh rotated",
            "username": "git-new",
            "private_key": replacement,
            "known_hosts": "new-host ssh-ed25519 BBBB\r\n",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    cred = store.get_cred(cred_id)
    assert cred["private_key"] == (replacement or "ORIGINAL-PRIVATE-KEY")
    assert cred["known_hosts"] == "new-host ssh-ed25519 BBBB\n"
    assert cred["username"] == "git-new"
    page = client.get(f"/creds?edit={cred_id}").text
    assert "ORIGINAL-PRIVATE-KEY" not in page and "ROTATED-PRIVATE-KEY" not in page
    assert "new-host ssh-ed25519 BBBB" in page


def test_credential_edit_enforces_authentication_csrf_and_immutable_type(client):
    store, access_id = _access(client)
    cred_id = store.get_access(access_id)["cred_id"]
    original = store.get_cred(cred_id)
    route = f"/creds/{cred_id}/edit"
    assert client.post(route, data={"name": "changed"}).status_code == 401
    login(client)
    assert client.post(route, data={"name": "changed"}).status_code == 403
    response = client.post(
        route,
        data={
            "csrf": csrf_of(client),
            "name": "changed",
            "kind": "ssh",
            "private_key": "must-not-leak",
        },
    )
    assert response.status_code == 400
    assert "must-not-leak" not in response.text and "platform-secret" not in response.text
    assert store.get_cred(cred_id) == original


def test_credential_edit_failure_hides_old_and_new_secrets(client):
    login(client)
    store, access_id = _access(client)
    cred_id = store.get_access(access_id)["cred_id"]
    original = store.get_cred(cred_id)
    response = client.post(
        f"/creds/{cred_id}/edit",
        data={
            "csrf": csrf_of(client),
            "name": "",
            "username": "platform-secret",
            "secret": "new-secret",
            "known_hosts": "new-secret",
        },
    )
    assert response.status_code == 400
    assert "platform-secret" not in response.text and "new-secret" not in response.text
    assert store.get_cred(cred_id) == original
    activity = store._rows("SELECT * FROM activity WHERE action='credential.update'")
    assert activity and "400" in activity[0]["detail"]
    assert "platform-secret" not in str(activity) and "new-secret" not in str(activity)
