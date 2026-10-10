from types import SimpleNamespace

import pytest

from gitwarden.db import Store
from gitwarden.platform import PlatformError
from gitwarden.provision import provision_target


def test_partial_creation_reports_repo_and_keeps_target_unbound(tmp_path, monkeypatch):
    store = Store(tmp_path)
    cred = store.add_cred("test", "https", username="bot", secret="test-token")
    instance = store.add_instance("local", "gitea", "http://127.0.0.1:18300")
    access = store.add_access(instance, "bot", cred)
    task = store.add_task("source", str(tmp_path / "source.git"))
    created = []

    def missing(*_):
        raise PlatformError("GET repository: 404", status=404)

    def create(owner, name, private=True):
        created.append(f"{owner}/{name}")
        return {"name": name}

    def fail(*_):
        raise PlatformError("PATCH repository: 403")

    client = SimpleNamespace(
        whoami=lambda: {"login": "bot"}, get_repo=missing, create_repo=create, harden=fail
    )
    monkeypatch.setattr("gitwarden.provision.client_from_store", lambda *_: client)
    with pytest.raises(PlatformError) as error:
        provision_target(store, tmp_path, task, access, "bot", "backup")
    assert "PATCH repository: 403" in str(error.value)
    assert "bot/backup" in str(error.value)
    assert created == ["bot/backup"]
    assert store.list_targets() == []
