import re
from pathlib import Path

import pytest

from gitwarden.git import GitError, rev_parse, run

COMMIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def test_run_init_and_rev_parse(tmp_path: Path) -> None:
    run(tmp_path, "init")

    proc = run(tmp_path, "rev-parse", "--is-inside-work-tree")

    assert proc.returncode == 0
    assert proc.stdout.strip() == "true"


def test_failed_command_raises_git_error_with_details(tmp_path: Path) -> None:
    run(tmp_path, "init")

    with pytest.raises(GitError) as excinfo:
        run(tmp_path, "no-such-subcommand")

    err = excinfo.value
    assert err.returncode == 1
    assert "not a git command" in err.stderr
    assert list(err.args) == ["no-such-subcommand"]
    assert "returncode=1" in str(err)


def test_check_false_returns_failed_process(tmp_path: Path) -> None:
    run(tmp_path, "init")

    proc = run(tmp_path, "no-such-subcommand", check=False)

    assert proc.returncode == 1


def test_rev_parse_returns_none_for_missing_ref_and_oid_for_existing(tmp_path: Path) -> None:
    run(tmp_path, "init")
    assert rev_parse(tmp_path, "refs/heads/nope") is None

    run(tmp_path, *COMMIT_ID, "commit", "--allow-empty", "-m", "init")

    oid = rev_parse(tmp_path, "HEAD")
    assert oid is not None
    assert re.fullmatch(r"[0-9a-f]{40}", oid)


def test_git_error_message_prefix_is_english(tmp_path: Path) -> None:
    run(tmp_path, "init")

    with pytest.raises(GitError) as excinfo:
        run(tmp_path, "no-such-subcommand")

    assert str(excinfo.value).startswith("git command failed:")
