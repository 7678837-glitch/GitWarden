import shutil
from pathlib import Path

import pytest

from gitwarden import target as tgt
from gitwarden.archive import capture, init_archive
from gitwarden.git import GitError, rev_parse, run
from gitwarden.target import (
    NotBound,
    Target,
    TargetMismatch,
    TargetNotEmpty,
    audit,
    bind,
    push_target,
)
from tests.conftest import ID, commit, make_source


class Env:
    def __init__(self, tmp: Path) -> None:
        self.src = make_source(tmp / "src")
        self.ar = tmp / "archive.git"
        init_archive(self.ar)
        run(None, "init", "--bare", str(tmp / "main.git"))
        run(None, "init", "--bare", str(tmp / "hist.git"))
        self.main = tmp / "main.git"
        self.hist = tmp / "hist.git"
        self.t = Target("t1", str(self.main), str(self.hist))

    def cap(self) -> None:
        capture(self.ar, str(self.src), "src1")

    def push(self) -> tgt.PushResult:
        return push_target(self.ar, self.t)

    def force_back(self, msg: str) -> str:
        run(self.src, "reset", "--hard", "HEAD~1")
        return commit(self.src, msg)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def bound(env: Env) -> Env:
    env.cap()
    bind(env.ar, env.t)
    return env


def refs(repo: Path, *prefixes: str) -> dict[str, str]:
    out = run(repo, "for-each-ref", "--format=%(refname) %(objectname)", *prefixes).stdout
    return dict(line.split(" ") for line in out.splitlines())


def preserved(ar: Path) -> dict[str, str]:
    return refs(ar, "refs/preserved")


def test_bind_rejects_non_empty_target(env: Env) -> None:
    commit(env.src, "a")
    env.cap()
    run(env.ar, "push", str(env.main), "refs/heads/main:refs/heads/main")
    with pytest.raises(TargetNotEmpty):
        bind(env.ar, env.t)

    env2 = Env(env.main.parent / "other")  # 历史仓非空
    env2.src = env.src
    run(env.ar, "push", str(env2.hist), "refs/heads/main:refs/heads/x")
    with pytest.raises(TargetNotEmpty):
        bind(env.ar, env2.t)


def test_push_requires_binding(env: Env) -> None:
    commit(env.src, "a")
    env.cap()
    with pytest.raises(NotBound):
        env.push()


def test_plain_sync(env: Env) -> None:
    c1 = commit(env.src, "a")
    run(env.src, "tag", "light")
    run(env.src, *ID, "tag", "-a", "-m", "m", "annot")
    bound(env)

    res = env.push()

    assert res.main_ok
    assert refs(env.main, "refs/heads", "refs/tags") == refs(env.ar, "refs/heads", "refs/tags")
    assert refs(env.main)["refs/heads/main"] == c1
    assert list(refs(env.hist)) == ["refs/tags/gw/binding"]


def test_force_push_goes_to_history_then_main(env: Env) -> None:
    c1 = commit(env.src, "a")
    c2 = commit(env.src, "b")
    bound(env)
    env.push()
    c3 = env.force_back("c")
    env.cap()

    res = env.push()

    assert res.main_ok and not res.gate_blocked
    [(pref, poid)] = preserved(env.ar).items()
    u = pref.removeprefix("refs/preserved/")
    assert refs(env.hist)[f"refs/tags/gw/{u}"] == poid
    assert res.history_pushed == [u]
    assert refs(env.main)["refs/heads/main"] == c3
    assert c1 != c2


def test_deleted_branch_removed_from_main(env: Env) -> None:
    commit(env.src, "a")
    run(env.src, "branch", "feat")
    bound(env)
    env.push()
    assert "refs/heads/feat" in refs(env.main)
    run(env.src, "branch", "-D", "feat")
    env.cap()

    res = env.push()

    assert res.main_ok
    assert "refs/heads/feat" not in refs(env.main)
    assert len(preserved(env.ar)) == 1


