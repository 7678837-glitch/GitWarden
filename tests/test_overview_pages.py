import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.web import create_app

PASSWORD = "pw-overview-test"


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


def add_platform_access(store: Store) -> int:
    cred_id = store.add_cred("overview-platform", "https", username="admin", secret="token")
    instance_id = store.add_instance("Overview Forgejo", "forgejo", "https://forgejo.example")
    return store.add_access(instance_id, "admin-all", cred_id)


def add_complete_target(store: Store, task_id: int) -> int:
    target_id = store.add_target(
        task_id,
        "NAS",
        "https://backup.example/main.git",
        "https://backup.example/history.git",
    )
    store.set_bound(target_id, True)
    store.set_target_status(target_id, "ok", "同步完成")
    return target_id


def test_tasks_searches_name_and_url_and_filters_state_manager_and_backup(client, config):
    login(client)
    store = Store(config.data_dir)
    access_id = add_platform_access(store)

    managed = store.add_task(
        "managed-name",
        "https://source.example/name-repository.git",
        instance_id=1,
        access_id=access_id,
        platform_repo_id=101,
    )
    not_visible = store.add_task(
        "not-visible-task",
        "https://source.example/other.git",
        instance_id=1,
        access_id=access_id,
        platform_repo_id=102,
        managed_state="not_visible",
    )
    disabled = store.add_task(
        "disabled-manual",
        "https://source.example/disabled.git",
        enabled=False,
    )
    store.add_task(
        "excluded-task",
        "https://source.example/excluded.git",
        enabled=False,
        managed_state="excluded",
    )
    page = client.get("/tasks?status=excluded")
    assert ">excluded-task</a>" in page.text
    store.add_task("incomplete-task", "https://source.example/incomplete.git")
    complete = store.add_task("complete-task", "https://source.example/complete.git")
    add_complete_target(store, complete)

    page = client.get("/tasks?search=managed-name")
    assert page.status_code == 200
    assert "managed-name" in page.text
    assert "not-visible-task" not in page.text

    page = client.get("/tasks?search=NAME-REPOSITORY")
    assert page.status_code == 200
    assert "managed-name" in page.text

    page = client.get("/tasks?status=not_visible")
    assert page.status_code == 200
    assert "not-visible-task" in page.text
    assert "managed-name" not in page.text

    page = client.get("/tasks?status=disabled")
    assert page.status_code == 200
    assert "disabled-manual" in page.text
    assert "managed-name" not in page.text

    page = client.get(f"/tasks?managed=access:{access_id}")
    assert page.status_code == 200
    assert "managed-name" in page.text
    assert "disabled-manual" not in page.text

    page = client.get("/tasks?managed=manual")
    assert page.status_code == 200
    assert "disabled-manual" in page.text
    assert "managed-name" not in page.text

    page = client.get("/tasks?backup=complete")
    assert page.status_code == 200
    assert "complete-task" in page.text
    assert "incomplete-task" not in page.text

    page = client.get("/tasks?backup=incomplete")
    assert page.status_code == 200
    assert "incomplete-task" in page.text
    assert ">complete-task</a>" not in page.text

    assert managed and not_visible and disabled


def test_tasks_paginates_twenty_rows_and_preserves_query_filters(client, config):
    login(client)
    store = Store(config.data_dir)
    for index in range(21):
        store.add_task(f"page-task-{index:02d}", f"https://source.example/page-{index:02d}.git")

    first = client.get("/tasks?page=1&search=page-task")
    assert first.status_code == 200
    assert sum(name in first.text for name in (f"page-task-{i:02d}" for i in range(21))) == 20
    assert "page-task-20" not in first.text
    assert "21" in first.text
    assert re.search(r'href="[^"]*page=2[^"]*search=page-task', first.text)

    second = client.get("/tasks?page=2&search=page-task")
    assert second.status_code == 200
    assert "page-task-20" in second.text
    assert "page-task-00" not in second.text
    assert re.search(r'href="[^"]*page=1[^"]*search=page-task', second.text)


def test_overview_renders_real_attention_summary_preservation_and_latest_run(client, config):
    login(client)
    store = Store(config.data_dir)
    healthy = store.add_task("healthy-task", "https://source.example/healthy.git")
    add_complete_target(store, healthy)
    overdue = store.add_task(
        "overdue-task",
        "https://source.example/overdue.git",
        interval_minutes=5,
        next_run_at=(datetime.now(UTC) - timedelta(minutes=20)).isoformat(timespec="seconds"),
    )
    store.add_task("disabled-task", "https://source.example/disabled.git", enabled=False)
    store.add_event(
        overdue,
        "refs/heads/main",
        "force",
        "aaaaaaaa",
        "bbbbbbbb",
        "refs/preserved/1",
    )
    run_id = store.start_run(healthy)
    store.finish_run(run_id, "ok", "同步完成")

    page = client.get("/")
    assert page.status_code == 200
    assert "healthy-task" in page.text
    assert "overdue-task" in page.text
    assert "disabled-task" in page.text
    assert "Needs attention" in page.text
    assert "refs/heads/main" in page.text
    assert "aaaaaaa" in page.text and "bbbbbbb" in page.text
    assert "Latest runs" in page.text
    assert "[N]" not in page.text


def test_tasks_show_running_initial_backup_progress(client, config):
    login(client)
    store = Store(config.data_dir)
    task_id = store.add_task("initial-backup", "https://source.example/initial.git")
    store.start_run(task_id, initial_backup=True, progress="received 3.2 GB")

    page = client.get("/tasks")
    assert page.status_code == 200
    assert "initial-backup" in page.text
    assert "Running" in page.text
    assert "Initial backup" in page.text
    assert "received 3.2 GB" in page.text
