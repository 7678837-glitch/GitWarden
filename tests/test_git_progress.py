import socket
import threading
import time
from pathlib import Path

import pytest

from gitwarden.creds import git_env
from gitwarden.db import Store
from gitwarden.git import GitError, execution_context, run
from gitwarden.runner import archive_path
from gitwarden.scheduler import Scheduler


def _progress_upload_pack(path: Path, count: int = 12, delay: float = 0.03) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        f"i=0; while [ $i -lt {count} ]; do printf 'progress-%s\\n' \"$i\" >&2; "
        f"i=$((i + 1)); sleep {delay}; done\n"
        "exit 7\n"
    )
    path.chmod(0o755)
    return path


def test_git_error_retains_stdout_and_original_args(tmp_path: Path) -> None:
    run(tmp_path, "init")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("before\n")
    run(tmp_path, "add", "tracked.txt")
    run(
        tmp_path,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    tracked.write_text("after\n")

    with pytest.raises(GitError) as excinfo:
        run(tmp_path, "diff", "--exit-code")

    error = excinfo.value
    assert list(error.args) == ["diff", "--exit-code"]
    assert "after" in error.stdout
    assert isinstance(error.stderr, str)


def test_execution_context_streams_stderr_and_calls_observer(tmp_path: Path) -> None:
    repo = tmp_path / "repo.git"
    run(None, "init", "--bare", str(repo))
    upload_pack = _progress_upload_pack(tmp_path / "upload-pack")
    output: list[str] = []
    observed: list[tuple[list[str], int, int]] = []

    with execution_context(
        stall_minutes=1,
        on_output=output.append,
        observer=lambda args, proc, duration_ms: observed.append(
            (args, proc.returncode, duration_ms)
        ),
    ):
        proc = run(
            None,
            "ls-remote",
            f"--upload-pack={upload_pack}",
            f"file://{repo}",
            check=False,
        )

    assert proc.returncode != 0
    assert any("progress-" in chunk for chunk in output)
    assert observed and observed[0][0][:1] == ["ls-remote"]
    assert observed[0][1] == proc.returncode
    assert observed[0][2] >= 0


def test_nested_none_context_inherits_stall_and_kills_stalled_git(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    accepted = threading.Event()

    def hold_connection() -> None:
        conn = None
        try:
            conn, _ = listener.accept()
            accepted.set()
            while not stop.wait(0.05):
                pass
        except OSError:
            pass
        finally:
            if conn is not None:
                conn.close()

    thread = threading.Thread(target=hold_connection, daemon=True)
    thread.start()
    started = time.monotonic()
    try:
        with execution_context(stall_minutes=0.03):
            with execution_context(stall_minutes=None):
                with pytest.raises(GitError):
                    run(None, "ls-remote", f"http://127.0.0.1:{port}/repo.git")
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=1)

    assert accepted.is_set()
    assert elapsed < 8


def test_progress_output_keeps_a_long_git_operation_alive(tmp_path: Path) -> None:
    repo = tmp_path / "repo.git"
    run(None, "init", "--bare", str(repo))
    upload_pack = _progress_upload_pack(tmp_path / "upload-pack", count=30, delay=0.06)
    started = time.monotonic()

    with execution_context(stall_minutes=0.02):
        proc = run(
            None,
            "ls-remote",
            f"--upload-pack={upload_pack}",
            f"file://{repo}",
            check=False,
        )

    assert proc.returncode != 0
    assert time.monotonic() - started > 1.2
    assert "progress-29" in proc.stderr


def test_fetch_and_push_add_progress_but_ls_remote_does_not(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    run(None, "init", str(repo))
    missing = tmp_path / "missing.git"
    fetch_trace = tmp_path / "fetch-trace.json"
    push_trace = tmp_path / "push-trace.json"
    ls_remote_trace = tmp_path / "ls-remote-trace.json"

    with pytest.raises(GitError) as fetch_error:
        run(repo, "fetch", str(missing), env={"GIT_TRACE2_EVENT": str(fetch_trace)})
    with pytest.raises(GitError) as push_error:
        run(
            repo,
            "push",
            str(missing),
            "HEAD:refs/heads/main",
            env={"GIT_TRACE2_EVENT": str(push_trace)},
        )
    with pytest.raises(GitError) as ls_remote_error:
        run(None, "ls-remote", str(missing), env={"GIT_TRACE2_EVENT": str(ls_remote_trace)})

    assert fetch_error.value.returncode != 0
    assert push_error.value.returncode != 0
    assert ls_remote_error.value.returncode != 0
    assert '"--progress"' in fetch_trace.read_text()
    assert '"--progress"' in push_trace.read_text()
    assert '"--progress"' not in ls_remote_trace.read_text()


def test_https_network_commands_set_low_speed_limits(tmp_path: Path) -> None:
    trace = tmp_path / "https-trace.json"

    with pytest.raises(GitError):
        run(
            None,
            "ls-remote",
            "https://127.0.0.1:1/repo.git",
            env={"GIT_TRACE2_EVENT": str(trace)},
        )

    trace_text = trace.read_text()
    assert "http.lowSpeedLimit=1000" in trace_text
    assert "http.lowSpeedTime=600" in trace_text


def test_stalled_run_releases_archive_lock_for_the_next_task(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    accepted = threading.Event()

    def hold_connection() -> None:
        conn = None
        try:
            conn, _ = listener.accept()
            accepted.set()
            while not stop.wait(0.05):
                pass
        except OSError:
            pass
        finally:
            if conn is not None:
                conn.close()

    server_thread = threading.Thread(target=hold_connection, daemon=True)
    server_thread.start()
    data_dir = tmp_path / "data"
    store = Store(data_dir)
    stalled_id = store.add_task(
        "stalled",
        f"http://127.0.0.1:{port}/repo.git",
        enabled=False,
    )
    source = tmp_path / "source.git"
    run(None, "init", "--bare", str(source))
    healthy_id = store.add_task("healthy", str(source), enabled=False)
    scheduler = Scheduler(store, data_dir, tick_seconds=0.01, workers=1, stall_minutes=0.01)
    scheduler.start()
    try:
        scheduler.trigger(stalled_id)
        deadline = time.monotonic() + 5
        while not accepted.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert accepted.is_set()
        scheduler.trigger(healthy_id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            healthy = store.get_task(healthy_id)
            if healthy["last_status"] == "ok":
                break
            time.sleep(0.02)
    finally:
        scheduler.stop()
        stop.set()
        listener.close()
        server_thread.join(timeout=1)

    assert accepted.is_set()
    assert store.get_task(stalled_id)["last_status"] == "failed"
    assert store.get_task(healthy_id)["last_status"] == "ok"
    assert archive_path(data_dir, store.get_task(healthy_id)).exists()


def test_ssh_credentials_enable_keepalive_options(tmp_path: Path) -> None:
    cred = {
        "kind": "ssh",
        "private_key": "KEYDATA",
        "known_hosts": "host ssh-ed25519 AAAA",
    }

    with git_env(cred, tmp_path) as env:
        command = env["GIT_SSH_COMMAND"]

    assert "ServerAliveInterval=30" in command
    assert "ServerAliveCountMax=10" in command


def test_long_progress_keeps_the_latest_line(tmp_path: Path) -> None:
    from gitwarden.db import Store
    from gitwarden.runner import _progress_text
    from gitwarden.web import _progress_summary

    parts = [f"Receiving objects: {i % 100}% ({i}/5000)\r" for i in range(5000)]
    text = _progress_text(Store(tmp_path / "data"), parts, tmp_path, True)
    assert len(text.encode()) <= 65536
    assert _progress_summary(text) == "Receiving objects: 99% (4999/5000)"
