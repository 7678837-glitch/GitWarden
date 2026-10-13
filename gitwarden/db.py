"""SQLite 存储（<data_dir>/gitwarden.db）。每次调用独立连接，线程安全。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gitwarden.creds import check_target_urls, check_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS creds(id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('none','https','ssh')),
    username TEXT,
    secret TEXT,
    private_key TEXT,
    known_hosts TEXT);
CREATE TABLE IF NOT EXISTS instances(id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('gitea','forgejo')),
    base_url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accesses(id INTEGER PRIMARY KEY,
    instance_id INTEGER NOT NULL REFERENCES instances(id),
    name TEXT NOT NULL,
    cred_id INTEGER NOT NULL REFERENCES creds(id),
    scope_mode TEXT NOT NULL DEFAULT 'all' CHECK(scope_mode IN ('all','owners')),
    owners TEXT NOT NULL DEFAULT '[]',
    include_forks INTEGER NOT NULL DEFAULT 0,
    include_archived INTEGER NOT NULL DEFAULT 1,
    include_public INTEGER NOT NULL DEFAULT 1,
    include_private INTEGER NOT NULL DEFAULT 1,
    rule_mode TEXT NOT NULL DEFAULT 'exclude' CHECK(rule_mode IN ('exclude','include')),
    patterns TEXT NOT NULL DEFAULT '[]',
    auto_enable INTEGER NOT NULL DEFAULT 1,
    detailed_default INTEGER NOT NULL DEFAULT 0,
    default_sync_interval_minutes INTEGER NOT NULL DEFAULT 60,
    discover_interval_minutes INTEGER NOT NULL DEFAULT 60,
    next_discovery_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    recent_durations TEXT NOT NULL DEFAULT '[]',
    last_discovery_at TEXT,
    last_discovery_summary TEXT,
    UNIQUE(instance_id, name));
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    source_url TEXT NOT NULL,
    source_cred_id INTEGER REFERENCES creds(id),
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    enabled INTEGER NOT NULL DEFAULT 1,
    archive_uuid TEXT UNIQUE NOT NULL,
    last_run_at TEXT,
    last_status TEXT,
    last_success_at TEXT,
    external_note TEXT,
    instance_id INTEGER REFERENCES instances(id),
    access_id INTEGER REFERENCES accesses(id) ON DELETE SET NULL,
    platform_repo_id INTEGER,
    managed_state TEXT NOT NULL DEFAULT 'active'
        CHECK(managed_state IN ('active','not_visible','excluded','source_gone')),
    detailed_logging INTEGER NOT NULL DEFAULT 0,
    recent_durations TEXT NOT NULL DEFAULT '[]',
    next_run_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    UNIQUE(instance_id, platform_repo_id));
CREATE TABLE IF NOT EXISTS targets(id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    main_url TEXT NOT NULL,
    history_url TEXT NOT NULL,
    cred_id INTEGER REFERENCES creds(id),
    bound INTEGER NOT NULL DEFAULT 0,
    last_status TEXT,
    last_detail TEXT,
    last_audit_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    old TEXT,
    new TEXT,
    preserved_ref TEXT);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    message TEXT,
    initial_backup INTEGER NOT NULL DEFAULT 0,
    progress TEXT);
CREATE TABLE IF NOT EXISTS run_steps(id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('info','warn','error')),
    step TEXT NOT NULL,
    target_id INTEGER REFERENCES targets(id) ON DELETE SET NULL,
    message TEXT,
    duration_ms INTEGER,
    raw TEXT);
CREATE TABLE IF NOT EXISTS run_refs(id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ref TEXT NOT NULL,
    kind TEXT NOT NULL,
    old TEXT,
    new TEXT);
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    object TEXT,
    detail TEXT,
    ip TEXT);
"""

