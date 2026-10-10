import re

import pytest
from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.git import run
from gitwarden.runner import archive_path, run_task
from gitwarden.web import create_app
from tests.conftest import commit, make_source

PASSWORD = "pw-task-pages"


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


def test_review_requires_all_three_prerequisites(client, config):
    """Review must reject creation unless every prerequisite is confirmed server-side."""
    login(client)
    source = make_source(config.data_dir.parent / "source")
    commit(source, "initial")
    main = config.data_dir.parent / "main.git"
    history = config.data_dir.parent / "history.git"
    run(None, "init", "--bare", str(main))
    run(None, "init", "--bare", str(history))

    response = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "review",
            "name": "review-gate",
            "source_url": str(source),
            "source_cred_id": "",
            "interval_minutes": "60",
            "enabled": "on",
            "mode": "manual",
            "main_url": str(main),
            "history_url": str(history),
            "target_cred_id": "",
            "confirm_writer": "on",
            "confirm_private": "on",
        },
    )

    assert response.status_code == 400
    assert "review-gate" in response.text
    assert Store(config.data_dir).list_tasks() == []


def test_manual_wizard_binds_runs_and_renders_real_history_state(client, config):
    """The no-JS wizard creates a bound target, runs it, and renders archive-backed details."""
    login(client)
    source = make_source(config.data_dir.parent / "source")
    first = commit(source, "initial")
    commit(source, "second")
    main = config.data_dir.parent / "main.git"
    history = config.data_dir.parent / "history.git"
    run(None, "init", "--bare", str(main))
    run(None, "init", "--bare", str(history))

    def trigger(task_id):
        run_task(Store(config.data_dir), config.data_dir, task_id)

    client.app.state.scheduler.trigger = trigger

    source_data = {
        "name": "manual-wizard",
        "source_url": str(source),
        "source_cred_id": "",
        "interval_minutes": "60",
        "enabled": "on",
    }
    target_data = {
        "mode": "manual",
        "main_url": str(main),
        "history_url": str(history),
        "target_cred_id": "",
    }

    step_one = client.post(
        "/tasks/new",
        data={"csrf": csrf_of(client, "/tasks/new"), "step": "source", **source_data},
        follow_redirects=True,
    )
    assert step_one.status_code == 200
    assert "Backup target" in step_one.text

    step_two = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "targets",
            **source_data,
            **target_data,
        },
        follow_redirects=True,
    )
    assert step_two.status_code == 200
    assert "Review" in step_two.text

    step_three = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "review",
            **source_data,
            **target_data,
            "confirm_writer": "on",
            "confirm_private": "on",
            "confirm_no_ci": "on",
        },
        follow_redirects=False,
    )
    assert step_three.status_code == 303
    task_id = int(step_three.headers["location"].rsplit("/", 1)[1])

    store = Store(config.data_dir)
    task = store.get_task(task_id)
    target = store.list_targets(task_id)[0]
    assert target["bound"] == 1
    assert store.list_runs(task_id)[0]["initial_backup"] == 1
    assert store.list_runs(task_id)[0]["status"] == "ok"

    run(source, "reset", "--hard", first)
    commit(source, "rewritten")
    client.app.state.scheduler.trigger(task_id)

    event = store.list_events(task_id)[0]
    assert event["preserved_ref"]
    preserved_label = event["preserved_ref"].removeprefix("refs/preserved/")
    archive = archive_path(config.data_dir, task)
    preserved_count = len(
        run(
            archive,
            "for-each-ref",
            "refs/preserved",
            "--format=%(refname)",
        ).stdout.splitlines()
    )
    assert preserved_count == 1

    page = client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
    assert str(history) in page.text
    assert f"gw/{preserved_label}" in page.text
    assert f"{preserved_count} / {preserved_count}" in page.text


def test_platform_target_creation_keeps_source_task_manual(client, config, monkeypatch):
    """A provisioned target must not make the source task use target credentials."""
    login(client)
    source = make_source(config.data_dir.parent / "platform-source")
    commit(source, "initial")
    store = Store(config.data_dir)
    source_cred = store.add_cred("source", "https", "source-user", "source-secret")
    target_cred = store.add_cred("platform", "https", "target-user", "target-secret")
    instance_id = store.add_instance("Forgejo", "forgejo", "https://forgejo.test")
    access_id = store.add_access(instance_id, "backup", target_cred)
    provisioned = {}
    triggered = []

    def fake_provision(store_arg, data_dir, task_id, selected_access, owner, name):
        provisioned.update(
            task_id=task_id,
            access_id=selected_access,
            owner=owner,
            name=name,
        )
        return 99

    monkeypatch.setattr("gitwarden.ui_task.provision_target", fake_provision)
    client.app.state.scheduler.trigger = triggered.append

    source_data = {
        "name": "platform-target-source",
        "source_url": str(source),
        "source_cred_id": str(source_cred),
        "interval_minutes": "60",
        "enabled": "on",
    }
    targets = client.post(
        "/tasks/new",
        data={"csrf": csrf_of(client, "/tasks/new"), "step": "source", **source_data},
        follow_redirects=True,
    )
    assert targets.status_code == 200
    review = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "targets",
            "mode": "platform",
            **source_data,
            "access_id": str(access_id),
            "owner": "backups",
            "target_name": "platform-target",
        },
        follow_redirects=True,
    )
    assert review.status_code == 200
    created = client.post(
        "/tasks/new",
        data={
            "csrf": csrf_of(client, "/tasks/new"),
            "step": "review",
            "mode": "platform",
            **source_data,
            "access_id": str(access_id),
            "owner": "backups",
            "target_name": "platform-target",
            "confirm_writer": "on",
            "confirm_private": "on",
            "confirm_no_ci": "on",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    task_id = int(created.headers["location"].rsplit("/", 1)[1])
    task = store.get_task(task_id)
    assert task["source_cred_id"] == source_cred
    assert task["access_id"] is None
    assert task["instance_id"] is None
    assert provisioned == {
        "task_id": task_id,
        "access_id": access_id,
        "owner": "backups",
        "name": "platform-target",
    }
    assert triggered == [task_id]


def test_failed_manual_binding_rolls_back_task_and_allows_same_name_retry(client, config):
    login(client)
    root = config.data_dir.parent
    source = make_source(root / "source")
    commit(source, "initial")
    bad_main, bad_history = root / "bad-main.git", root / "bad-history.git"
    good_main, good_history = root / "good-main.git", root / "good-history.git"
    for repo in (bad_main, bad_history, good_main, good_history):
        run(None, "init", "--bare", str(repo))
    run(source, "push", str(bad_main), "HEAD:refs/heads/main")

    def submit(main, history):
        return client.post(
            "/tasks/new",
            data={
                "csrf": csrf_of(client, "/tasks/new"),
                "step": "review",
                "mode": "manual",
                "name": "retry-me",
                "source_url": str(source),
                "interval_minutes": "60",
                "enabled": "on",
                "target_name": "t",
                "main_url": str(main),
                "history_url": str(history),
                "confirm_writer": "on",
                "confirm_private": "on",
                "confirm_no_ci": "on",
            },
            follow_redirects=False,
        )

    failed = submit(bad_main, bad_history)
    store = Store(config.data_dir)
    assert failed.status_code == 400
    assert "retry-me" in failed.text and "must be empty" in failed.text
    assert store.list_tasks() == [] and store.list_targets() == []
    archives = config.data_dir / "archives"
    assert not archives.exists() or list(archives.iterdir()) == []

    retried = submit(good_main, good_history)
    assert retried.status_code == 303
    assert [task["name"] for task in store.list_tasks()] == ["retry-me"]
