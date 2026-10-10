"""同步核心的线程隔离观测上下文。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from gitwarden.i18n import msg


@dataclass
class StepRecord:
    name: str
    duration_ms: int = 0
    failed: bool = False
    error_message: str | None = None
    _parts: list[str] = field(default_factory=list)

    @property
    def raw(self) -> str:
        return "".join(self._parts)

    def add_output(self, text: str) -> None:
        if text:
            self._parts.append(text)

    def add_command_output(self, text: str) -> None:
        if not text:
            return
        current = self.raw
        if current.endswith(text) or text in current[-len(text) :]:
            return
        self._parts.append(text)


_stack: ContextVar[tuple[StepRecord, ...]] = ContextVar("gitwarden_telemetry_stack", default=())
_collector: ContextVar[list[StepRecord] | None] = ContextVar(
    "gitwarden_telemetry_collector", default=None
)


@contextmanager
def collect() -> Iterator[list[StepRecord]]:
    records: list[StepRecord] = []
    token = _collector.set(records)
    try:
        yield records
    finally:
        _collector.reset(token)


@contextmanager
def step(name: str) -> Iterator[StepRecord]:
    record = StepRecord(name)
    token = _stack.set((*_stack.get(), record))
    started = time.monotonic()
    try:
        yield record
    finally:
        record.duration_ms = max(0, int((time.monotonic() - started) * 1000))
        _stack.reset(token)
        records = _collector.get()
        if records is not None:
            records.append(record)


def output(text: str) -> None:
    """接收 git 流式输出，并归入当前所有嵌套步骤。"""
    for record in _stack.get():
        record.add_output(text)


def command_output(proc: object, args: list[str] | None = None) -> None:
    """把一次 git 命令的完整输出补入步骤，兼容无流式回调的命令。"""
    stdout = getattr(proc, "stdout", None) or ""
    stderr = getattr(proc, "stderr", None) or ""
    command = None
    if args:
        index = 0
        while index < len(args):
            value = args[index]
            if value in {"-c", "--config"}:
                index += 2
                continue
            if value.startswith("-"):
                index += 1
                continue
            command = value
            break
    if getattr(proc, "returncode", 0) != 0 and command in {"fetch", "push", "ls-remote"}:
        for record in _stack.get():
            record.failed = True
            record.error_message = stderr or msg("run.git_failed", command=command)
    for text in (stdout, stderr):
        if not isinstance(text, str):
            continue
        for record in _stack.get():
            record.add_command_output(text)
