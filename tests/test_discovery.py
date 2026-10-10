from types import SimpleNamespace

import pytest

from gitwarden import discovery as discovery_module
from gitwarden.db import Store
from gitwarden.discovery import discover
from gitwarden.platform import IncompleteListing


def test_incomplete_listing_does_not_change_tasks(tmp_path, monkeypatch):
    store = Store(tmp_path)
    cred = store.add_cred("test", "https", username="bot", secret="test-token")
    instance = store.add_instance("local", "gitea", "http://127.0.0.1:18300")
    access = store.add_access(instance, "bot", cred)
    task = store.add_task(
        "old",
        "http://127.0.0.1:18300/bot/old.git",
        instance_id=instance,
        access_id=access,
        platform_repo_id=1,
    )

    def incomplete():
        raise IncompleteListing("仓库数量不一致")

    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=incomplete),
    )
    before = store.list_tasks()
    with pytest.raises(IncompleteListing):
        discover(store, access)
    assert store.list_tasks() == before
    assert store.get_task(task)["source_url"].endswith("/old.git")


def test_discovery_deduplicates_by_instance_repository_and_counts_other_access(
    tmp_path, monkeypatch
):
    store = Store(tmp_path)
    cred1 = store.add_cred("test-1", "https", username="bot-1", secret="test-token-1")
    cred2 = store.add_cred("test-2", "https", username="bot-2", secret="test-token-2")
    instance = store.add_instance("local", "gitea", "http://127.0.0.1:18300")
    access1 = store.add_access(instance, "first", cred1)
    access2 = store.add_access(instance, "second", cred2)
    repos = [
        {
            "id": 1,
            "full_name": "alice/repo",
            "clone_url": "http://127.0.0.1:18300/alice/repo.git",
            "mirror": False,
        }
    ]

    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: repos),
    )
    first = discover(store, access1)
    assert first.created == ["alice/repo"]
    task = store.list_tasks()[0]
    assert task["instance_id"] == instance
    assert task["access_id"] == access1
    assert task["platform_repo_id"] == 1
    assert task["enabled"] == 1

    second = discover(store, access2)
    assert second.created == []
    assert second.other_access == ["alice/repo"]
    assert len(store.list_tasks()) == 1


def test_discovery_respects_access_auto_enable(tmp_path, monkeypatch):
    store = Store(tmp_path)
    cred = store.add_cred("test", "https", username="bot", secret="test-token")
    instance = store.add_instance("local", "forgejo", "http://127.0.0.1:18301")
    access = store.add_access(instance, "manual", cred, auto_enable=False)
    repos = [
        {
            "id": 2,
            "full_name": "alice/disabled",
            "clone_url": "http://127.0.0.1:18301/alice/disabled.git",
            "mirror": False,
        }
    ]
    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: repos),
    )

    result = discover(store, access)
    assert result.created == ["alice/disabled"]
    assert store.list_tasks()[0]["enabled"] == 0


def _repo(
    repo_id, full_name, *, clone_url=None, mirror=False, fork=False, archived=False, private=True
):
    return {
        "id": repo_id,
        "full_name": full_name,
        "clone_url": clone_url or f"http://127.0.0.1/{full_name}.git",
        "mirror": mirror,
        "fork": fork,
        "archived": archived,
        "private": private,
    }


def _access_store(tmp_path, **settings):
    store = Store(tmp_path / "data")
    cred = store.add_cred("bot", "https", username="bot", secret="test-token")
    instance = store.add_instance("local", "gitea", "http://127.0.0.1:18300")
    access = store.add_access(instance, "bot", cred, **settings)
    return store, instance, access, cred


