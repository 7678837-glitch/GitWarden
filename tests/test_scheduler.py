import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gitwarden.db import Store
from gitwarden.scheduler import Scheduler


def _wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def _task(store: Store, tmp_path: Path, name: str, *, enabled: bool = False) -> int:
    return store.add_task(name, str(tmp_path / f"{name}.git"), enabled=enabled)


def test_scheduler_defaults_to_two_workers_and_rejects_invalid_pool_sizes(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "data")

    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01)
    assert scheduler.status()["workers"] == 2

    with pytest.raises(ValueError):
        Scheduler(store, tmp_path / "data", workers=0)
    with pytest.raises(ValueError):
        Scheduler(store, tmp_path / "data", workers=5)


def test_trigger_deduplicates_and_retrigger_moves_queued_task_to_front(
    tmp_path: Path, monkeypatch
) -> None:
    store = Store(tmp_path / "data")
    blocked = _task(store, tmp_path, "blocked")
    second = _task(store, tmp_path, "second")
    third = _task(store, tmp_path, "third")
    calls: list[int] = []
    started = threading.Event()
    release = threading.Event()

    def fake_run(store_arg: Store, _data_dir: Path, task_id: int) -> None:
        calls.append(task_id)
        if task_id == blocked:
            started.set()
            release.wait(5)
        store_arg.finish_task(task_id, "ok")

    monkeypatch.setattr("gitwarden.scheduler.run_task", fake_run)
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=1)
    scheduler.start()
    try:
        scheduler.trigger(blocked)
        _wait_for(started.is_set)
        scheduler.trigger(second)
        scheduler.trigger(third)
        scheduler.trigger(second)
        release.set()
        _wait_for(lambda: len(calls) >= 3)
    finally:
        release.set()
        scheduler.stop()

    assert calls[:3] == [blocked, second, third]


def test_two_workers_do_not_block_independent_tasks(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "data")
    first = _task(store, tmp_path, "first")
    second = _task(store, tmp_path, "second")
    started: set[int] = set()
    active = 0
    maximum_active = 0
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def fake_run(store_arg: Store, _data_dir: Path, task_id: int) -> None:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            started.add(task_id)
            if started == {first, second}:
                both_started.set()
        release.wait(5)
        with lock:
            active -= 1
        store_arg.finish_task(task_id, "ok")

    monkeypatch.setattr("gitwarden.scheduler.run_task", fake_run)
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=2)
    scheduler.start()
    try:
        scheduler.trigger(first)
        scheduler.trigger(second)
        _wait_for(both_started.is_set)
        assert maximum_active == 2
    finally:
        release.set()
        scheduler.stop()


def test_same_task_is_never_run_concurrently_or_queued_twice(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "data")
    task_id = _task(store, tmp_path, "one")
    started = threading.Event()
    release = threading.Event()
    calls = 0
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def fake_run(store_arg: Store, _data_dir: Path, _task_id: int) -> None:
        nonlocal calls, active, maximum_active
        with lock:
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
            started.set()
        release.wait(5)
        with lock:
            active -= 1
        store_arg.finish_task(task_id, "ok")

    monkeypatch.setattr("gitwarden.scheduler.run_task", fake_run)
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=2)
    scheduler.start()
    try:
        scheduler.trigger(task_id)
        _wait_for(started.is_set)
        scheduler.trigger(task_id)
        scheduler.trigger(task_id)
        time.sleep(0.05)
        assert calls == 1
        assert maximum_active == 1
    finally:
        release.set()
        scheduler.stop()