_CRED_FIELDS = {"name", "kind", "username", "secret", "private_key", "known_hosts"}
_ACCESS_FIELDS = {
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
}
_TASK_FIELDS = {
    "name",
    "source_url",
    "source_cred_id",
    "interval_minutes",
    "enabled",
    "instance_id",
    "access_id",
    "platform_repo_id",
    "managed_state",
    "detailed_logging",
    "recent_durations",
    "next_run_at",
    "consecutive_failures",
}
_TARGET_FIELDS = {"name", "main_url", "history_url", "cred_id"}
_JSON_FIELDS = {"owners", "patterns", "recent_durations", "last_discovery_summary"}
_MANAGED_STATES = {"active", "not_visible", "excluded", "source_gone"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json_value(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _encode_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _json_value(value) if key in _JSON_FIELDS else value for key, value in fields.items()
    }


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in _JSON_FIELDS & row.keys():
        if row[key] is not None:
            row[key] = json.loads(row[key])
    return row


class Store:
    def __init__(
        self,
        data_dir: Path,
        min_sync_minutes: int = 5,
        min_discovery_minutes: int = 15,
    ) -> None:
        if isinstance(min_sync_minutes, bool) or min_sync_minutes < 1:
            raise ValueError("同步间隔下限必须至少为 1 分钟")
        if isinstance(min_discovery_minutes, bool) or min_discovery_minutes < 1:
            raise ValueError("发现间隔下限必须至少为 1 分钟")
        self.min_sync_minutes = min_sync_minutes
        self.min_discovery_minutes = min_discovery_minutes
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "gitwarden.db"
        self.path.touch(mode=0o600)
        self.path.chmod(0o600)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with closing(self._connect()) as conn, conn:
            return conn.execute(sql, args).lastrowid or 0

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            return [_decode_row(dict(row)) for row in conn.execute(sql, args)]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _update(self, table: str, row_id: int, allowed: set[str], fields: dict[str, Any]) -> None:
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"不可修改的字段: {sorted(bad)}")
        if fields:
            encoded = _encode_fields(fields)
            sets = ", ".join(f"{key}=?" for key in encoded)
            self._exec(f"UPDATE {table} SET {sets} WHERE id=?", (*encoded.values(), row_id))

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str):
            raise ValueError("平台地址必须是 HTTP(S) URL")
        base_url = base_url.strip()
        parts = urlsplit(base_url)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or parts.query
            or parts.fragment
        ):
            raise ValueError("平台地址必须是 HTTP(S) URL，且不得内嵌凭据")
        if parts.username is not None or parts.password is not None:
            raise ValueError("URL 不得内嵌凭据，请改用凭据配置")
        return base_url.rstrip("/")

    def _validate_interval(self, value: int, minimum: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{label}不能低于 {minimum} 分钟")
        return value

    def _https_cred(self, cred_id: int) -> dict[str, Any]:
        cred = self.get_cred(cred_id)
        if (
            not cred
            or cred["kind"] != "https"
            or not isinstance(cred.get("username"), str)
            or not cred["username"].strip()
            or not isinstance(cred.get("secret"), str)
            or not cred["secret"].strip()
        ):
            raise ValueError("接入凭据必须是包含用户名和令牌的 HTTPS 凭据")
        return cred

    @staticmethod
    def _validate_access_modes(
        scope_mode: str, rule_mode: str, owners: list[str] | None = None
    ) -> None:
        if scope_mode not in {"all", "owners"}:
            raise ValueError("接入范围必须是 all 或 owners")
        if scope_mode == "owners" and not [o for o in owners or [] if str(o).strip()]:
            raise ValueError("owners 范围至少需要一个 owner")
        if rule_mode not in {"exclude", "include"}:
            raise ValueError("接入规则必须是 exclude 或 include")

    # credentials
    def add_cred(
        self,
        name: str,
        kind: str,
        username: str | None = None,
        secret: str | None = None,
        private_key: str | None = None,
        known_hosts: str | None = None,
    ) -> int:
        return self._exec(
            "INSERT INTO creds(name,kind,username,secret,private_key,known_hosts)"
            " VALUES(?,?,?,?,?,?)",
            (name, kind, username, secret, private_key, known_hosts),
        )

    def get_cred(self, cred_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM creds WHERE id=?", (cred_id,))

    def list_creds(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM creds ORDER BY id")

    def update_cred(self, cred_id: int, **fields: Any) -> None:
        self._update("creds", cred_id, _CRED_FIELDS, fields)

    def delete_cred(self, cred_id: int) -> None:
        if self._one("SELECT id FROM accesses WHERE cred_id=?", (cred_id,)):
            raise ValueError(f"凭据 {cred_id} 正被接入引用，不能删除")
        used = self._one(
            "SELECT (SELECT count(*) FROM tasks WHERE source_cred_id=?)"
            " + (SELECT count(*) FROM targets WHERE cred_id=?) AS n",
            (cred_id, cred_id),
        )
        if used and used["n"]:
            raise ValueError(f"凭据 {cred_id} 正被任务或目标引用，不能删除")
        self._exec("DELETE FROM creds WHERE id=?", (cred_id,))

    def secret_values(self) -> list[str]:
        values = []
        for row in self.list_creds():
            for key in ("secret", "private_key"):
                if row[key]:
                    values += [row[key], row[key].strip()]
        return values

    # instances and accesses
    def add_instance(self, name: str, kind: str, base_url: str) -> int:
        if kind not in {"gitea", "forgejo"}:
            raise ValueError("实例类型必须是 gitea 或 forgejo")
        return self._exec(
            "INSERT INTO instances(name,kind,base_url) VALUES(?,?,?)",
            (name, kind, self._validate_base_url(base_url)),
        )

    def get_instance(self, instance_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM instances WHERE id=?", (instance_id,))

    def list_instances(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM instances ORDER BY id")

    def delete_instance(self, instance_id: int) -> None:
        if self._one("SELECT id FROM accesses WHERE instance_id=?", (instance_id,)):
            raise ValueError(f"实例 {instance_id} 被接入引用，不能删除")
        if self._one("SELECT id FROM tasks WHERE instance_id=?", (instance_id,)):
            raise ValueError(f"实例 {instance_id} 被任务引用，不能删除")
        self._exec("DELETE FROM instances WHERE id=?", (instance_id,))

    def add_access(
        self,
        instance_id: int,
        name: str,
        cred_id: int,
        *,
        scope_mode: str = "all",
        owners: list[str] | None = None,
        include_forks: bool = False,
        include_archived: bool = True,
        include_public: bool = True,
        include_private: bool = True,
        rule_mode: str = "exclude",
        patterns: list[str] | None = None,
        auto_enable: bool = True,
        detailed_default: bool = False,
        default_sync_interval_minutes: int = 60,
        discover_interval_minutes: int = 60,
        next_discovery_at: str | None = None,
        consecutive_failures: int = 0,
        recent_durations: list[float] | None = None,
        last_discovery_at: str | None = None,
        last_discovery_summary: dict[str, Any] | None = None,
    ) -> int:
        if self.get_instance(instance_id) is None:
            raise ValueError("实例不存在")
        self._https_cred(cred_id)
        self._validate_access_modes(scope_mode, rule_mode, owners)
        default_sync_interval_minutes = self._validate_interval(
            default_sync_interval_minutes, self.min_sync_minutes, "同步间隔"
        )
        discover_interval_minutes = self._validate_interval(
            discover_interval_minutes, self.min_discovery_minutes, "发现间隔"
        )
        return self._exec(
            "INSERT INTO accesses(instance_id,name,cred_id,scope_mode,owners,include_forks,"
            "include_archived,include_public,include_private,rule_mode,patterns,auto_enable,"
            "detailed_default,default_sync_interval_minutes,discover_interval_minutes,"
            "next_discovery_at,consecutive_failures,recent_durations,last_discovery_at,"
            "last_discovery_summary) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                instance_id,
                name,
                cred_id,
                scope_mode,
                _json_value(owners or []),
                int(include_forks),
                int(include_archived),
                int(include_public),
                int(include_private),
                rule_mode,
                _json_value(patterns or []),
                int(auto_enable),
                int(detailed_default),
                default_sync_interval_minutes,
                discover_interval_minutes,
                next_discovery_at,
                consecutive_failures,
                _json_value(recent_durations or []),
                last_discovery_at,
                _json_value(last_discovery_summary),
            ),
        )

    def get_access(self, access_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM accesses WHERE id=?", (access_id,))

    def list_accesses(self, instance_id: int | None = None) -> list[dict[str, Any]]:
        if instance_id is None:
            return self._rows("SELECT * FROM accesses ORDER BY id")
        return self._rows("SELECT * FROM accesses WHERE instance_id=? ORDER BY id", (instance_id,))

    def validate_access_fields(self, access_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        """校验接入设置（保存与预览共用），返回规范化后的字段；不写库。"""
        fields = dict(fields)
        current = self.get_access(access_id)
        if current is None:
            raise ValueError("接入不存在")
        self._validate_access_modes(
            fields.get("scope_mode", current["scope_mode"]),
            fields.get("rule_mode", current["rule_mode"]),
            fields.get("owners", current["owners"]),
        )
        if "cred_id" in fields:
            self._https_cred(fields["cred_id"])
        if "default_sync_interval_minutes" in fields:
            fields["default_sync_interval_minutes"] = self._validate_interval(
                fields["default_sync_interval_minutes"], self.min_sync_minutes, "同步间隔"
            )
        if "discover_interval_minutes" in fields:
            fields["discover_interval_minutes"] = self._validate_interval(
                fields["discover_interval_minutes"], self.min_discovery_minutes, "发现间隔"
            )
        return fields

    def update_access(self, access_id: int, **fields: Any) -> None:
        fields = self.validate_access_fields(access_id, fields)
        for key in (
            "include_forks",
            "include_archived",
            "include_public",
            "include_private",
            "auto_enable",
            "detailed_default",
        ):
            if key in fields:
                fields[key] = int(fields[key])
        self._update("accesses", access_id, _ACCESS_FIELDS, fields)

    def delete_access(self, access_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """UPDATE tasks SET
                   source_cred_id=(SELECT cred_id FROM accesses WHERE id=?), access_id=NULL
                   WHERE access_id=?""",
                (access_id, access_id),
            )
            conn.execute("DELETE FROM accesses WHERE id=?", (access_id,))

    # tasks
    def add_task(
        self,
        name: str,
        source_url: str,
        source_cred_id: int | None = None,
        interval_minutes: int = 60,
        enabled: bool = True,
        instance_id: int | None = None,
        access_id: int | None = None,
        platform_repo_id: int | None = None,
        managed_state: str = "active",
        detailed_logging: bool = False,
        recent_durations: list[float] | None = None,
        next_run_at: str | None = None,
        consecutive_failures: int = 0,
    ) -> int:
        check_url(source_url)
        self._validate_interval(interval_minutes, self.min_sync_minutes, "同步间隔")
        if managed_state not in _MANAGED_STATES:
            raise ValueError("任务管理状态无效")
        return self._exec(
            "INSERT INTO tasks(name,source_url,source_cred_id,interval_minutes,enabled,"
            "archive_uuid,instance_id,access_id,platform_repo_id,managed_state,detailed_logging,"
            "recent_durations,next_run_at,consecutive_failures) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                name,
                source_url,
                source_cred_id,
                interval_minutes,
                int(enabled),
                uuid.uuid4().hex,
                instance_id,
                access_id,
                platform_repo_id,
                managed_state,
                int(detailed_logging),
                _json_value(recent_durations or []),
                next_run_at,
                consecutive_failures,
            ),
        )

    def delete_task(self, task_id: int) -> None:
        """仅用于撤销刚创建、尚未运行的任务；目标、运行和事件随外键级联删除。"""
        if self.list_events(task_id, limit=1):
            raise ValueError("任务已有保全事件，不能删除历史")
        self._exec("DELETE FROM tasks WHERE id=?", (task_id,))

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM tasks ORDER BY id")

    def update_task(self, task_id: int, **fields: Any) -> None:
        if "source_url" in fields:
            check_url(fields["source_url"])
        if "interval_minutes" in fields:
            fields["interval_minutes"] = self._validate_interval(
                fields["interval_minutes"], self.min_sync_minutes, "同步间隔"
            )
        if "enabled" in fields:
            fields["enabled"] = int(fields["enabled"])
        if "detailed_logging" in fields:
            fields["detailed_logging"] = int(fields["detailed_logging"])
        if "managed_state" in fields and fields["managed_state"] not in _MANAGED_STATES:
            raise ValueError("任务管理状态无效")
        self._update("tasks", task_id, _TASK_FIELDS, fields)

    def finish_task(self, task_id: int, status: str) -> None:
        at = now()
        if status == "ok":
            self._exec(
                "UPDATE tasks SET last_run_at=?, last_status=?, last_success_at=? WHERE id=?",
                (at, status, at, task_id),
            )
        else:
            self._exec(
                "UPDATE tasks SET last_run_at=?, last_status=? WHERE id=?", (at, status, task_id)
            )

    def set_external_note(self, task_id: int, note: str | None) -> None:
        self._exec("UPDATE tasks SET external_note=? WHERE id=?", (note, task_id))

    # targets
    def add_target(
        self, task_id: int, name: str, main_url: str, history_url: str, cred_id: int | None = None
    ) -> int:
        check_target_urls(main_url, history_url)
        return self._exec(
            "INSERT INTO targets(task_id,name,main_url,history_url,cred_id) VALUES(?,?,?,?,?)",
            (task_id, name, main_url, history_url, cred_id),
        )

    def get_target(self, target_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM targets WHERE id=?", (target_id,))

    def list_targets(self, task_id: int | None = None) -> list[dict[str, Any]]:
        if task_id is None:
            return self._rows("SELECT * FROM targets ORDER BY id")
        return self._rows("SELECT * FROM targets WHERE task_id=? ORDER BY id", (task_id,))

    def update_target(self, target_id: int, **fields: Any) -> None:
        current = self.get_target(target_id)
        if current:
            check_target_urls(
                fields.get("main_url", current["main_url"]),
                fields.get("history_url", current["history_url"]),
            )
        else:
            for key in ("main_url", "history_url"):
                if key in fields:
                    check_url(fields[key])
        if current and any(
            key in fields and fields[key] != current[key] for key in ("main_url", "history_url")
        ):
            fields["bound"] = 0
        self._update("targets", target_id, _TARGET_FIELDS | {"bound"}, fields)

    def delete_target(self, target_id: int) -> None:
        self._exec("DELETE FROM targets WHERE id=?", (target_id,))

    def set_bound(self, target_id: int, bound: bool) -> None:
        self._exec("UPDATE targets SET bound=? WHERE id=?", (int(bound), target_id))

    def set_target_status(self, target_id: int, status: str | None, detail: str | None) -> None:
        self._exec(
            "UPDATE targets SET last_status=?, last_detail=? WHERE id=?",
            (status, detail, target_id),
        )

    # events / runs
    def add_event(
        self,
        task_id: int,
        ref: str,
        kind: str,
        old: str | None,
        new: str | None,
        preserved_ref: str | None,
    ) -> None:
        self._exec(
            "INSERT INTO events(task_id,at,ref,kind,old,new,preserved_ref) VALUES(?,?,?,?,?,?,?)",
            (task_id, now(), ref, kind, old, new, preserved_ref),
        )

    def list_events(self, task_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
        )

    def start_run(
        self, task_id: int, initial_backup: bool = False, progress: str | None = None
    ) -> int:
        return self._exec(
            "INSERT INTO runs(task_id,started_at,initial_backup,progress) VALUES(?,?,?,?)",
            (task_id, now(), int(initial_backup), progress),
        )

    def finish_run(
        self,
        run_id: int,
        status: str,
        message: str | None = None,
        progress: str | None = None,
    ) -> None:
        if progress is None:
            self._exec(
                "UPDATE runs SET finished_at=?, status=?, message=? WHERE id=?",
                (now(), status, message, run_id),
            )
        else:
            self._exec(
                "UPDATE runs SET finished_at=?, status=?, message=?, progress=? WHERE id=?",
                (now(), status, message, progress, run_id),
            )

    def list_runs(self, task_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM runs WHERE task_id=? ORDER BY id DESC LIMIT ?", (task_id, limit)
        )