def test_rewritten_annotated_tag_released_by_exact_oid(env: Env) -> None:
    commit(env.src, "a")
    run(env.src, *ID, "tag", "-a", "-m", "v1", "rel")
    bound(env)
    env.push()
    old_tag_obj = refs(env.main)["refs/tags/rel"]
    run(env.src, *ID, "tag", "-f", "-a", "-m", "v2", "rel")
    env.cap()

    res = env.push()

    assert res.main_ok and not res.gate_blocked
    assert refs(env.main)["refs/tags/rel"] == refs(env.src)["refs/tags/rel"]
    [(pref, _)] = preserved(env.ar).items()
    u = pref.removeprefix("refs/preserved/")
    direct = run(env.hist, "for-each-ref", "--format=%(object)", f"refs/tags/gw/{u}").stdout.strip()
    assert direct == old_tag_obj


def test_gate_blocks_when_history_push_ineffective(env: Env, monkeypatch) -> None:
    commit(env.src, "a")
    commit(env.src, "b")
    bound(env)
    env.push()
    old = refs(env.main)["refs/heads/main"]
    env.force_back("c")
    env.cap()
    monkeypatch.setattr(tgt, "_push_history", lambda *a, **k: None)

    res = env.push()

    assert res.gate_blocked == ["refs/heads/main"]
    assert not res.main_ok
    assert refs(env.main)["refs/heads/main"] == old


def test_backlog_filled_after_history_outage(env: Env, tmp_path: Path) -> None:
    commit(env.src, "a")
    commit(env.src, "b")
    bound(env)
    env.push()
    old = refs(env.main)["refs/heads/main"]
    away = tmp_path / "away.git"
    shutil.move(env.hist, away)
    env.force_back("c")
    env.cap()
    env.force_back("d")
    env.cap()
    assert len(preserved(env.ar)) == 2

    with pytest.raises(GitError):
        env.push()
    assert refs(env.main)["refs/heads/main"] == old

    shutil.move(away, env.hist)
    res = env.push()

    assert res.main_ok
    assert len(res.history_pushed) == 2
    gw = {k for k in refs(env.hist) if k != "refs/tags/gw/binding"}
    assert len(gw) == 2
    assert refs(env.main)["refs/heads/main"] == refs(env.src)["refs/heads/main"]


def test_lost_ack_does_not_report_conflict(env: Env) -> None:
    c1 = commit(env.src, "a")
    bound(env)
    env.push()
    commit(env.src, "b")
    env.cap()
    env.push()
    record = "refs/pushed/t1/main/heads/main"
    run(env.ar, "update-ref", record, c1)

    res = env.push()

    assert res.main_ok and not res.main_conflicts
    assert refs(env.ar, record)[record] == refs(env.src)["refs/heads/main"]


def test_external_main_change_is_conflict_and_preserved(env: Env, tmp_path: Path) -> None:
    commit(env.src, "a")
    bound(env)
    env.push()
    third = tmp_path / "third"
    run(None, "clone", str(env.main), str(third))
    theirs = commit(third, "theirs")
    run(third, "push", "origin", "+HEAD:refs/heads/main")
    commit(env.src, "b")
    env.cap()

    res = env.push()

    assert res.main_conflicts == ["refs/heads/main"]
    assert not res.main_ok
    assert refs(env.main)["refs/heads/main"] == theirs


def test_history_name_collision_is_conflict(env: Env) -> None:
    commit(env.src, "a")
    commit(env.src, "b")
    bound(env)
    env.push()
    env.force_back("c")
    env.cap()
    [(pref, _)] = preserved(env.ar).items()
    u = pref.removeprefix("refs/preserved/")
    other = commit(env.src, "other")
    run(env.ar, "fetch", str(env.src), "HEAD")
    run(env.ar, "push", str(env.hist), f"{other}:refs/tags/gw/{u}")

    res = env.push()

    assert res.history_conflicts == [u]
    assert refs(env.hist)[f"refs/tags/gw/{u}"] == other
    assert not res.main_ok


def test_replaced_history_repo_is_mismatch(env: Env, tmp_path: Path) -> None:
    commit(env.src, "a")
    bound(env)
    shutil.rmtree(env.hist)
    run(None, "init", "--bare", str(env.hist))
    with pytest.raises(TargetMismatch):
        env.push()