def test_discovery_filters_rules_and_transitions_managed_states(tmp_path, monkeypatch):
    store, instance, access, cred = _access_store(
        tmp_path,
        rule_mode="exclude",
        patterns=["*/tmp-*"],
        auto_enable=False,
        detailed_default=True,
        default_sync_interval_minutes=30,
    )
    other_cred = store.add_cred("other", "https", username="other", secret="other-token")
    other_access = store.add_access(instance, "other", other_cred)
    keep_id = store.add_task(
        "keep",
        "http://127.0.0.1/old.git",
        source_cred_id=cred,
        instance_id=instance,
        access_id=access,
        platform_repo_id=1,
    )
    excluded_id = store.add_task(
        "excluded",
        "http://127.0.0.1/excluded.git",
        source_cred_id=cred,
        instance_id=instance,
        access_id=access,
        platform_repo_id=2,
    )
    missing_id = store.add_task(
        "missing",
        "http://127.0.0.1/missing.git",
        source_cred_id=cred,
        instance_id=instance,
        access_id=access,
        platform_repo_id=3,
    )
    other_id = store.add_task(
        "other-task",
        "http://127.0.0.1/other.git",
        source_cred_id=other_cred,
        instance_id=instance,
        access_id=other_access,
        platform_repo_id=4,
    )
    repos = [
        _repo(1, "alice/renamed", clone_url="http://127.0.0.1/alice/renamed.git"),
        _repo(2, "alice/tmp-scratch"),
        _repo(4, "alice/other"),
        _repo(5, "alice/new"),
        _repo(6, "alice/pull-mirror", mirror=True),
    ]
    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: repos),
    )

    result = discover(store, access)

    assert result.created == ["alice/new"]
    assert result.renamed == ["alice/renamed"]
    assert result.disabled == ["alice/tmp-scratch"]
    assert result.not_visible == ["missing"]
    assert result.other_access == ["alice/other"]
    assert result.skipped == ["alice/pull-mirror"]
    assert {item["category"] for item in result.items} == {
        "Add",
        "Keep",
        "Disable",
        "Skip",
        "Other access",
    }
    assert all(item["reason"] for item in result.items)

    keep = store.get_task(keep_id)
    assert keep["source_url"] == "http://127.0.0.1/alice/renamed.git"
    assert keep["managed_state"] == "active"
    excluded = store.get_task(excluded_id)
    assert excluded["enabled"] == 0
    assert excluded["managed_state"] == "excluded"
    missing = store.get_task(missing_id)
    assert missing["enabled"] == 1
    assert missing["managed_state"] == "not_visible"
    other = store.get_task(other_id)
    assert other["enabled"] == 1
    assert other["managed_state"] == "active"
    added = next(task for task in store.list_tasks() if task["platform_repo_id"] == 5)
    assert added["enabled"] == 0
    assert added["detailed_logging"] == 1
    assert added["interval_minutes"] == 30
    assert added["source_cred_id"] == cred
    summary = store.get_access(access)["last_discovery_summary"]
    assert summary["complete"] is True
    assert 3 not in summary["seen_repo_ids"]
    assert summary["not_visible"] == ["missing"]


def test_discovery_matches_include_patterns_case_insensitively(tmp_path, monkeypatch):
    store, _, access, _ = _access_store(
        tmp_path,
        rule_mode="include",
        patterns=["alice/KEEP-*"],
    )
    repos = [_repo(1, "Alice/Keep-This"), _repo(2, "alice/skip-this")]
    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: repos),
    )

    result = discover(store, access)

    assert result.created == ["Alice/Keep-This"]
    assert result.skipped == ["alice/skip-this"]
    assert [task["platform_repo_id"] for task in store.list_tasks()] == [1]


def test_preview_returns_five_categories_without_writing(tmp_path, monkeypatch):
    store, instance, access, cred = _access_store(
        tmp_path,
        rule_mode="exclude",
        patterns=[],
    )
    other_cred = store.add_cred("other", "https", username="other", secret="other-token")
    other_access = store.add_access(instance, "other", other_cred)
    store.add_task(
        "keep",
        "http://127.0.0.1/keep.git",
        source_cred_id=cred,
        instance_id=instance,
        access_id=access,
        platform_repo_id=1,
    )
    store.add_task(
        "will-disable",
        "http://127.0.0.1/disable.git",
        source_cred_id=cred,
        instance_id=instance,
        access_id=access,
        platform_repo_id=2,
    )
    store.add_task(
        "other-task",
        "http://127.0.0.1/other.git",
        source_cred_id=other_cred,
        instance_id=instance,
        access_id=other_access,
        platform_repo_id=3,
    )
    repos = [
        _repo(1, "alice/keep"),
        _repo(2, "alice/disabled"),
        _repo(3, "alice/other"),
        _repo(4, "alice/add"),
        _repo(5, "alice/skip"),
    ]
    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: repos),
    )
    before_tasks = store.list_tasks()
    before_access = store.get_access(access)

    result = discovery_module.preview(
        store,
        access,
        {"rule_mode": "include", "patterns": ["alice/keep", "alice/add"]},
    )

    assert {item["category"] for item in result.items} == {
        "Add",
        "Keep",
        "Disable",
        "Skip",
        "Other access",
    }
    assert all(item["reason"] for item in result.items)
    assert store.list_tasks() == before_tasks
    assert store.get_access(access) == before_access


def test_incomplete_discovery_does_not_write_summary(tmp_path, monkeypatch):
    store, _, access, _ = _access_store(tmp_path)
    before = store.get_access(access)

    def incomplete():
        raise IncompleteListing("owner page missing")

    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=incomplete),
    )

    with pytest.raises(IncompleteListing):
        discover(store, access)

    assert store.get_access(access) == before


def test_rule_excluded_task_is_reenabled_when_rule_no_longer_matches(tmp_path, monkeypatch):
    store, instance, access, cred = _access_store(
        tmp_path, rule_mode="exclude", patterns=["*/tmp-*"], auto_enable=True
    )
    task_id = store.add_task(
        "tmp",
        "http://127.0.0.1/alice/tmp-x.git",
        source_cred_id=cred,
        instance_id=instance,
        access_id=access,
        platform_repo_id=1,
    )
    monkeypatch.setattr(
        "gitwarden.discovery.client_from_store",
        lambda *_: SimpleNamespace(list_all_repos=lambda: [_repo(1, "alice/tmp-x")]),
    )

    discover(store, access)
    assert store.get_task(task_id)["enabled"] == 0

    store.update_access(access, patterns=[])
    discover(store, access)
    task = store.get_task(task_id)
    assert task["enabled"] == 1
    assert task["managed_state"] == "active"
