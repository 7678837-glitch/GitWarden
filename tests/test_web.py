import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.git import run
from gitwarden.runner import run_task
from gitwarden.web import create_app
from tests.conftest import commit, make_source

PASSWORD = "pw-test-123"
VERSION = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", admin_password=PASSWORD)


@pytest.fixture
def client(config):
    app = create_app(config, start_scheduler=False)
    with TestClient(app) as c:
        yield c


def login(client):
    resp = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303
    return client


def csrf_of(client, path="/"):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} 页面没有 csrf 字段"
    return match.group(1)


def location_id(resp, prefix):
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].removeprefix(prefix).split("/")[0])


def test_health_is_public_and_returns_version(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"version": VERSION, "git_commit": "unknown"}


def test_anonymous_requests_are_rejected(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert client.post("/tasks/new", data={"name": "x"}).status_code == 401
    assert client.post("/creds", data={"name": "x"}).status_code == 401


def test_wrong_password_is_401_and_correct_password_logs_in(client, monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    resp = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert resp.status_code == 401
    assert "Incorrect password" in resp.text
    assert slept == [1]
    login(client)
    assert client.get("/").status_code == 200


def test_login_replaces_previous_sessions(client):
    """A fresh login keeps only the new session so the map cannot grow forever."""
    first = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert first.status_code == 303
    first_token = first.cookies["gw_session"]
    second = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert second.status_code == 303
    second_token = second.cookies["gw_session"]
    assert second_token != first_token
    assert client.app.state.sessions == {second_token: client.app.state.sessions[second_token]}
    from fastapi.testclient import TestClient

    with TestClient(client.app, follow_redirects=False) as stale:
        stale.cookies.set("gw_session", first_token)
        assert stale.get("/").status_code == 303


def test_post_without_valid_csrf_is_403(client):
    login(client)
    assert client.post("/creds", data={"name": "cred-probe", "kind": "https"}).status_code == 403
    bad = {"name": "cred-probe", "kind": "https", "csrf": "bogus"}
    assert client.post("/creds", data=bad).status_code == 403
    assert "cred-probe" not in client.get("/creds").text


def test_secret_never_rendered_on_any_page(client):
    secret = "S3cr3t-xyz"
    login(client)
    resp = client.post(
        "/creds",
        data={
            "csrf": csrf_of(client, "/creds"),
            "name": "cred-probe",
            "kind": "https",
            "username": "bot",
            "secret": secret,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cred_id = Store(client.app.state.data_dir).list_creds()[0]["id"]
    task_id = Store(client.app.state.data_dir).add_task(
        "demo", "https://example.com/org/demo.git", source_cred_id=cred_id
    )
    resp = client.post(
        f"/tasks/{task_id}/targets",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "name": "main",
            "main_url": "https://example.com/org/demo-main.git",
            "history_url": "https://example.com/org/demo-hist.git",
            "cred_id": str(cred_id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    for path in ("/", "/tasks", "/tasks/new", "/platforms", "/creds", "/logs", f"/tasks/{task_id}"):
        text = client.get(path).text
        assert secret not in text, path
    assert "cred-probe" in client.get("/creds").text


def test_invalid_url_shows_chinese_error_not_500(client):
    login(client)
    client.cookies.set("gw_lang", "zh_CN")
    resp = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "name": "bad",
            "step": "source",
            "source_url": "ext::sh",
            "interval_minutes": "60",
            "enabled": "on",
        },
    )
    assert resp.status_code == 400
    assert "不支持的 Git URL 协议" in resp.text


def test_bundle_recovers_commit_lost_to_force_push(client, config):
    data_dir = config.data_dir
    login(client)
    origin = config.data_dir.parent / "origin.git"
    run(None, "init", "--bare", str(origin))
    work = make_source(config.data_dir.parent / "work")
    commit(work, "one")
    two = commit(work, "two")
    run(work, "push", str(origin), "main")

    main = config.data_dir.parent / "t-main.git"
    hist = config.data_dir.parent / "t-hist.git"
    run(None, "init", "--bare", str(main))
    run(None, "init", "--bare", str(hist))

    task_id = Store(data_dir).add_task("demo", str(origin))
    resp = client.post(
        f"/tasks/{task_id}/targets",
        data={
            "csrf": csrf_of(client, f"/tasks/{task_id}"),
            "name": "t1",
            "main_url": str(main),
            "history_url": str(hist),
            "cred_id": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    store = Store(data_dir)
    target_id = store.list_targets(task_id)[0]["id"]
    resp = client.post(
        f"/targets/{target_id}/bind",
        data={"csrf": csrf_of(client, f"/tasks/{task_id}")},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    run_task(store, data_dir, task_id)
    run(work, "reset", "--hard", "HEAD~1")
    commit(work, "three")
    run(work, "push", "--force", str(origin), "main")
    run_task(store, data_dir, task_id)

    event = next(e for e in store.list_events(task_id) if e["preserved_ref"])
    assert event["old"] == two
    resp = client.get(f"/tasks/{task_id}/events/{event['id']}/bundle")
    assert resp.status_code == 200
    assert (
        resp.headers["content-disposition"] == f'attachment; filename="demo-{event["id"]}.bundle"'
    )
    bundle = config.data_dir.parent / "restored.bundle"
    bundle.write_bytes(resp.content)
    heads = run(None, "bundle", "list-heads", str(bundle)).stdout.split()
    restored = config.data_dir.parent / "restored.git"
    run(None, "init", "--bare", str(restored))
    run(restored, "fetch", str(bundle), f"{heads[1]}:refs/restored/x")
    assert run(restored, "rev-parse", "refs/restored/x^{commit}").stdout.strip() == two


def test_task_detail_shows_incomplete_backup_note(client, config):
    login(client)
    client.cookies.set("gw_lang", "zh_CN")
    store = Store(config.data_dir)
    task_id = store.add_task("lfs-task", str(config.data_dir.parent / "none.git"))
    store.set_external_note(task_id, "LFS：main")
    page = client.get(f"/tasks/{task_id}").text
    assert "LFS：main" in page
    assert "备份不完整" in page
    assert "备份不完整" in client.get("/").text


def test_bundle_download_avoids_eager_read_and_removes_temporary_directory(
    client, config, monkeypatch
):
    login(client)
    source = make_source(config.data_dir.parent / "bundle-source")
    commit(source, "one")
    commit(source, "two")
    store = Store(config.data_dir)
    task = store.add_task("bundle", str(source))
    run_task(store, config.data_dir, task)
    run(source, "reset", "--hard", "HEAD~1")
    run_task(store, config.data_dir, task)
    event = store.list_events(task)[0]
    original_read_bytes = Path.read_bytes

    def reject_eager_bundle_read(path):
        assert path.name != "restore.bundle", "恢复包不得一次性读入内存"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_eager_bundle_read)
    response = client.get(f"/tasks/{task}/events/{event['id']}/bundle")
    assert response.status_code == 200
    assert response.content.startswith(b"# v")
    assert not list((config.data_dir / "work").glob("bundle-*"))


def test_failed_bundle_generation_keeps_chinese_error_and_cleans_directory(client, config):
    login(client)
    client.cookies.set("gw_lang", "zh_CN")
    source = make_source(config.data_dir.parent / "bundle-source")
    commit(source, "one")
    store = Store(config.data_dir)
    task = store.add_task("bundle", str(source))
    run_task(store, config.data_dir, task)
    store.add_event(task, "refs/heads/main", "force", None, None, "refs/preserved/missing")
    event = store.list_events(task)[0]
    response = client.get(f"/tasks/{task}/events/{event['id']}/bundle")
    assert response.status_code == 500
    assert response.json()["detail"] == "生成恢复包失败，请查看服务日志。"
    assert not list((config.data_dir / "work").glob("bundle-*"))


@pytest.mark.parametrize(
    "lang,expected", [("en", "Interrupted by service restart"), ("zh_CN", "服务重启时中断")]
)
def test_startup_finishes_interrupted_runs_but_keeps_completed_runs(config, lang, expected):
    from gitwarden.i18n import render

    store = Store(config.data_dir)
    task = store.add_task("interrupted", "/tmp/local-fixture.git", enabled=False)
    completed = store.start_run(task)
    store.finish_run(completed, "ok", "previous success")
    completed_before = store.list_runs(task)[0]
    interrupted = store.start_run(task, progress="Receiving objects: 40%")
    for _ in range(2):
        with TestClient(create_app(config, start_scheduler=False)) as client:
            latest, previous = store.list_runs(task)
            assert latest["id"] == interrupted and latest["status"] == "failed"
            assert latest["finished_at"] is not None
            assert render(latest["message"], lang) == expected
            assert previous == completed_before
            login(client)
            client.cookies.set("gw_lang", lang)
            assert expected in client.get("/logs").text
            assert expected in client.get(f"/tasks/{task}").text
