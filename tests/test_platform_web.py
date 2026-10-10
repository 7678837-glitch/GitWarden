import re
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


def csrf_of(client, path):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} 页面没有 csrf 字段"
    return match.group(1)


def add_credential(client, secret="platform-token"):
    response = client.post(
        "/creds",
        data={
            "csrf": csrf_of(client, "/creds"),
            "name": "platform-cred",
            "kind": "https",
            "username": "admin",
            "secret": secret,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return Store(client.app.state.data_dir).list_creds()[0]["id"]


def add_platform(client, cred_id=""):
    response = client.post(
        "/instances",
        data={
            "csrf": csrf_of(client, "/platforms"),
            "name": "local-gitea",
            "kind": "gitea",
            "base_url": "https://git.example.test",
            "cred_id": str(cred_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    instance = Store(client.app.state.data_dir).list_instances()[0]
    response = client.post(
        "/accesses",
        data={
            "csrf": csrf_of(client, "/platforms"),
            "instance_id": instance["id"],
            "name": "local-gitea",
            "cred_id": str(cred_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return Store(client.app.state.data_dir).list_accesses()[0]


def test_platform_pages_require_login_and_csrf(client):
    response = client.get("/platforms", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.post("/instances", data={"name": "x"}).status_code == 401

    login(client)
    assert client.get("/platforms").status_code == 200
    assert client.post("/instances", data={"name": "x"}).status_code == 403
    assert client.post("/accesses/1/discover", data={}).status_code == 403


def test_platform_crud_preserves_credential_reference_and_hides_secret(client):
    login(client)
    secret = "platform-secret-do-not-render"
    cred_id = add_credential(client, secret)
    platform = add_platform(client, cred_id)

    page = client.get("/platforms")
    assert page.status_code == 200
    assert "local-gitea" in page.text
    assert secret not in page.text

    response = client.post(
        f"/creds/{cred_id}/delete",
        data={"csrf": csrf_of(client, "/creds")},
    )
    assert response.status_code == 400
    assert "referenced" in response.text
    assert secret not in response.text
    assert Store(client.app.state.data_dir).get_cred(cred_id) is not None

    response = client.post(
        f"/accesses/{platform['id']}/delete",
        data={"csrf": csrf_of(client, "/platforms")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        f"/creds/{cred_id}/delete",
        data={"csrf": csrf_of(client, "/creds")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert Store(client.app.state.data_dir).list_accesses() == []


def test_discover_renders_all_result_buckets_and_redacts_errors(client, monkeypatch):
    login(client)
    secret = "discovery-error-secret"
    platform = add_platform(client, add_credential(client, secret))
    calls = []

    def fake_discover(store, platform_id, draft):
        calls.append((store.path, platform_id))
        return SimpleNamespace(
            created=["alice/new"],
            existing=["alice/existing"],
            renamed=["alice/renamed"],
            conflicts=["alice/conflict"],
            skipped=["alice/mirror"],
        )

    monkeypatch.setattr(web, "preview", fake_discover, raising=False)
    response = client.post(
        f"/accesses/{platform['id']}/preview",
        data={"csrf": csrf_of(client, "/platforms")},
    )
    assert response.status_code == 200
    for name in ("alice/new", "alice/existing", "alice/renamed", "alice/conflict", "alice/mirror"):
        assert name in response.text
    assert calls == [(Store(client.app.state.data_dir).path, platform["id"])]

    def failed_discover(store, platform_id, draft):
        raise ValueError(f"平台请求失败: {secret}")

    monkeypatch.setattr(web, "preview", failed_discover, raising=False)
    response = client.post(
        f"/accesses/{platform['id']}/preview",
        data={"csrf": csrf_of(client, "/platforms")},
    )
    assert response.status_code == 400
    assert secret not in response.text


def test_task_detail_provision_form_calls_backend_and_redacts_failure(client, config, monkeypatch):
    login(client)
    secret = "provision-error-secret"
    platform = add_platform(client, add_credential(client, secret))
    store = Store(config.data_dir)
    task_id = store.add_task("demo", "https://source.example/demo.git")

    detail = client.get(f"/tasks/{task_id}")
    assert detail.status_code == 200
    assert "Create on a platform" in detail.text
    assert 'name="access_id"' in detail.text
    assert 'name="owner"' in detail.text
    assert 'name="name"' in detail.text

    calls = []

    def fake_provision(store_arg, data_dir, task_id_arg, platform_id, owner, name):
        calls.append((store_arg.path, data_dir, task_id_arg, platform_id, owner, name))
        return 17

    monkeypatch.setattr(web, "provision_target", fake_provision, raising=False)
    response = client.post(
        f"/tasks/{task_id}/targets/provision",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "access_id": str(platform["id"]),
            "owner": "alice",
            "name": "demo-backup",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/tasks/{task_id}"
    assert calls == [(store.path, config.data_dir, task_id, platform["id"], "alice", "demo-backup")]

    def failed_provision(*args):
        raise ValueError(f"创建仓库失败: {secret}" + "x" * 350 + "；已创建：backups/demo-history")

    monkeypatch.setattr(web, "provision_target", failed_provision, raising=False)
    response = client.post(
        f"/tasks/{task_id}/targets/provision",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "access_id": str(platform["id"]),
            "owner": "alice",
            "name": "demo-backup",
        },
    )
    assert response.status_code == 400
    assert secret not in response.text

    assert "backups/demo-history" in response.text


def test_platform_url_with_embedded_token_is_not_redisplayed(client):
    login(client)
    cred_id = add_credential(client)
    response = client.post(
        "/instances",
        data={
            "csrf": csrf_of(client, "/platforms"),
            "name": "bad",
            "kind": "gitea",
            "base_url": "https://bot:do-not-echo-me@git.example.test",
            "cred_id": str(cred_id),
        },
    )
    assert response.status_code == 400
    assert "do-not-echo-me" not in response.text
