import re
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from gitwarden import logs
from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.web import create_app

PASSWORD = "pw-test-123"


def _csrf(client: TestClient, path: str = "/logs") -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} 页面没有 csrf 字段"
    return match.group(1)


def _login(client: TestClient) -> None:
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303


def _run(store: Store, task_id: int, message: str, status: str = "ok") -> int:
    run_id = store.start_run(task_id)
    store.finish_run(run_id, status, message)
    return run_id


def _app(tmp_path):
    return create_app(
        Config(data_dir=tmp_path / "data", admin_password=PASSWORD),
        start_scheduler=False,
    )


def test_logs_has_runs_and_activity_tabs_with_real_sqlite(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        runs = client.get("/logs", params={"tab": "runs"})
        activity = client.get("/logs", params={"tab": "activity"})

    assert runs.status_code == 200
    assert activity.status_code == 200
    assert "Runs" in runs.text and "Activity" in runs.text
    assert "Runs" in activity.text and "Activity" in activity.text
    assert 'aria-selected="true"' in runs.text
    assert 'aria-selected="true"' in activity.text


def test_logs_filters_by_task_status_days_and_search(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        store = Store(client.app.state.data_dir)
        old_task = store.add_task("old-task", "https://example.test/old.git")
        new_task = store.add_task("new-task", "https://example.test/new.git")
        old_run = _run(store, old_task, "old message", "failed")
        new_run = _run(store, new_task, "needle message", "ok")
        old_at = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
        with sqlite3.connect(store.path) as conn:
            conn.execute(
                "UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
                (old_at, old_at, old_run),
            )

        response = client.get(
            "/logs",
            params={
                "tab": "runs",
                "task_id": str(new_task),
                "status": "ok",
                "days": "7",
                "search": "needle",
            },
        )

    assert response.status_code == 200
    assert "needle message" in response.text
    assert "old message" not in response.text
    assert f'data-run-id="{new_run}"' in response.text


def test_logs_search_sql_injection_string_does_not_return_all_rows(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        store = Store(client.app.state.data_dir)
        task_id = store.add_task("search-task", "https://example.test/search.git")
        _run(store, task_id, "first message")
        _run(store, task_id, "second message")
        response = client.get("/logs", params={"search": "' OR 1=1 --"})

    assert response.status_code == 200
    assert 'data-run-id="' not in response.text
    assert "Showing 0–0 of 0" in response.text


def test_logs_expands_steps_refs_and_raw_in_nested_details(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        store = Store(client.app.state.data_dir)
        task_id = store.add_task("details-task", "https://example.test/details.git")
        run_id = _run(store, task_id, "run summary")
        logs.add_step(
            store,
            run_id,
            "fetch",
            "fetch completed",
            duration_ms=1250,
            raw="git stdout\ngit stderr",
        )
        logs.add_ref(store, run_id, "refs/heads/main", "fast-forward", "old-oid", "new-oid")
        response = client.get("/logs", params={"run_id": str(run_id)})

    assert response.status_code == 200
    assert f'data-run-id="{run_id}"' in response.text
    assert "fetch completed" in response.text
    assert "1.25 s" in response.text or "1250 ms" in response.text
    assert "refs/heads/main" in response.text
    assert "git stdout" in response.text and "git stderr" in response.text
    assert response.text.count("<details") >= 2


def test_logs_paginates_at_twenty_rows(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        store = Store(client.app.state.data_dir)
        task_id = store.add_task("page-task", "https://example.test/page.git")
        for index in range(21):
            _run(store, task_id, f"page-message-{index}")
        first = client.get("/logs", params={"page": "1"})
        second = client.get("/logs", params={"page": "2"})

    assert first.status_code == 200 and second.status_code == 200
    assert first.text.count('data-run-id="') == 20
    assert second.text.count('data-run-id="') == 1
    assert "page-message-0" in second.text
    assert "page-message-20" in first.text


def test_logs_page_never_renders_known_secret(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        store = Store(client.app.state.data_dir)
        secret = "logs-secret-do-not-render"
        cred_id = store.add_cred("logs-credential", "https", username="bot", secret=secret)
        task_id = store.add_task(
            "secret-task",
            "https://example.test/secret.git",
            source_cred_id=cred_id,
        )
        run_id = _run(store, task_id, f"message={secret}")
        logs.add_step(store, run_id, "fetch", f"raw={secret}", raw=f"stderr={secret}")
        logs.add_activity(store, "task.run", "secret-task", f"detail={secret}", "127.0.0.1")
        runs = client.get("/logs", params={"tab": "runs"})
        activity = client.get("/logs", params={"tab": "activity"})

    assert runs.status_code == 200 and activity.status_code == 200
    assert secret not in runs.text
    assert secret not in activity.text


def test_every_recorded_post_action_has_a_translated_label():
    from gitwarden.i18n import translate
    from gitwarden.ui_logs import _ACTIVITY_KEYS
    from gitwarden.web import _activity_name

    paths = [
        "/login", "/logout", "/instances", "/instances/1/delete", "/accesses",
        "/accesses/1", "/accesses/1/preview", "/accesses/1/discover", "/accesses/1/delete",
        "/tasks/new", "/tasks/bulk", "/tasks/1/edit", "/tasks/1/run", "/tasks/1/toggle",
        "/tasks/1/detailed", "/tasks/1/targets", "/tasks/1/targets/provision",
        "/targets/1/retry", "/targets/1/audit", "/targets/1/bind", "/targets/1/delete",
        "/creds", "/creds/1/delete",
    ]  # fmt: skip
    for path in paths:
        key = _ACTIVITY_KEYS.get(_activity_name(path))
        assert key, path
        assert translate(key, "en") != key
