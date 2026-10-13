"""以参数列表调用 git，流式收集网络操作的进度并检测卡死。"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

_STDERR_LIMIT = 500
_NETWORK_COMMANDS = {"fetch", "push", "ls-remote"}
_OutputCallback = Callable[[str], None]
_Observer = Callable[[list[str], subprocess.CompletedProcess[str], int], None]


class GitError(Exception):
    """git 命令非零退出，保留调用参数和两路输出。"""

    def __init__(
        self,
        args: list[str],
        returncode: int,
        stderr: str,
        stdout: str = "",
    ) -> None:
        super().__init__(args, returncode, stderr, stdout)
        self.args = list(args)
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    def __str__(self) -> str:
        return (
            f"git command failed: args={list(self.args)!r}, returncode={self.returncode}, "
            f"stderr={self.stderr[-_STDERR_LIMIT:]}"
        )


@dataclass(frozen=True)
class _ExecutionContext:
    stall_minutes: float = 10
    on_output: _OutputCallback | None = None
    observer: _Observer | None = None


_DEFAULT_CONTEXT = _ExecutionContext()
_execution_context: ContextVar[_ExecutionContext] = ContextVar(
    "git_execution_context", default=_DEFAULT_CONTEXT
)


@contextmanager
def execution_context(
    *,
    stall_minutes: float | None = None,
    on_output: _OutputCallback | None = None,
    observer: _Observer | None = None,
) -> Iterator[None]:
    """设置当前线程的 git 执行选项；None 继承外层上下文。"""

    parent = _execution_context.get()
    current = _ExecutionContext(
        stall_minutes=parent.stall_minutes if stall_minutes is None else stall_minutes,
        on_output=parent.on_output if on_output is None else on_output,
        observer=parent.observer if observer is None else observer,
    )
    token = _execution_context.set(current)
    try:
        yield
    finally:
        _execution_context.reset(token)


def _network_command(args: tuple[str, ...]) -> tuple[int | None, str | None]:
    """找到命令本身，跳过命令前的 -c/--config 参数。"""

    index = 0
    while index < len(args):
        value = args[index]
        if value in {"-c", "--config"}:
            index += 2
            continue
        if value.startswith("-c") and value != "-c":
            index += 1
            continue
        if value.startswith("-"):
            index += 1
            continue
        if value in _NETWORK_COMMANDS:
            return index, value
        return None, None
    return None, None


def _command_args(args: tuple[str, ...]) -> tuple[list[str], str | None]:
    command_index, command = _network_command(args)
    values = list(args)
    if command in {"fetch", "push"} and "--progress" not in values:
        assert command_index is not None
        values.insert(command_index + 1, "--progress")
    return values, command


def _https_operation(args: tuple[str, ...], command: str | None) -> bool:
    if command not in _NETWORK_COMMANDS:
        return False
    return any(value.lower().startswith("https://") for value in args)


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        proc.kill()
    else:
        # 在回收进程前杀掉整个进程组：组长未回收时进程组号不会被复用，
        # ssh、git-remote-https 等子进程也一并结束。档案写入是两阶段提交，直接 SIGKILL 不会损坏。
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def _run_streamed(
    cmd: list[str],
    *,
    input: str | None,
    env: Mapping[str, str],
    command: str | None,
    context: _ExecutionContext,
) -> subprocess.CompletedProcess[str]:
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        bufsize=0,
        start_new_session=os.name != "nt",
    )
    if proc.stdin is not None:
        try:
            if input is not None:
                proc.stdin.write(input.encode())
            proc.stdin.close()
        except BrokenPipeError:
            proc.stdin.close()

    selector = selectors.DefaultSelector()
    streams: dict[object, str] = {}
    if proc.stdout is not None:
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        streams[proc.stdout] = "stdout"
    if proc.stderr is not None:
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        streams[proc.stderr] = "stderr"
    output: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    last_output = time.monotonic()
    stall_seconds = context.stall_minutes * 60 if command in _NETWORK_COMMANDS else None

    try:
        while streams or proc.poll() is None:
            timeout = 0.1
            if stall_seconds is not None:
                timeout = min(timeout, max(0.0, stall_seconds - (time.monotonic() - last_output)))
            events = selector.select(timeout)
            if not events:
                if stall_seconds is not None and time.monotonic() - last_output >= stall_seconds:
                    _terminate_process_group(proc)
                    output["stderr"].append(
                        f"Git stalled: no output for {stall_seconds:g} seconds\n".encode()
                    )
                    stall_seconds = None
                continue
            for key, _ in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        selector.unregister(stream)
                    except (KeyError, ValueError):
                        pass
                    streams.pop(stream, None)
                    stream.close()
                    continue
                kind = streams[stream]
                output[kind].append(chunk)
                last_output = time.monotonic()
                if kind == "stderr" and context.on_output is not None:
                    context.on_output(chunk.decode(errors="replace"))
    finally:
        selector.close()
        if proc.poll() is None:
            _terminate_process_group(proc)

    returncode = proc.wait()
    result = subprocess.CompletedProcess(
        cmd,
        returncode,
        b"".join(output["stdout"]).decode(errors="replace"),
        b"".join(output["stderr"]).decode(errors="replace"),
    )
    if context.observer is not None:
        context.observer(
            list(cmd[cmd.index("protocol.ext.allow=never") + 1 :]),
            result,
            int((time.monotonic() - started) * 1000),
        )
    return result


def run(
    repo: Path | None,
    *args: str,
    input: str | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """运行 git 并返回 CompletedProcess[str]；网络命令按进展检测卡死。"""

    original_args = tuple(args)
    command_args, command = _command_args(original_args)
    cmd = ["git"]
    if repo is not None:
        cmd += ["-C", str(repo)]
    cmd += ["-c", "protocol.ext.allow=never"]
    if _https_operation(original_args, command):
        cmd += ["-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=600"]
    cmd += command_args
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})}
    proc = _run_streamed(
        cmd,
        input=input,
        env=full_env,
        command=command,
        context=_execution_context.get(),
    )
    if check and proc.returncode != 0:
        raise GitError(list(original_args), proc.returncode, proc.stderr, proc.stdout)
    return proc


def rev_parse(repo: Path | None, rev: str) -> str | None:
    """返回 rev 对应的完整 OID；引用不存在时返回 None。其他失败照常抛错。"""

    proc = run(repo, "rev-parse", "-q", "--verify", rev, check=False)
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        raise GitError(
            ["rev-parse", "-q", "--verify", rev],
            proc.returncode,
            proc.stderr,
            proc.stdout,
        )
    return proc.stdout.strip()
