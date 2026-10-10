import json
import sqlite3

import pytest

from gitwarden.db import Store


def _columns(store, table):
    with sqlite3.connect(store.path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_t9_schema_contains_instances_accesses_extended_tasks_and_logs(tmp_path):
    store = Store(tmp_path / "data")
    with sqlite3.connect(store.path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert "platforms" not in tables
    assert {"instances", "accesses", "tasks", "targets", "events", "runs"} <= tables
    assert {"run_steps", "run_refs", "activity"} <= tables
    assert {"id", "name", "kind", "base_url"} <= _columns(store, "instances")
    assert {
        "id",
        "instance_id",
        "name",
        "cred_id",
        "scope_mode",
        "owners",
        "include_forks",
        "include_archived",
        "include_public",
        "include_private",
        "rule_mode",
        "patterns",
        "auto_enable",
        "detailed_default",
        "default_sync_interval_minutes",
        "discover_interval_minutes",
        "next_discovery_at",
        "consecutive_failures",
        "recent_durations",
        "last_discovery_at",
        "last_discovery_summary",
    } <= _columns(store, "accesses")
    assert {
        "id",
        "name",
        "source_url",
        "source_cred_id",
        "interval_minutes",
        "enabled",
        "archive_uuid",
        "instance_id",
        "access_id",
        "platform_repo_id",
        "managed_state",
        "detailed_logging",
        "recent_durations",
        "next_run_at",
        "consecutive_failures",
    } <= _columns(store, "tasks")
    assert {"last_audit_at"} <= _columns(store, "targets")
    assert {"initial_backup", "progress"} <= _columns(store, "runs")
    assert {
        "id",
        "run_id",
        "at",
        "level",
        "step",
        "target_id",
        "message",
        "duration_ms",
        "raw",
    } <= _columns(store, "run_steps")
    assert {"id", "run_id", "ref", "kind", "old", "new"} <= _columns(store, "run_refs")
    assert {"id", "at", "action", "object", "detail", "ip"} <= _columns(store, "activity")


def test_instance_and_access_crud_defaults_json_and_validation(tmp_path):
    store = Store(tmp_path / "data")
    cred_id = store.add_cred("gitea-token", "https", username="admin", secret="token-1")

    instance_id = store.add_instance("internal-gitea", "gitea", "https://git.example.test")
    assert store.get_instance(instance_id) == {
        "id": instance_id,
        "name": "internal-gitea",
        "kind": "gitea",
        "base_url": "https://git.example.test",
    }

    access_id = store.add_access(instance_id, "admin", cred_id)
    access = store.get_access(access_id)
    assert access["instance_id"] == instance_id
    assert access["name"] == "admin"
    assert access["cred_id"] == cred_id
    assert access["scope_mode"] == "all"
    assert access["owners"] == []
    assert access["include_forks"] == 0
    assert access["include_archived"] == 1
    assert access["include_public"] == 1
    assert access["include_private"] == 1
    assert access["rule_mode"] == "exclude"
    assert access["patterns"] == []
    assert access["auto_enable"] == 1
    assert access["detailed_default"] == 0
    assert access["default_sync_interval_minutes"] == 60
    assert access["discover_interval_minutes"] == 60
    assert access["consecutive_failures"] == 0
    assert access["recent_durations"] == []
    assert access["last_discovery_summary"] is None

    owners_access = store.add_access(
        instance_id,
        "owners",
        cred_id,
        scope_mode="owners",
        owners=["alice", "backups"],
        include_forks=True,
        include_archived=True,
        include_public=False,
        include_private=True,
        rule_mode="include",
        patterns=["alice/*"],
        auto_enable=False,
        detailed_default=True,
        default_sync_interval_minutes=30,
        discover_interval_minutes=30,
    )
    updated = store.update_access(
        owners_access,
        owners=["bob"],
        patterns=["*/tmp-*"],
        last_discovery_summary={"created": 1},
    )
    assert updated is None
    access = store.get_access(owners_access)
    assert access["owners"] == ["bob"]
    assert access["patterns"] == ["*/tmp-*"]
    assert access["last_discovery_summary"] == {"created": 1}
    with sqlite3.connect(store.path) as conn:
        raw_owners, raw_patterns, raw_summary = conn.execute(
            "SELECT owners, patterns, last_discovery_summary FROM accesses WHERE id=?",
            (owners_access,),
        ).fetchone()
    assert json.loads(raw_owners) == ["bob"]
    assert json.loads(raw_patterns) == ["*/tmp-*"]
    assert json.loads(raw_summary) == {"created": 1}
    assert [row["id"] for row in store.list_accesses(instance_id)] == [access_id, owners_access]

    incomplete = store.add_cred("missing-token", "https", username="admin", secret="")
    with pytest.raises(ValueError, match="HTTPS|凭据"):
        store.add_access(instance_id, "bad-cred", incomplete)
    with pytest.raises(ValueError, match="URL 不得内嵌凭据"):
        store.add_instance("bad-url", "gitea", "https://admin:token@git.example.test")
    with pytest.raises(ValueError, match="scope|范围"):
        store.add_access(instance_id, "bad-scope", cred_id, scope_mode="invalid")
    with pytest.raises(ValueError, match="rule|规则"):
        store.add_access(instance_id, "bad-rule", cred_id, rule_mode="invalid")
    with pytest.raises(ValueError, match="发现|间隔"):
        store.add_access(instance_id, "too-fast", cred_id, discover_interval_minutes=14)


def test_tasks_use_instance_and_access_identity_and_decode_new_json_fields(tmp_path):
    store = Store(tmp_path / "data")
    cred_id = store.add_cred("source", "https", username="bot", secret="token")
    instance_id = store.add_instance("internal", "forgejo", "https://git.example.test")
    access_id = store.add_access(instance_id, "bot", cred_id)
    task_id = store.add_task(
        "discovered",
        "https://git.example.test/alice/repo.git",
        source_cred_id=cred_id,
        interval_minutes=10,
        instance_id=instance_id,
        access_id=access_id,
        platform_repo_id=123,
        managed_state="active",
        detailed_logging=True,
        recent_durations=[1.25, 2.5],
        next_run_at="2026-10-10T00:00:00+00:00",
        consecutive_failures=2,
    )
    task = store.get_task(task_id)
    assert task["instance_id"] == instance_id
    assert task["access_id"] == access_id
    assert task["platform_repo_id"] == 123
    assert task["managed_state"] == "active"
    assert task["detailed_logging"] == 1
    assert task["recent_durations"] == [1.25, 2.5]
    assert task["next_run_at"] == "2026-10-10T00:00:00+00:00"
    assert task["consecutive_failures"] == 2

    store.update_task(
        task_id,
        managed_state="excluded",
        detailed_logging=False,
        recent_durations=[3.0],
        next_run_at=None,
        consecutive_failures=0,
    )
    task = store.get_task(task_id)
    assert task["managed_state"] == "excluded"
    assert task["detailed_logging"] == 0
    assert task["recent_durations"] == [3.0]
    assert task["next_run_at"] is None
    assert task["consecutive_failures"] == 0

    with pytest.raises(sqlite3.IntegrityError):
        store.add_task(
            "same-repository",
            "https://git.example.test/alice/other.git",
            instance_id=instance_id,
            access_id=access_id,
            platform_repo_id=123,
        )
    manual_id = store.add_task("manual", "https://example.test/manual.git")
    assert store.get_task(manual_id)["instance_id"] is None
    assert store.get_task(manual_id)["access_id"] is None
    assert store.get_task(manual_id)["platform_repo_id"] is None


def test_delete_access_makes_tasks_manual_but_preserves_identity_and_history(tmp_path):
    store = Store(tmp_path / "data")
    cred_id = store.add_cred("source", "https", username="bot", secret="token")
    instance_id = store.add_instance("internal", "gitea", "https://git.example.test")
    access_id = store.add_access(instance_id, "bot", cred_id)
    task_id = store.add_task(
        "discovered",
        "https://git.example.test/alice/repo.git",
        source_cred_id=cred_id,
        instance_id=instance_id,
        access_id=access_id,
        platform_repo_id=123,
    )
    target_id = store.add_target(
        task_id,
        "history",
        "https://backup.example/main.git",
        "https://backup.example/history.git",
    )
    store.add_event(task_id, "refs/heads/main", "force", "old", "new", "refs/preserved/x")
    run_id = store.start_run(task_id)
    store.finish_run(run_id, "ok")

    current_cred = store.add_cred("rotated", "https", username="bot", secret="new-token")
    store.update_access(access_id, cred_id=current_cred)

    store.delete_access(access_id)

    assert store.get_access(access_id) is None
    assert store.get_instance(instance_id) is not None
    task = store.get_task(task_id)
    assert task["access_id"] is None
    assert task["source_cred_id"] == current_cred
    assert task["instance_id"] == instance_id
    assert task["platform_repo_id"] == 123
    assert store.get_target(target_id) is not None
    assert store.list_events(task_id)
    assert store.list_runs(task_id)


def test_delete_task_is_removed_or_refuses_to_delete_history(tmp_path):
    store = Store(tmp_path / "data")
    task_id = store.add_task("task", "https://example.test/source.git")
    store.add_event(task_id, "refs/heads/main", "force", "old", "new", None)
    delete_task = getattr(store, "delete_task", None)
    if delete_task is None:
        return
    with pytest.raises(ValueError, match="删除|历史"):
        delete_task(task_id)
    assert store.get_task(task_id) is not None
    assert store.list_events(task_id)
