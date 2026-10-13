"""小型 worker 池调度任务和平台接入发现。"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gitwarden.db import Store
from gitwarden.discovery import discover
from gitwarden.git import execution_context
from gitwarden.runner import run_task

_MAX_INTERVAL_MINUTES = 6 * 60
_STOP_JOIN_SECONDS = 10


def effective_interval(task: dict[str, Any]) -> float:
    """返回包含耗时延长和失败退避的实际间隔，单位为分钟。"""

    configured = float(
        task.get(
            "interval_minutes",
            task.get("default_sync_interval_minutes", task.get("discover_interval_minutes", 60)),
        )
    )
    recent = [float(value) for value in (task.get("recent_durations") or [])[-3:]]
    if recent:
        configured = max(configured, 2 * (sum(recent) / len(recent)) / 60)
    failures = max(0, int(task.get("consecutive_failures") or 0))
    if failures > 1:
        configured *= 2 ** (failures - 1)
    return min(configured, _MAX_INTERVAL_MINUTES) if failures else configured


@dataclass(frozen=True)
class _WorkItem:
    kind: str
    object_id: int
    due_at: datetime
    manual: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.kind, self.object_id


def _at(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class Scheduler:
    def __init__(
        self,
        store: Store,
        data_dir: Path,
        tick_seconds: float = 30,
        *,
        workers: int = 2,
        stall_minutes: float = 10,
        retention_days: int = 90,
    ) -> None:
        if isinstance(workers, bool) or not 1 <= workers <= 4:
            raise ValueError("worker 数量必须在 1 到 4 之间")
        if isinstance(stall_minutes, bool) or stall_minutes <= 0:
            raise ValueError("卡死检测间隔必须大于 0 分钟")
        if isinstance(retention_days, bool) or retention_days < 1:
            raise ValueError("日志保留天数必须大于 0")
        if tick_seconds <= 0:
            raise ValueError("调度 tick 必须大于 0 秒")
        self.store = store
        self.data_dir = data_dir
        self.tick = tick_seconds
        self.workers = workers
        self.stall_minutes = stall_minutes
        self.retention_days = retention_days
        self._queue: list[_WorkItem] = []
        self._pending: set[tuple[str, int]] = set()
        self._running: set[tuple[str, int]] = set()
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker_threads: list[threading.Thread] = []
        self._started = False

    def effective_interval(self, task: dict[str, Any]) -> float:
        return effective_interval(task)

    def trigger(self, task_id: int) -> None:
        self._trigger("task", task_id)

    def trigger_discovery(self, access_id: int) -> None:
        self._trigger("access", access_id)

    def _trigger(self, kind: str, object_id: int) -> None:
        item = _WorkItem(kind, object_id, datetime.now(UTC), manual=True)
        with self._condition:
            key = item.key
            if key in self._running:
                return
            if key in self._pending:
                self._queue = [queued for queued in self._queue if queued.key != key]
            self._queue.insert(0, item)
            self._pending.add(key)
            self._condition.notify_all()

    def start(self) -> None:
        with self._condition:
            if self._started:
                return
            self._stop.clear()
            self._started = True
            self._worker_threads = [
                threading.Thread(
                    target=self._worker_loop,
                    name=f"gitwarden-worker-{index + 1}",
                    daemon=True,
                )
                for index in range(self.workers)
            ]
            self._thread = threading.Thread(
                target=self._loop,
                name="gitwarden-scheduler",
                daemon=True,
            )
            for worker in self._worker_threads:
                worker.start()
            self._thread.start()

    def stop(self) -> None:
        with self._condition:
            if not self._started:
                return
            self._stop.set()
            self._queue.clear()
            self._pending.clear()
            self._condition.notify_all()
            coordinator = self._thread
            workers = list(self._worker_threads)
        # worker 是 daemon 线程；档案写入为两阶段提交，被打断不会损坏，所以超时后直接返回。
        deadline = time.monotonic() + _STOP_JOIN_SECONDS
        for thread in [coordinator, *workers]:
            if thread is None:
                continue
            thread.join(max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                logging.getLogger("gitwarden").warning(
                    "停止调度器时 %s 仍在运行，已等待 %s 秒后放弃等待",
                    thread.name,
                    _STOP_JOIN_SECONDS,
                )
        with self._condition:
            self._thread = None
            self._worker_threads = []
            self._started = False

    def status(self) -> dict[str, Any]:
        with self._condition:
            queue_length = len(self._queue)
            running = self._started
        return {
            "running": running,
            "workers": self.workers,
            "queue_length": queue_length,
            "most_overdue_seconds": self._most_overdue_seconds(),
        }

    def _task_due(self, task: dict[str, Any], now: datetime) -> datetime:
        next_run = _at(task.get("next_run_at"))
        if next_run is not None:
            return next_run
        last_run = _at(task.get("last_run_at"))
        if last_run is not None:
            return last_run + timedelta(minutes=effective_interval(task))
        return now

    def _access_due(self, access: dict[str, Any], now: datetime) -> datetime:
        next_discovery = _at(access.get("next_discovery_at"))
        if next_discovery is not None:
            return next_discovery
        last_discovery = _at(access.get("last_discovery_at"))
        if last_discovery is not None:
            interval = {
                "interval_minutes": access["discover_interval_minutes"],
                "recent_durations": access.get("recent_durations"),
                "consecutive_failures": access.get("consecutive_failures"),
            }
            return last_discovery + timedelta(minutes=effective_interval(interval))
        return now

    def _due_items(self, now: datetime) -> list[_WorkItem]:
        items = [
            _WorkItem("task", task["id"], self._task_due(task, now))
            for task in self.store.list_tasks()
            if task["enabled"] and self._task_due(task, now) <= now
        ]
        items.extend(
            _WorkItem("access", access["id"], self._access_due(access, now))
            for access in self.store.list_accesses()
            if self._access_due(access, now) <= now
        )
        return sorted(items, key=lambda item: item.due_at)

    def _enqueue_due(self) -> None:
        now = datetime.now(UTC)
        with self._condition:
            for item in self._due_items(now):
                if item.key in self._pending or item.key in self._running:
                    continue
                position = len(self._queue)
                for index, queued in enumerate(self._queue):
                    if not queued.manual and queued.due_at > item.due_at:
                        position = index
                        break
                self._queue.insert(position, item)
                self._pending.add(item.key)
            if self._queue:
                self._condition.notify_all()

    def _loop(self) -> None:
        from gitwarden import logs

        next_cleanup = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_cleanup:
                try:
                    logs.cleanup(self.store, self.retention_days)
                except Exception:
                    logging.getLogger("gitwarden").exception("清理过期日志失败")
                next_cleanup = time.monotonic() + 86400
            self._enqueue_due()
            with self._condition:
                if self._stop.is_set():
                    return
                self._condition.wait(timeout=self.tick)

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stop.is_set():
                    self._condition.wait()
                if self._stop.is_set():
                    return
                item = self._queue.pop(0)
                self._pending.discard(item.key)
                self._running.add(item.key)
            try:
                self._execute(item)
            finally:
                with self._condition:
                    self._running.discard(item.key)
                    self._condition.notify_all()

    def _execute(self, item: _WorkItem) -> None:
        started = time.monotonic()
        error: Exception | None = None
        captured = False
        try:
            with execution_context(stall_minutes=self.stall_minutes):
                if item.kind == "task":
                    captured = run_task(self.store, self.data_dir, item.object_id)
                else:
                    discover(self.store, item.object_id)
        except Exception as exc:
            error = exc
            logging.getLogger("gitwarden").exception("%s %s 执行失败", item.kind, item.object_id)
            if item.kind == "access":
                from gitwarden import logs

                access = self.store.get_access(item.object_id)
                name = access["name"] if access else str(item.object_id)
                logs.add_activity(self.store, "discovery.failed", name, str(exc), None)
        duration = max(0.0, time.monotonic() - started)
        if item.kind == "task":
            self._finish_task(item.object_id, duration, error, captured)
        else:
            self._finish_access(item.object_id, duration, error)

    def _finish_task(
        self, task_id: int, duration: float, error: Exception | None, captured: bool | None
    ) -> None:
        before = self.store.get_task(task_id)
        if before is None:
            return
        if error is not None and before.get("last_status") != "failed":
            self.store.finish_task(task_id, "failed")
        after = self.store.get_task(task_id) or before
        durations = list(before.get("recent_durations") or [])
        failures = int(before.get("consecutive_failures") or 0)
        if error is None and captured:
            durations = (durations + [duration])[-3:]
            failures = 0
        elif error is not None or captured is False:
            failures += 1
        finished = datetime.now(UTC)
        interval = effective_interval(
            {
                **after,
                "recent_durations": durations,
                "consecutive_failures": failures,
            }
        )
        self.store.update_task(
            task_id,
            recent_durations=durations,
            consecutive_failures=failures,
            next_run_at=_iso(finished + timedelta(minutes=interval)),
        )

    def _finish_access(self, access_id: int, duration: float, error: Exception | None) -> None:
        access = self.store.get_access(access_id)
        if access is None:
            return
        success = error is None
        durations = list(access.get("recent_durations") or [])
        failures = int(access.get("consecutive_failures") or 0)
        if success:
            durations = (durations + [duration])[-3:]
            failures = 0
        else:
            failures += 1
        finished = datetime.now(UTC)
        interval = effective_interval(
            {
                "interval_minutes": access["discover_interval_minutes"],
                "recent_durations": durations,
                "consecutive_failures": failures,
            }
        )
        fields: dict[str, Any] = {
            "recent_durations": durations,
            "consecutive_failures": failures,
            "next_discovery_at": _iso(finished + timedelta(minutes=interval)),
        }
        if success:
            fields["last_discovery_at"] = _iso(finished)
        self.store.update_access(access_id, **fields)

    def _most_overdue_seconds(self) -> float:
        now = datetime.now(UTC)
        due: list[datetime] = []
        for task in self.store.list_tasks():
            if task["enabled"]:
                due.append(self._task_due(task, now))
        due.extend(self._access_due(access, now) for access in self.store.list_accesses())
        if not due:
            return 0.0
        return max(0.0, max((now - value).total_seconds() for value in due))
