import shutil
import time

import pytest

from gitwarden.archive import capture
from gitwarden.db import Store
from gitwarden.git import rev_parse, run
from gitwarden.i18n import msg
from gitwarden.logs import list_refs, list_steps
from gitwarden.runner import archive_path, audit_target, bind_target, retry_target, run_task
from gitwarden.scheduler import Scheduler
from tests.conftest import ID, commit, make_source


class Env:
    def __init__(self, tmp):
        self.data = tmp / "data"
        self.store = Store(self.data)
        self.src = make_source(tmp / "src")
        commit(self.src, "one")
        commit(self.src, "two")
        self.task_id = self.store.add_task("t", str(self.src))
        self.tmp = tmp

    def add_target(self, name):
        main, hist = self.tmp / f"{name}-main.git", self.tmp / f"{name}-hist.git"
        run(None, "init", "--bare", str(main))
        run(None, "init", "--bare", str(hist))
        tid = self.store.add_target(self.task_id, name, str(main), str(hist))
        bind_target(self.store, self.data, tid)
        return tid, main, hist

    def run(self):
        run_task(self.store, self.data, self.task_id)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_bind_run_syncs_mirror(env):
    tid, main, _ = env.add_target("a")
    env.run()
    assert env.store.get_target(tid)["last_status"] == "ok"
    assert env.store.get_task(env.task_id)["last_status"] == "ok"
    assert run(main, "rev-parse", "main").stdout == run(env.src, "rev-parse", "main").stdout


def test_force_push_records_event_and_history_tag(env):
    tid, main, hist = env.add_target("a")
    env.run()
    run(env.src, "reset", "--hard", "HEAD~1")
    commit(env.src, "three")
    env.run()
    assert [e["kind"] for e in env.store.list_events(env.task_id)] == ["force"]
    assert env.store.get_target(tid)["last_status"] == "ok"
    assert "refs/tags/gw/" in run(hist, "for-each-ref", "refs/tags/gw/").stdout


def test_one_broken_target_does_not_block_others(env, tmp_path):
    bad, _, bad_hist = env.add_target("bad")
    good, _, _ = env.add_target("good")
    env.run()
    shutil.rmtree(bad_hist)
    env.run()
    assert env.store.get_target(bad)["last_status"] == "error"
    assert env.store.get_target(good)["last_status"] == "ok"
    assert env.store.get_task(env.task_id)["last_status"] == "partial"
    assert env.store.list_runs(env.task_id)[0]["status"] == "partial"


def test_run_logs_steps_refs_and_failed_target_stderr(env):
    bad, _, bad_hist = env.add_target("bad")
    env.add_target("good")
    env.run()
    run(env.src, "reset", "--hard", "HEAD~1")
    commit(env.src, "rewritten")
    shutil.rmtree(bad_hist)
    env.run()

    run_row = env.store.list_runs(env.task_id)[0]
    steps = list_steps(env.store, run_row["id"])
    refs = list_refs(env.store, run_row["id"])
    assert {
        "fetch",
        "capture",
        "preserve",
        "scan",
        "history",
        "gate",
        "mirror",
        "audit",
    } <= {step["step"] for step in steps}
    assert any(ref["kind"] == "force" for ref in refs)
    failed = [step for step in steps if step["target_id"] == bad and step["level"] == "error"]
    assert failed and any(step["raw"] for step in failed)
    assert any("fatal" in step["raw"].lower() for step in failed if step["raw"])


def test_first_run_records_initial_backup_and_received_progress(env):
    env.add_target("a")
    env.run()

    run_row = env.store.list_runs(env.task_id)[0]
    assert run_row["initial_backup"] == 1
    assert isinstance(run_row["progress"], str) and run_row["progress"]


def test_detailed_logging_keeps_raw_for_successful_steps(env):
    env.store.update_task(env.task_id, detailed_logging=True)
    env.add_target("a")
    env.run()

    run_row = env.store.list_runs(env.task_id)[0]
    steps = list_steps(env.store, run_row["id"])
    assert steps
    assert all(step["raw"] is not None for step in steps if step["level"] == "info")


def test_retry_target_only_pushes_selected_target_and_audit_updates_time(env):
    first, first_main, _ = env.add_target("first")
    _, second_main, _ = env.add_target("second")
    env.run()
    old_second = rev_parse(second_main, "refs/heads/main")

    run(env.src, "reset", "--hard", "HEAD~1")
    new_head = commit(env.src, "retry")
    capture(archive_path(env.data, env.store.get_task(env.task_id)), str(env.src), "t")

    retry_target(env.store, env.data, first)

    assert rev_parse(first_main, "refs/heads/main") == new_head
    assert rev_parse(second_main, "refs/heads/main") == old_second
    before = env.store.get_target(first)["last_audit_at"]
    audit_target(env.store, env.data, first)
    after = env.store.get_target(first)["last_audit_at"]
    assert after and after >= before


