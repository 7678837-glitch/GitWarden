import os
import shlex
import stat

import pytest

from gitwarden.creds import check_url, git_env
from gitwarden.git import GitError, run
from tests.conftest import commit, make_source
from tests.http_git import PASSWORD, USER, serve


@pytest.fixture
def http_repo(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    run(None, "init", "--bare", str(root / "repo.git"))
    server, base = serve(root)
    yield base + "/repo.git"
    server.shutdown()


def https_cred(password=PASSWORD):
    return {"kind": "https", "username": USER, "secret": password}


def test_https_credentials_allow_push_and_fetch(tmp_path, http_repo):
    src = make_source(tmp_path / "src")
    oid = commit(src, "one")
    with git_env(https_cred(), tmp_path / "w") as env:
        run(src, "push", http_repo, "HEAD:refs/heads/main", env=env)
        out = run(None, "ls-remote", http_repo, env=env).stdout
    assert oid in out


def test_wrong_password_fails_without_leaking_password(tmp_path, http_repo):
    with git_env(https_cred("wrong-pw-123"), tmp_path / "w") as env:
        with pytest.raises(GitError) as exc:
            run(None, "ls-remote", http_repo, env=env)
    assert "wrong-pw-123" not in str(exc.value)
    assert PASSWORD not in str(exc.value)
    assert not any("wrong-pw-123" in a for a in exc.value.args)


def test_ssh_env_uses_private_key_file_and_cleans_up(tmp_path):
    cred = {"kind": "ssh", "private_key": "KEYDATA", "known_hosts": "host ssh-ed25519 AAAA"}
    with git_env(cred, tmp_path) as env:
        parts = shlex.split(env["GIT_SSH_COMMAND"])
        key = parts[parts.index("-i") + 1]
        kh = next(p for p in parts if p.startswith("UserKnownHostsFile=")).split("=", 1)[1]
        assert {"IdentitiesOnly=yes", "BatchMode=yes", "StrictHostKeyChecking=yes"} <= set(parts)
        assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(key)).st_mode) == 0o700
        assert open(kh).read().startswith("host ssh-ed25519")
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert not os.path.exists(os.path.dirname(key))


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.git",
        "http://example.com/a.git",
        "ssh://git@example.com/a.git",
        "git@example.com:org/a.git",
        "/srv/repos/a.git",
    ],
)
def test_check_url_accepts(url):
    check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.com/a.git",
        "https://user@example.com/a.git",
        "ext::sh -c evil",
        "file:///tmp/a.git",
        "git://example.com/a.git",
        "relative/path",
        "--upload-pack=evil",
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(ValueError):
        check_url(url)
