import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gitwarden.config import Config
from gitwarden.db import Store
from gitwarden.web import create_app

PASSWORD = "pw-platform-pages"


@pytest.fixture
def config(tmp_path):
    return Config(data_dir=tmp_path / "data", admin_password=PASSWORD)


@pytest.fixture
def client(config):
    app = create_app(config, start_scheduler=False)
    with TestClient(app) as test_client:
        yield test_client


def login(client):
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303


def csrf_of(client, path):
    match = re.search(r'name="csrf" value="([^"]+)"', client.get(path).text)
    assert match, f"{path} 页面没有 csrf 字段"
    return match.group(1)


def bootstrap_platforms(config):
    store = Store(config.data_dir)
    cred_id = store.add_cred("ops-token", "https", username="backup-ops", secret="never-render")
    unused_cred_id = store.add_cred("unused-token", "https", username="ci", secret="unused")
    instance_id = store.add_instance("Company Forgejo", "forgejo", "https://git.example.test")
    access_id = store.add_access(
        instance_id,
        "ops-team",
        cred_id,
        scope_mode="owners",
        owners=["ops", "infra"],
        include_forks=False,
        include_archived=True,
        include_public=True,
        include_private=True,
        rule_mode="exclude",
        patterns=["*/tmp-*"],
        auto_enable=True,
        detailed_default=False,
        default_sync_interval_minutes=60,
        discover_interval_minutes=60,
    )
    task_id = store.add_task(
        "ops/runbooks",
        "https://git.example.test/ops/runbooks.git",
        source_cred_id=cred_id,
        instance_id=instance_id,
        access_id=access_id,
        platform_repo_id=11,
    )
    target_id = store.add_target(
        task_id,
        "history",
        "https://backup.example/main.git",
        "https://backup.example/history.git",
        cred_id=cred_id,
    )
    return store, instance_id, access_id, cred_id, unused_cred_id, task_id, target_id


def test_platform_module_exposes_shared_page_contract():
    from gitwarden.ui_platforms import creds_page, platform_page, register

    assert callable(register)
    assert callable(platform_page)
    assert callable(creds_page)


def test_platforms_group_instances_and_render_editable_access_draft(client, config):
    store, instance_id, access_id, *_ = bootstrap_platforms(config)
    login(client)

    response = client.get(f"/platforms?instance_id={instance_id}&access_id={access_id}")

    assert response.status_code == 200
    assert "Company Forgejo" in response.text
    assert "ops-team" in response.text
    assert 'name="scope_mode" value="owners"' in response.text
    assert 'name="owners"' in response.text
    assert ">ops\ninfra</textarea>" in response.text
    assert ">*/tmp-*</textarea>" in response.text
    assert 'name="include_forks" value="0"' in response.text
    assert 'name="include_forks" value="1"' in response.text
    assert 'name="include_archived" value="0"' in response.text
    assert 'name="include_archived" value="1"' in response.text
    assert 'name="include_public" value="0"' in response.text
    assert 'name="include_private" value="0"' in response.text
    assert 'name="rule_mode" value="exclude"' in response.text
    assert 'name="patterns"' in response.text
    assert 'name="auto_enable" value="0"' in response.text
    assert 'name="detailed_default" value="1"' in response.text
    assert 'name="default_sync_interval_minutes"' in response.text
    assert 'name="discover_interval_minutes"' in response.text
    assert f'formaction="/accesses/{access_id}/preview"' in response.text
    assert f'formaction="/accesses/{access_id}"' in response.text
    assert "never-render" not in response.text
    assert store.get_access(access_id)["instance_id"] == instance_id


def test_preview_response_keeps_same_draft_for_save_and_preview(client, config, monkeypatch):
    _, _, access_id, *_ = bootstrap_platforms(config)
    login(client)

    import gitwarden.web as web

    monkeypatch.setattr(
        web,
        "preview",
        lambda store, selected_id, draft: SimpleNamespace(
            items=[{"category": "Add", "name": "ops/alerts", "reason": "new on platform"}],
            created=["ops/alerts"],
            existing=[],
            renamed=[],
            conflicts=[],
            skipped=[],
            disabled=[],
            not_visible=[],
            other_access=[],
        ),
    )
    payload = {
        "csrf": csrf_of(client, "/platforms"),
        "name": "ops-team-draft",
        "cred_id": "1",
        "scope_mode": "owners",
        "owners": "ops\ninfra",
        "include_forks": ["0", "1"],
        "include_archived": ["0", "1"],
        "include_public": ["0", "1"],
        "include_private": ["0", "1"],
        "rule_mode": "include",
        "patterns": "ops/*",
        "auto_enable": ["0", "1"],
        "detailed_default": ["0", "1"],
        "default_sync_interval_minutes": "75",
        "discover_interval_minutes": "90",
    }

    response = client.post(f"/accesses/{access_id}/preview", data=payload)

    assert response.status_code == 200
    assert 'data-preview-access-id="' in response.text
    assert 'name="name" value="ops-team-draft"' in response.text
    assert 'name="default_sync_interval_minutes" value="75"' in response.text
    assert 'name="discover_interval_minutes" value="90"' in response.text
    assert 'name="rule_mode" value="include"' in response.text
    assert "ops/alerts" in response.text
    assert "never-render" not in response.text


def test_credentials_show_real_reference_counts_disable_used_delete_and_hide_secrets(
    client, config
):
    _, _, _, _, unused_cred_id, *_ = bootstrap_platforms(config)
    login(client)

    response = client.get("/creds")

    assert response.status_code == 200
    assert "ops-token" in response.text
    assert "1 task" in response.text
    assert "1 target" in response.text
    assert "1 platform" in response.text
    assert re.search(r"<button[^>]+disabled[^>]*>[^<]*Delete", response.text)
    assert f'action="/creds/{unused_cred_id}/delete"' in response.text
    assert "never-render" not in response.text


def test_credential_kind_switch_uses_get_and_preserves_ordinary_fields_without_javascript(
    client, config
):
    bootstrap_platforms(config)
    login(client)

    response = client.get("/creds?kind=ssh&name=rotate-me&username=git")

    assert response.status_code == 200
    assert 'name="kind" value="ssh"' in response.text
    assert 'name="name" value="rotate-me"' in response.text
    assert 'name="username" value="git"' in response.text
    assert 'name="private_key"' in response.text
    assert 'name="known_hosts"' in response.text
    assert "<script" not in response.text