def test_due_tasks_are_sorted_by_earliest_due_and_scheduled_from_finish_time(
    tmp_path: Path, monkeypatch
) -> None:
    store = Store(tmp_path / "data")
    late = _task(store, tmp_path, "late", enabled=True)
    early = _task(store, tmp_path, "early", enabled=True)
    now = datetime.now(UTC)
    store.update_task(late, next_run_at=(now - timedelta(minutes=1)).isoformat())
    store.update_task(early, next_run_at=(now - timedelta(minutes=3)).isoformat())
    calls: list[int] = []

    def fake_run(store_arg: Store, _data_dir: Path, task_id: int) -> None:
        calls.append(task_id)
        time.sleep(0.03)
        store_arg.finish_task(task_id, "ok")

    monkeypatch.setattr("gitwarden.scheduler.run_task", fake_run)
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=1)
    scheduler.start()
    try:
        _wait_for(lambda: len(calls) >= 2)
    finally:
        scheduler.stop()

    assert calls[:2] == [early, late]
    task = store.get_task(early)
    assert task["last_run_at"] is not None
    assert task["next_run_at"] is not None
    assert datetime.fromisoformat(task["next_run_at"]) > datetime.fromisoformat(task["last_run_at"])


def test_effective_interval_uses_recent_seconds_and_failure_backoff(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    scheduler = Scheduler(store, tmp_path / "data")
    task = {
        "interval_minutes": 5,
        "recent_durations": [180.0, 240.0, 300.0],
        "consecutive_failures": 0,
    }

    assert scheduler.effective_interval(task) == pytest.approx(8.0)
    assert scheduler.effective_interval({**task, "recent_durations": [2.0, 3.0, 4.0]}) == 5
    assert (
        scheduler.effective_interval({**task, "recent_durations": [], "consecutive_failures": 1})
        == 5
    )
    assert (
        scheduler.effective_interval({**task, "recent_durations": [], "consecutive_failures": 2})
        == 10
    )
    assert (
        scheduler.effective_interval({**task, "recent_durations": [], "consecutive_failures": 3})
        == 20
    )
    assert (
        scheduler.effective_interval({**task, "recent_durations": [], "consecutive_failures": 99})
        == 360
    )


def test_due_access_is_discovered_on_its_interval(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path / "data")
    cred_id = store.add_cred("api", "https", username="u", secret="s")
    instance_id = store.add_instance("forge", "gitea", "https://git.example.test")
    access_id = store.add_access(instance_id, "access", cred_id)
    store.update_access(
        access_id,
        next_discovery_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    discovered: list[int] = []
    monkeypatch.setattr(
        "gitwarden.scheduler.discover",
        lambda _store, current_access_id: discovered.append(current_access_id),
        raising=False,
    )

    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=1)
    scheduler.start()
    try:
        _wait_for(lambda: discovered == [access_id])
    finally:
        scheduler.stop()


def test_status_reports_running_workers_queue_and_overdue_seconds(
    tmp_path: Path, monkeypatch
) -> None:
    store = Store(tmp_path / "data")
    task_id = _task(store, tmp_path, "running")
    started = threading.Event()
    release = threading.Event()

    def fake_run(store_arg: Store, _data_dir: Path, current_task_id: int) -> None:
        started.set()
        release.wait(5)
        store_arg.finish_task(current_task_id, "ok")

    monkeypatch.setattr("gitwarden.scheduler.run_task", fake_run)
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=2)
    scheduler.start()
    try:
        scheduler.trigger(task_id)
        _wait_for(started.is_set)
        status = scheduler.status()
        assert status["running"] is True
        assert status["workers"] == 2
        assert status["queue_length"] == 0
        assert status["most_overdue_seconds"] >= 0
    finally:
        release.set()
        scheduler.stop()


def test_failed_discovery_is_recorded_in_activity(tmp_path: Path, monkeypatch) -> None:
    from gitwarden import logs

    store = Store(tmp_path / "data")
    cred_id = store.add_cred("api", "https", username="u", secret="s3cret-token")
    instance_id = store.add_instance("forge", "gitea", "https://git.example.test")
    access_id = store.add_access(instance_id, "access", cred_id)

    def fail(_store, _access_id):
        raise RuntimeError("HTTP 401 for s3cret-token")

    monkeypatch.setattr("gitwarden.scheduler.discover", fail)
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=1)
    scheduler.start()
    try:
        _wait_for(lambda: store.get_access(access_id)["consecutive_failures"] >= 1)
    finally:
        scheduler.stop()

    rows, _ = logs.query_activity(store)
    assert rows[0]["action"] == "discovery.failed"
    assert rows[0]["object"] == "access"
    assert "s3cret-token" not in rows[0]["detail"]


