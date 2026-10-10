import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from gitwarden import logs
from gitwarden.db import Store
from gitwarden.i18n import msg, render


def _task(store: Store) -> int:
    return store.add_task("log-task", "https://example.test/log.git")


@pytest.mark.parametrize("secret", ['token-"quoted"-\\path', '"', "\\"])
def test_serialized_status_redacts_secrets_before_storage_without_breaking_translation(
    tmp_path, secret
):
    store = Store(tmp_path / "data")
    store.add_cred("test", "https", secret=secret)
    run_id = store.start_run(_task(store))
    message = msg("run.audit_alert", missing=[secret], modified=[])

    logs.add_step(store, run_id, "audit", message)

    stored = logs.list_steps(store, run_id)[0]["message"]
    assert render(stored, "en") == render(
        msg("run.audit_alert", missing=["***"], modified=[]), "en"
    )
    assert render(stored, "zh_CN") == render(
        msg("run.audit_alert", missing=["***"], modified=[]), "zh_CN"
    )


def test_step_and_activity_redact_secrets_and_bound_raw_by_utf8_bytes(tmp_path):
    store = Store(tmp_path / "data")
    secret = "token-秘密-123"
    cred_id = store.add_cred("source", "https", username="bot", secret=secret)
    task_id = store.add_task("log-task", "https://example.test/log.git", source_cred_id=cred_id)
    run_id = store.start_run(task_id)
    raw = ("进度 " + secret + " ") + ("界" * 40000)

    logs.add_step(
        store,
        run_id,
        "fetch",
        f"failed with {secret}",
        level="error",
        duration_ms=12,
        raw=raw,
    )
    logs.add_activity(store, "login_failed", "session", f"secret={secret}", "127.0.0.1")

    [step] = logs.list_steps(store, run_id)
    assert secret not in step["message"]
    assert secret not in step["raw"]
    assert len(step["raw"].encode("utf-8")) <= 64 * 1024
    assert re.search(r"…\[truncated \d+ bytes\]$", step["raw"])

    [activity] = logs.query_activity(store, page_size=20)[0]
    assert secret not in activity["detail"]


def test_query_runs_search_is_parameterized_and_matches_message_or_ref(tmp_path):
    store = Store(tmp_path / "data")
    task_id = _task(store)
    run_id = store.start_run(task_id)
    store.finish_run(run_id, "ok", "needle")
    logs.add_ref(store, run_id, "refs/heads/main", "force", "old", "new")

    rows, total = logs.query_runs(store, search="' OR 1=1 --")
    assert rows == []
    assert total == 0

    rows, total = logs.query_runs(store, search="needle")
    assert total == 1
    assert rows[0]["id"] == run_id

    rows, total = logs.query_runs(store, search="refs/heads/main")
    assert total == 1
    assert rows[0]["id"] == run_id


def test_cleanup_removes_old_runs_steps_refs_activity_but_keeps_events(tmp_path):
    store = Store(tmp_path / "data")
    task_id = _task(store)
    old_run = store.start_run(task_id)
    store.finish_run(old_run, "ok", "old")
    logs.add_step(store, old_run, "fetch", "old")
    logs.add_ref(store, old_run, "refs/heads/main", "force", "old", "new")
    logs.add_activity(store, "save", "task", "old", "127.0.0.1")
    store.add_event(task_id, "refs/heads/main", "force", "old", "new", None)
    recent_run = store.start_run(task_id)
    store.finish_run(recent_run, "ok", "recent")

    old_at = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE runs SET started_at=?, finished_at=? WHERE id=?",
            (old_at, old_at, old_run),
        )
        conn.execute("UPDATE run_steps SET at=? WHERE run_id=?", (old_at, old_run))
        conn.execute("UPDATE activity SET at=?", (old_at,))

    logs.cleanup(store, retention_days=90)

    assert [row["id"] for row in store.list_runs(task_id)] == [recent_run]
    assert logs.list_steps(store, old_run) == []
    assert logs.list_refs(store, old_run) == []
    assert logs.query_activity(store, page_size=20)[1] == 0
    assert len(store.list_events(task_id)) == 1
