"""使用平台容器自带的 SSH 服务验证真实采集、推送和主机密钥校验。"""

import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from gitwarden.creds import git_env
from gitwarden.db import Store
from gitwarden.git import run
from gitwarden.runner import archive_path, bind_target, run_task
from tests.conftest import commit, make_source
from tests.integration.conftest import PlatformEnvironment

SSH_PORTS = {"gitea": 18322, "forgejo": 18323}


def scan_host_key(port):
    result = subprocess.run(
        ["ssh-keyscan", "-T", "5", "-t", "ed25519", "-p", str(port), "127.0.0.1"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "ssh-ed25519" in result.stdout
    return result.stdout


@pytest.fixture(params=["gitea", "forgejo"])
def ssh_forge(request, platforms, tmp_path):
    platform = platforms[request.param]
    # 包括 SQLite 内的私钥副本，成功或失败都由同一个临时目录统一清理。
    with tempfile.TemporaryDirectory(prefix="ssh-it-", dir=tmp_path) as directory:
        root = Path(directory)
        key = root / "id_ed25519"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        platform.api(
            "POST",
            "/user/keys",
            {
                "title": "ssh-it-" + uuid.uuid4().hex,
                "key": key.with_suffix(".pub").read_text(),
                "read_only": False,
            },
        )
        data = root / "data"
        store = Store(data)
        port = SSH_PORTS[platform.kind]
        cred_id = store.add_cred(
            "ssh-test", "ssh", private_key=key.read_text(), known_hosts=scan_host_key(port)
        )
        yield PlatformEnvironment(
            **vars(platform), root=root, data=data, store=store, cred_id=cred_id, port=port
        )
    assert not root.exists(), "SSH 测试目录和密钥应已清理"


def test_ssh_source_and_targets_preserve_force_pushed_history(ssh_forge):
    forge = ssh_forge
    name = "ssh-" + uuid.uuid4().hex[:10]
    repos = [
        forge.client.create_repo("gwadmin", name + suffix) for suffix in ("", "-main", "-history")
    ]
    source_url, main_url, history_url = [repo["ssh_url"] for repo in repos]
    for url in (source_url, main_url, history_url):
        assert url.startswith(f"ssh://git@127.0.0.1:{forge.port}/"), url
    local = make_source(forge.root / "source")
    commit(local, "one")
    old = commit(local, "two")
    with git_env(forge.store.get_cred(forge.cred_id), forge.data / "work") as env:
        run(local, "push", source_url, "main", env=env)
    task_id = forge.store.add_task(name, source_url, source_cred_id=forge.cred_id)
    target_id = forge.store.add_target(task_id, name, main_url, history_url, forge.cred_id)
    bind_target(forge.store, forge.data, target_id)
    run_task(forge.store, forge.data, task_id)
    task = forge.store.get_task(task_id)
    assert task["last_status"] == "ok", forge.store.list_runs(task_id)
    assert forge.store.get_target(target_id)["last_status"] == "ok"
    assert run(archive_path(forge.data, task), "rev-parse", "refs/heads/main").stdout.strip() == old
    assert not list((forge.data / "work").glob("ssh-*"))

    run(local, "reset", "--hard", "HEAD~1")
    new = commit(local, "replacement")
    with git_env(forge.store.get_cred(forge.cred_id), forge.data / "work") as env:
        run(local, "push", "--force", source_url, "main", env=env)
    run_task(forge.store, forge.data, task_id)
    assert forge.store.get_task(task_id)["last_status"] == "ok", forge.store.list_runs(task_id)
    assert forge.store.get_target(target_id)["last_status"] == "ok"
    assert forge.api("GET", f"/repos/gwadmin/{name}-main/branches/main")["commit"]["id"] == new
    tags = forge.api("GET", f"/repos/gwadmin/{name}-history/tags")
    preserved = [tag for tag in tags if re.fullmatch(r"gw/[0-9A-HJKMNP-TV-Z]{26}", tag["name"])]
    assert len(preserved) == 1 and preserved[0]["commit"]["sha"] == old
    assert any(tag["name"] == "gw/binding" for tag in tags)
    assert not list((forge.data / "work").glob("ssh-*"))


def test_wrong_ssh_host_key_fails_without_private_key_or_file_leak(ssh_forge, caplog):
    forge = ssh_forge
    name = "ssh-reject-" + uuid.uuid4().hex[:10]
    repo = forge.api("POST", "/user/repos", {"name": name, "private": True, "auto_init": True})
    other_port = next(port for port in SSH_PORTS.values() if port != forge.port)
    wrong_hosts = scan_host_key(other_port).replace(f"]:{other_port}", f"]:{forge.port}")
    cred = forge.store.get_cred(forge.cred_id)
    assert wrong_hosts.split()[2] != cred["known_hosts"].split()[2]
    forge.store.update_cred(forge.cred_id, known_hosts=wrong_hosts)
    task_id = forge.store.add_task(name, repo["ssh_url"], source_cred_id=forge.cred_id)
    run_task(forge.store, forge.data, task_id)
    assert forge.store.get_task(task_id)["last_status"] == "failed"
    result = forge.store.list_runs(task_id)[0]
    assert result["status"] == "failed"
    assert "REMOTE HOST IDENTIFICATION HAS CHANGED" in result["message"]
    text = result["message"] + caplog.text
    assert all(line not in text for line in cred["private_key"].splitlines())
    assert not list((forge.data / "work").glob("ssh-*"))
