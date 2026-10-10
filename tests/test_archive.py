import fcntl
from pathlib import Path

import pytest

from gitwarden import archive as arch
from gitwarden.archive import ArchiveBusy, capture, init_archive
from gitwarden.git import GitError, run
from tests.conftest import ID, commit, make_source


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    src = make_source(tmp_path / "src")
    ar = tmp_path / "archive.git"
    init_archive(ar)
    return src, ar


def cap(ar: Path, src: Path) -> arch.CaptureResult:
    return capture(ar, str(src), "src1")


def ref(repo: Path, name: str) -> str | None:
    p = run(repo, "rev-parse", "-q", "--verify", name, check=False)
    return p.stdout.strip() if p.returncode == 0 else None


def preserved(ar: Path) -> list[tuple[str, str, str]]:
    """[(保留引用名, 直接对象 OID, 说明)]"""
    out = run(
        ar, "for-each-ref", "refs/preserved", "--format=%(refname)%00%(object)%00%(contents)%01"
    ).stdout
    return [tuple(r.split("\x00")) for r in out.split("\x01\n") if r]  # type: ignore[misc]


def test_first_capture_mirrors_source_without_preserved(env) -> None:
    src, ar = env
    c1 = commit(src, "a")
    run(src, "tag", "light")
    run(src, *ID, "tag", "-a", "-m", "m", "annot")

    res = cap(ar, src)

    assert ref(ar, "refs/heads/main") == c1
    assert ref(ar, "refs/tags/light") == c1
    assert ref(ar, "refs/tags/annot") == ref(src, "refs/tags/annot")
    assert preserved(ar) == []
    assert {e.kind for e in res.events} == {"create"}


def test_fast_forward_creates_no_preserved(env) -> None:
    src, ar = env
    commit(src, "a")
    cap(ar, src)
    c2 = commit(src, "b")

    res = cap(ar, src)

    assert ref(ar, "refs/heads/main") == c2
    assert preserved(ar) == []
    assert res.events[0].kind == "fast-forward"


def test_force_push_preserves_old_commit(env) -> None:
    src, ar = env
    commit(src, "a")
    c2 = commit(src, "b")
    cap(ar, src)
    run(src, "reset", "--hard", "HEAD~1")
    c3 = commit(src, "c")

    res = cap(ar, src)

    assert ref(ar, "refs/heads/main") == c3
    [(name, obj, text)] = preserved(ar)
    assert obj == c2
    assert res.events[0].preserved_ref == name
    assert "ref: refs/heads/main\n" in text
    assert f"old: {c2}\n" in text
    assert f"new: {c3}\n" in text
    assert "event: force\n" in text
    assert "source: src1\n" in text


def test_delete_branch_preserves_old_commit(env) -> None:
    src, ar = env
    commit(src, "a")
    run(src, "checkout", "-b", "feat")
    c2 = commit(src, "b")
    run(src, "checkout", "main")
    cap(ar, src)
    run(src, "branch", "-D", "feat")

    cap(ar, src)

    assert ref(ar, "refs/heads/feat") is None
    [(_, obj, text)] = preserved(ar)
    assert obj == c2
    assert "ref: refs/heads/feat\n" in text
    assert "new: deleted\n" in text
    assert "event: delete\n" in text


def test_annotated_tag_rewrite_preserves_old_tag_object(env) -> None:
    src, ar = env
    commit(src, "a")
    run(src, *ID, "tag", "-a", "-m", "m", "v1")
    old_tag = ref(src, "refs/tags/v1")
    cap(ar, src)
    commit(src, "b")
    run(src, *ID, "tag", "-f", "-a", "-m", "m2", "v1")

    cap(ar, src)

    [(_, obj, text)] = preserved(ar)
    assert obj == old_tag
    assert run(ar, "cat-file", "-t", obj).stdout.strip() == "tag"
    assert run(ar, "cat-file", "tag", obj).stdout == run(src, "cat-file", "tag", old_tag).stdout
    assert "event: tag-rewrite\n" in text


def test_lightweight_tag_rewrite_preserves_old_commit(env) -> None:
    src, ar = env
    c1 = commit(src, "a")
    run(src, "tag", "v1")
    cap(ar, src)
    commit(src, "b")
    run(src, "tag", "-f", "v1")

    cap(ar, src)

    [(_, obj, _text)] = preserved(ar)
    assert obj == c1


def test_crash_between_phases_is_recovered_without_duplicate(env, monkeypatch) -> None:
    src, ar = env
    commit(src, "a")
    c2 = commit(src, "b")
    cap(ar, src)
    run(src, "reset", "--hard", "HEAD~1")
    c3 = commit(src, "c")

    def boom(*_a, **_k):
        raise RuntimeError("crash")

    with monkeypatch.context() as m:
        m.setattr(arch, "_commit_mirror", boom)
        with pytest.raises(RuntimeError):
            cap(ar, src)
    assert ref(ar, "refs/heads/main") == c2
    assert len(preserved(ar)) == 1

    cap(ar, src)

    assert ref(ar, "refs/heads/main") == c3
    assert len(preserved(ar)) == 1


def test_preserved_objects_survive_gc_and_fsck_passes(env) -> None:
    src, ar = env
    commit(src, "a")
    run(src, *ID, "tag", "-a", "-m", "m", "v1")
    cap(ar, src)
    run(src, *ID, "commit", "--amend", "--allow-empty", "-m", "a2")
    run(src, *ID, "tag", "-f", "-a", "-m", "m2", "v1")
    cap(ar, src)

    run(ar, "gc", "--prune=now")

    entries = preserved(ar)
    assert len(entries) == 2
    for _, obj, _ in entries:
        assert run(ar, "cat-file", "-e", obj, check=False).returncode == 0
    run(ar, "fsck", "--strict")


def test_unreachable_source_raises_and_keeps_mirror(env, tmp_path: Path) -> None:
    src, ar = env
    c1 = commit(src, "a")
    cap(ar, src)

    with pytest.raises(GitError):
        capture(ar, str(tmp_path / "missing"), "src1")

    assert ref(ar, "refs/heads/main") == c1


def test_same_branch_back_to_old_value_is_not_preserved_twice(env) -> None:
    src, ar = env
    commit(src, "base")
    o = commit(src, "o")
    cap(ar, src)

    run(src, "reset", "--hard", "HEAD~1")
    x = commit(src, "x")
    cap(ar, src)
    assert [p[1] for p in preserved(ar)] == [o]

    run(src, "reset", "--hard", o)
    cap(ar, src)
    assert sorted(p[1] for p in preserved(ar)) == sorted([o, x])

    run(src, "reset", "--hard", "HEAD~1")
    commit(src, "y")
    cap(ar, src)
    assert sorted(p[1] for p in preserved(ar)) == sorted([o, x])


def test_busy_archive_raises_archive_busy(env) -> None:
    src, ar = env
    commit(src, "a")
    with open(ar / "gitwarden.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ArchiveBusy):
            cap(ar, src)
