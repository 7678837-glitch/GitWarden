"""只在 GW_IT=1 时运行；所有平台与凭据均在本机临时 Docker 项目内。"""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from gitwarden.db import Store
from gitwarden.platform import PlatformClient

COMPOSE = ["docker", "compose", "-f", str(Path(__file__).with_name("compose.yml"))]
SCOPES = "read:admin,write:repository,write:organization,write:user"


def pytest_collection_modifyitems(items):
    if os.environ.get("GW_IT") != "1":
        for item in items:
            if Path(__file__).parent in item.path.parents:
                item.add_marker(pytest.mark.skip(reason="本机平台集成测试需 GW_IT=1"))


class PlatformEnvironment(SimpleNamespace):
    def __repr__(self):
        return f"PlatformEnvironment({self.kind})"


def compose(*args):
    result = subprocess.run([*COMPOSE, *args], capture_output=True, text=True, timeout=300)
    if result.returncode:
        pytest.fail(f"docker compose {args[0]} 失败：{result.stderr[-1500:]}")
    return result.stdout.strip()


def api(base_url, token, method, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        base_url + "/api/v1" + path,
        data=data,
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=30) as response:
        body = response.read()
        return json.loads(body) if body else None


def token_for(kind, username, scopes):
    return compose(
        "exec",
        "-T",
        "--user",
        "git",
        kind,
        kind,
        "admin",
        "user",
        "generate-access-token",
        "--username",
        username,
        "--token-name",
        uuid.uuid4().hex,
        "--scopes",
        scopes,
        "--raw",
    ).splitlines()[-1]


@pytest.fixture(scope="session")
def platforms():
    if os.environ.get("GW_IT") != "1":
        pytest.skip("本机平台集成测试需 GW_IT=1")
    try:
        compose("up", "-d")
        environments = {}
        for kind, port in (("gitea", 18300), ("forgejo", 18301)):
            url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 90
            while True:
                try:
                    with urlopen(url + "/api/healthz", timeout=2) as response:
                        if response.status == 200:
                            break
                except (URLError, OSError):
                    if time.monotonic() >= deadline:
                        pytest.fail(f"{kind} 健康检查超时")
                time.sleep(0.5)
            for user in ("gwadmin", "alice"):
                args = [
                    "exec",
                    "-T",
                    "--user",
                    "git",
                    kind,
                    kind,
                    "admin",
                    "user",
                    "create",
                    "--username",
                    user,
                    "--email",
                    f"{user}@example.invalid",
                    "--random-password",
                    "--must-change-password=false",
                ]
                if user == "gwadmin":
                    args.append("--admin")
                compose(*args)
            admin = token_for(kind, "gwadmin", SCOPES)
            alice = token_for(kind, "alice", "write:repository,read:organization,write:user")
            api(url, admin, "POST", "/orgs", {"username": "backups", "visibility": "private"})
            environments[kind] = PlatformEnvironment(
                kind=kind,
                url=url,
                token=admin,
                alice_token=alice,
                client=PlatformClient(url, admin, page_size=2),
                alice=PlatformClient(url, alice, page_size=2),
                api=lambda *args, _url=url, _token=admin: api(_url, _token, *args),
            )
        yield environments
    finally:
        compose("down", "-v")


@pytest.fixture(params=["gitea", "forgejo"])
def forge(request, platforms, tmp_path):
    env = platforms[request.param]
    store = Store(tmp_path / "data")
    cred = store.add_cred("test-token", "https", username="gwadmin", secret=env.token)
    instance = store.add_instance(env.kind, env.kind, env.url)
    access = store.add_access(instance, env.kind, cred)
    return PlatformEnvironment(
        **vars(env), store=store, instance_id=instance, access_id=access, data=tmp_path / "data"
    )