def test_restore_from_target_only(env: Env, tmp_path: Path) -> None:
    commit(env.src, "a")
    commit(env.src, "b")
    run(env.src, "branch", "feat")
    run(env.src, *ID, "tag", "-a", "-m", "v1", "rel")
    bound(env)
    env.push()
    env.force_back("c")
    run(env.src, "branch", "-D", "feat")
    run(env.src, *ID, "tag", "-f", "-a", "-m", "v2", "rel")
    env.cap()
    assert env.push().main_ok
    assert len(preserved(env.ar)) == 3

    fresh = tmp_path / "fresh.git"
    run(None, "init", "--bare", str(fresh))
    run(fresh, "fetch", str(env.main), "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    run(fresh, "fetch", str(env.hist), "+refs/tags/gw/*:refs/tags/gw/*")

    for oid in preserved(env.ar).values():
        run(fresh, "cat-file", "-e", oid)
    for pref in preserved(env.ar):
        direct = run(env.ar, "for-each-ref", "--format=%(object)", pref).stdout.strip()
        run(fresh, "cat-file", "-e", direct)
    run(fresh, "fsck", "--no-dangling")


def test_audit_reports_missing_modified_unknown(env: Env) -> None:
    commit(env.src, "a")
    commit(env.src, "b")
    bound(env)
    env.push()
    env.force_back("c")
    env.cap()
    env.push()
    assert audit(env.ar, env.t) == tgt.AuditResult()
    [(pref, _)] = preserved(env.ar).items()
    u = pref.removeprefix("refs/preserved/")

    run(env.hist, "update-ref", "-d", f"refs/tags/gw/{u}")
    assert audit(env.ar, env.t).missing == [u]

    head = rev_parse(env.hist, "refs/tags/gw/binding")
    run(env.hist, "update-ref", "refs/tags/gw/ZZZ", head)
    assert audit(env.ar, env.t).unknown == ["ZZZ"]


@pytest.mark.parametrize("namespace", ["heads", "tags"])
@pytest.mark.parametrize("old_name,new_name", [("a", "a/b"), ("a/b", "a")])
def test_target_handles_ref_namespace_replacement_in_one_run(env, namespace, old_name, new_name):
    old = commit(env.src, "old")
    old_ref, new_ref = f"refs/{namespace}/{old_name}", f"refs/{namespace}/{new_name}"
    run(env.src, "update-ref", old_ref, old)
    bound(env)
    assert env.push().main_ok
    new = commit(env.src, "new")
    run(env.src, "update-ref", "-d", old_ref)
    run(env.src, "update-ref", new_ref, new)
    env.cap()

    result = env.push()

    assert result.main_ok and not result.main_conflicts
    assert old_ref not in refs(env.main)
    assert refs(env.main)[new_ref] == new
    records = refs(env.ar, "refs/pushed/t1/main/")
    assert f"refs/pushed/t1/main/{namespace}/{old_name}" not in records
    assert records[f"refs/pushed/t1/main/{namespace}/{new_name}"] == new
    assert env.push().main_ok


@pytest.mark.parametrize("change", ["force", "delete"])
@pytest.mark.parametrize("confirm_history", [True, False])
def test_target_catches_up_after_skipped_fast_forward(env, monkeypatch, change, confirm_history):
    first = commit(env.src, "first")
    bound(env)
    assert env.push().main_ok
    preserved_tip = commit(env.src, "unpublished fast forward")
    env.cap()
    if change == "force":
        run(env.src, "checkout", "--orphan", "replacement")
        replacement = commit(env.src, "unrelated")
        run(env.src, "update-ref", "refs/heads/main", replacement)
        run(env.src, "checkout", "main")
        run(env.src, "branch", "-D", "replacement")
    else:
        run(env.src, "update-ref", "-d", "refs/heads/main")
    env.cap()
    [pref] = preserved(env.ar)
    assert run(env.ar, "rev-parse", f"{pref}^{{}}").stdout.strip() == preserved_tip
    if not confirm_history:
        monkeypatch.setattr(tgt, "_push_history", lambda *args, **kwargs: None)

    result = env.push()

    if confirm_history:
        assert result.main_ok and not result.gate_blocked
        assert refs(env.main, "refs/heads") == refs(env.ar, "refs/heads")
    else:
        assert not result.main_ok
        assert result.gate_blocked == ["refs/heads/main"]
        assert refs(env.main)["refs/heads/main"] == first