def test_stop_returns_after_join_limit_when_worker_is_stuck(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    store = Store(tmp_path / "data")
    task_id = _task(store, tmp_path, "stuck")
    started = threading.Event()
    release = threading.Event()

    def fake_run(store_arg: Store, _data_dir: Path, task_id: int) -> None:
        started.set()
        release.wait(30)

    monkeypatch.setattr("gitwarden.scheduler.run_task", fake_run)
    monkeypatch.setattr("gitwarden.scheduler._STOP_JOIN_SECONDS", 0.3)
    scheduler = Scheduler(store, tmp_path / "data", tick_seconds=0.01, workers=1)
    scheduler.start()
    try:
        scheduler.trigger(task_id)
        _wait_for(started.is_set)
        begin = time.monotonic()
        with caplog.at_level("WARNING", logger="gitwarden"):
            scheduler.stop()
        assert time.monotonic() - begin < 3
        assert any("worker" in record.getMessage() for record in caplog.records)
    finally:
        release.set()


@pytest.mark.parametrize("has_good_target", [False, True])
def test_target_failures_do_not_back_off_source_capture(tmp_path, has_good_target):
    from gitwarden.scheduler import _WorkItem
    from tests.test_runner import Env

    env = Env(tmp_path)
    _, _, bad_hist = env.add_target("bad")
    if has_good_target:
        env.add_target("good")
    bad_hist.rename(tmp_path / "offline-history.git")
    env.store.update_task(env.task_id, interval_minutes=5, consecutive_failures=2)
    scheduler = Scheduler(env.store, env.data)
    for _ in range(2):
        scheduler._execute(_WorkItem("task", env.task_id, datetime.now(UTC)))
        task = env.store.get_task(env.task_id)
        assert task["consecutive_failures"] == 0
        assert task["recent_durations"]
        assert task["last_status"] == ("partial" if has_good_target else "failed")
        delay = datetime.fromisoformat(task["next_run_at"]) - datetime.fromisoformat(
            task["last_run_at"]
        )
        assert abs(delay.total_seconds() - 300) <= 1


@pytest.mark.parametrize("failures", [0, 2])
def test_busy_run_keeps_source_failure_count(tmp_path, failures):
    from gitwarden.archive import _locked, init_archive
    from gitwarden.runner import archive_path
    from gitwarden.scheduler import _WorkItem
    from tests.test_runner import Env

    env = Env(tmp_path)
    env.store.update_task(env.task_id, consecutive_failures=failures, recent_durations=[10])
    archive = archive_path(env.data, env.store.get_task(env.task_id))
    init_archive(archive)
    scheduler = Scheduler(env.store, env.data)
    with _locked(archive):
        scheduler._execute(_WorkItem("task", env.task_id, datetime.now(UTC)))
    task = env.store.get_task(env.task_id)
    assert task["last_status"] == "busy"
    assert task["consecutive_failures"] == failures
    assert task["recent_durations"] == [10]


def test_source_failure_backoff_recovers_after_success(tmp_path):
    from gitwarden.scheduler import _WorkItem
    from tests.test_runner import Env

    env = Env(tmp_path)
    env.store.update_task(env.task_id, source_url=str(tmp_path / "unreachable.git"))
    scheduler = Scheduler(env.store, env.data)
    item = _WorkItem("task", env.task_id, datetime.now(UTC))
    for failures in (1, 2):
        scheduler._execute(item)
        assert env.store.get_task(env.task_id)["consecutive_failures"] == failures
    env.store.update_task(env.task_id, source_url=str(env.src))
    scheduler._execute(item)
    assert env.store.get_task(env.task_id)["consecutive_failures"] == 0