def test_changing_target_url_unbinds_and_skips_it(env, tmp_path):
    tid, _, _ = env.add_target("a")
    env.store.update_target(tid, main_url=str(tmp_path / "other.git"))
    assert env.store.get_target(tid)["bound"] == 0
    env.run()
    assert env.store.get_target(tid)["last_status"] is None
    assert env.store.get_task(env.task_id)["last_status"] == "ok"


def test_scheduler_trigger_runs_task_within_a_tick(env):
    env.store.update_task(env.task_id, enabled=False)
    sched = Scheduler(env.store, env.data, tick_seconds=0.1)
    sched.start()
    try:
        sched.trigger(env.task_id)
        deadline = time.time() + 10
        while time.time() < deadline and not env.store.list_runs(env.task_id):
            time.sleep(0.05)
    finally:
        sched.stop()
    assert archive_path(env.data, env.store.get_task(env.task_id)).exists()
    assert env.store.get_task(env.task_id)["last_status"] == "ok"


def test_deleting_referenced_cred_is_rejected(env):
    cid = env.store.add_cred("c", "https", username="u", secret="p")
    env.store.update_task(env.task_id, source_cred_id=cid)
    with pytest.raises(ValueError):
        env.store.delete_cred(cid)


def test_lfs_source_is_flagged_but_run_still_ok(env):
    (env.src / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    run(env.src, "add", ".gitattributes")
    run(env.src, *ID, "commit", "-m", "lfs")
    env.run()
    task = env.store.get_task(env.task_id)
    assert task["external_note"] == msg("ext.lfs", items="main")
    assert task["last_status"] == "ok"


def test_missing_managed_source_requires_complete_invisible_discovery(env):
    instance = env.store.add_instance("forge", "gitea", "https://forge.example.test")
    cred = env.store.add_cred("access", "https", username="bot", secret="test-secret")
    access = env.store.add_access(instance, "source", cred)
    env.store.update_task(env.task_id, instance_id=instance, access_id=access, platform_repo_id=123)
    env.store.update_task(env.task_id, source_url=str(env.tmp / "missing.git"))
    env.run()
    assert env.store.get_task(env.task_id)["managed_state"] == "active"
    env.store.update_access(access, last_discovery_summary={"complete": True, "seen_repo_ids": []})
    env.store.update_task(env.task_id, managed_state="not_visible")
    env.run()
    assert env.store.get_task(env.task_id)["managed_state"] == "source_gone"


def test_initial_backup_progress_is_visible_before_capture_finishes(env, monkeypatch):
    import gitwarden.runner as runner

    captured = runner.capture
    progress = []

    def capture_and_read(*args, **kwargs):
        result = captured(*args, **kwargs)
        progress.append(env.store.list_runs(env.task_id)[0]["progress"])
        return result

    monkeypatch.setattr(runner, "capture", capture_and_read)
    env.run()
    assert progress and progress[0]


@pytest.mark.parametrize("failure", ["git", "other"])
def test_offline_source_still_flushes_preserved_backlog(env, monkeypatch, failure):
    tid, main, hist = env.add_target("a")
    env.run()
    away = hist.with_name("offline-history.git")
    hist.rename(away)
    run(env.src, "reset", "--hard", "HEAD~1")
    new = commit(env.src, "rewritten while target offline")
    env.run()
    event = env.store.list_events(env.task_id)[0]
    away.rename(hist)
    if failure == "git":
        env.store.update_task(env.task_id, source_url=str(env.tmp / "unreachable.git"))
        cause = "unreachable.git"
    else:

        def fail_capture(*args, **kwargs):
            raise RuntimeError("source capture failed")

        monkeypatch.setattr("gitwarden.runner.capture", fail_capture)
        cause = "source capture failed"

    env.run()

    tag = event["preserved_ref"].replace("refs/preserved/", "refs/tags/gw/")
    assert rev_parse(hist, tag) is not None
    assert rev_parse(main, "refs/heads/main") == new
    assert env.store.get_target(tid)["last_status"] == "ok"
    row = env.store.list_runs(env.task_id)[0]
    assert row["status"] == "failed" and cause in row["message"]
    assert env.store.get_task(env.task_id)["last_status"] == "failed"
    assert any(step["target_id"] == tid for step in list_steps(env.store, row["id"]))


def test_maintenance_preserves_refs_and_objects_and_runs_at_most_weekly(env, monkeypatch):
    import os

    import gitwarden.runner as runner
    from gitwarden.archive import ArchiveBusy, _locked, init_archive

    archive = archive_path(env.data, env.store.get_task(env.task_id))
    init_archive(archive)
    capture(archive, str(env.src), "t")
    run(env.src, "reset", "--hard", "HEAD~1")
    capture(archive, str(env.src), "t")
    before = run(archive, "for-each-ref").stdout
    objects = run(archive, "for-each-ref", "--format=%(objectname)").stdout.splitlines()
    loose = run(archive, "hash-object", "-w", "--stdin", input="unreferenced object").stdout.strip()
    old = time.time() - 30 * 86400
    os.utime(archive / "objects" / loose[:2] / loose[2:], (old, old))
    gc_calls = []
    real_run = runner.run

    def observed_run(path, *args, **kwargs):
        if args[:1] == ("gc",):
            assert args == ("gc", "--no-prune")
            with pytest.raises(ArchiveBusy):
                with _locked(archive):
                    pass
            gc_calls.append(args)
        return real_run(path, *args, **kwargs)

    monkeypatch.setattr(runner, "run", observed_run)
    env.run()
    assert len(gc_calls) == 1
    assert run(archive, "for-each-ref").stdout == before
    for oid in [*objects, loose]:
        assert run(archive, "cat-file", "-e", oid).returncode == 0
    marker = archive / "gitwarden-maintenance"
    first_mtime = marker.stat().st_mtime
    env.run()
    assert len(gc_calls) == 1 and marker.stat().st_mtime == first_mtime
    expired = time.time() - 8 * 86400
    os.utime(marker, (expired, expired))
    env.run()
    assert len(gc_calls) == 2 and marker.stat().st_mtime > expired


def test_maintenance_failure_is_a_warning_and_does_not_fail_the_run(env, monkeypatch):
    import gitwarden.runner as runner
    from gitwarden.git import GitError

    real_run = runner.run

    def fail_gc(path, *args, **kwargs):
        if args[:1] == ("gc",):
            raise GitError(list(args), 1, "maintenance fixture failure")
        return real_run(path, *args, **kwargs)

    monkeypatch.setattr(runner, "run", fail_gc)
    env.run()
    latest = env.store.list_runs(env.task_id)[0]
    assert latest["status"] == "ok"
    assert any(
        step["level"] == "warn" and "maintenance fixture failure" in (step["message"] or "")
        for step in list_steps(env.store, latest["id"])
    )
    archive = archive_path(env.data, env.store.get_task(env.task_id))
    assert not (archive / "gitwarden-maintenance").exists()


@pytest.mark.parametrize("stalled", [False, True])
def test_failed_run_keeps_final_git_error_after_large_progress(env, monkeypatch, stalled):
    from gitwarden.git import execution_context

    upload_pack = env.tmp / "failing-upload-pack"
    upload_pack.write_text(
        "#!/bin/sh\n"
        "i=0; while [ $i -lt 4000 ]; do "
        "printf 'Receiving objects progress %s\\n' \"$i\" >&2; i=$((i+1)); done\n"
        + ("sleep 30\n" if stalled else "printf 'fatal: final transfer failure\\n' >&2; exit 7\n")
    )
    upload_pack.chmod(0o755)

    def failing_capture(*args, **kwargs):
        with execution_context(stall_minutes=0.03):
            run(None, "ls-remote", f"--upload-pack={upload_pack}", f"file://{env.src}")

    monkeypatch.setattr("gitwarden.runner.capture", failing_capture)
    env.run()
    latest = env.store.list_runs(env.task_id)[0]
    expected = "Git stalled" if stalled else "fatal: final transfer failure"
    assert latest["status"] == "failed"
    assert expected in latest["message"]
    capture_step = next(s for s in list_steps(env.store, latest["id"]) if s["step"] == "capture")
    assert expected in capture_step["raw"]
    assert capture_step["raw"].startswith("…[truncated ")
    assert len(capture_step["raw"].encode()) <= 65536


def test_run_task_returns_none_when_task_was_deleted(tmp_path):
    store = Store(tmp_path / "data")
    task_id = store.add_task("gone", str(tmp_path / "missing.git"), enabled=False)
    store.delete_task(task_id)
    assert run_task(store, tmp_path / "data", task_id) is None
