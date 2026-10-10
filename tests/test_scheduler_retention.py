import time
from datetime import UTC, datetime, timedelta

from gitwarden import logs
from gitwarden.db import Store
from gitwarden.scheduler import Scheduler


def test_scheduler_cleans_expired_logs_once_per_day_and_keeps_events(tmp_path, monkeypatch):
    store = Store(tmp_path)
    task = store.add_task("old", str(tmp_path / "source.git"), enabled=False)
    run = store.start_run(task)
    store.finish_run(run, "ok")
    old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
    store._exec("UPDATE runs SET started_at=?,finished_at=? WHERE id=?", (old, old, run))
    store.add_event(task, "refs/heads/main", "force", "old", "new", None)
    cleaned = []
    original = logs.cleanup

    def cleanup(store_arg, retention_days=90):
        cleaned.append(retention_days)
        return original(store_arg, retention_days)

    monkeypatch.setattr(logs, "cleanup", cleanup)
    scheduler = Scheduler(store, tmp_path, tick_seconds=0.01, retention_days=90)
    scheduler.start()
    try:
        deadline = time.monotonic() + 3
        while store.list_runs(task) and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.05)
    finally:
        scheduler.stop()
    assert not store.list_runs(task)
    assert len(store.list_events(task)) == 1
    assert cleaned == [90]
