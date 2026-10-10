"""工具机档案裸仓：采集来源状态，先保留被覆盖的旧对象，再更新镜像引用。"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from gitwarden.git import run
from gitwarden.telemetry import step as telemetry_step
from gitwarden.ulid import new_ulid


class ArchiveBusy(Exception):
    """档案仓正被另一个采集持有。"""


@dataclass
class Event:
    ref: str
    old: str | None
    new: str | None
    kind: str  # create | fast-forward | force | tag-rewrite | delete
    preserved_ref: str | None = None


@dataclass
class CaptureResult:
    events: list[Event] = field(default_factory=list)


def init_archive(path: Path) -> None:
    if not path.exists():
        run(None, "init", "--bare", str(path))
    run(path, "config", "gc.auto", "0")
    run(path, "config", "core.fsync", "all")
    run(path, "config", "advice.nestedTag", "false")


@contextmanager
def _locked(archive: Path) -> Iterator[None]:
    with open(archive / "gitwarden.lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveBusy(str(archive)) from None
        yield


def _read_refs(archive: Path, *prefixes: str) -> dict[str, str]:
    """返回 {ref 全名: 引用直接指向的 OID}（标签对象不剥离）。"""
    out = run(archive, "for-each-ref", "--format=%(refname) %(objectname)", *prefixes).stdout
    refs = {}
    for line in out.splitlines():
        name, oid = line.split(" ")
        refs[name] = oid
    return refs


def _existing_preserved(archive: Path) -> dict[tuple[str, str], str]:
    """返回 {(原 ref 全名, 旧 OID): 保留标签引用名}。"""
    out = run(
        archive,
        "for-each-ref",
        "refs/preserved",
        "--format=%(refname)%00%(object)%00%(contents)%01",
    ).stdout
    found = {}
    for record in out.split("\x01\n"):
        if not record:
            continue
        name, obj, contents = record.split("\x00")
        for line in contents.splitlines():
            if line.startswith("ref: "):
                found[(line[len("ref: ") :], obj)] = name
                break
    return found


def _classify(archive: Path) -> list[Event]:
    incoming = {
        ref.replace("refs/incoming/", "refs/", 1): oid
        for ref, oid in _read_refs(archive, "refs/incoming/heads", "refs/incoming/tags").items()
    }
    mirror = _read_refs(archive, "refs/heads", "refs/tags")
    events = []
    for ref, new in sorted(incoming.items()):
        old = mirror.get(ref)
        if old is None:
            events.append(Event(ref, None, new, "create"))
        elif old == new:
            continue
        elif ref.startswith("refs/tags/"):
            events.append(Event(ref, old, new, "tag-rewrite"))
        else:
            ff = run(archive, "merge-base", "--is-ancestor", old, new, check=False).returncode == 0
            events.append(Event(ref, old, new, "fast-forward" if ff else "force"))
    for ref, old in sorted(mirror.items()):
        if ref not in incoming:
            events.append(Event(ref, old, None, "delete"))
    return events


def _preserve(archive: Path, source_id: str, events: list[Event]) -> None:
    """阶段 A：为被覆盖或删除的旧对象创建保留标签，单事务提交。"""
    existing = _existing_preserved(archive)
    now = datetime.now(UTC)
    commands = []
    for ev in events:
        if ev.kind not in ("force", "tag-rewrite", "delete"):
            continue
        assert ev.old is not None
        dup = existing.get((ev.ref, ev.old))
        if dup is not None:
            ev.preserved_ref = dup
            continue
        ulid = new_ulid()
        obj_type = run(archive, "cat-file", "-t", ev.old).stdout.strip()
        body = (
            f"object {ev.old}\n"
            f"type {obj_type}\n"
            f"tag gw/{ulid}\n"
            f"tagger GitWarden <gitwarden@localhost> {int(now.timestamp())} +0000\n"
            "\n"
            "GitWarden preserved\n"
            "\n"
            f"source: {source_id}\n"
            f"ref: {ev.ref}\n"
            f"old: {ev.old}\n"
            f"new: {ev.new or 'deleted'}\n"
            f"event: {ev.kind}\n"
            f"time: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        )
        tag_oid = run(archive, "mktag", input=body).stdout.strip()
        ev.preserved_ref = f"refs/preserved/{ulid}"
        commands.append(f"create {ev.preserved_ref} {tag_oid}\n")
    if commands:
        run(archive, "update-ref", "--stdin", input="start\n" + "".join(commands) + "commit\n")


def _commit_mirror(archive: Path, events: list[Event]) -> None:
    """阶段 B：带期望旧值，单事务更新镜像引用。"""
    commands = []
    for ev in events:
        if ev.kind == "create":
            commands.append(f"create {ev.ref} {ev.new}\n")
        elif ev.kind == "delete":
            commands.append(f"delete {ev.ref} {ev.old}\n")
        else:
            commands.append(f"update {ev.ref} {ev.new} {ev.old}\n")
    if commands:
        run(archive, "update-ref", "--stdin", input="start\n" + "".join(commands) + "commit\n")


def capture(
    archive: Path, source_url: str, source_id: str, *, env: dict[str, str] | None = None
) -> CaptureResult:
    with _locked(archive):
        with telemetry_step("capture"):
            with telemetry_step("fetch"):
                run(
                    archive,
                    "fetch",
                    "--no-tags",
                    "--prune",
                    source_url,
                    "+refs/heads/*:refs/incoming/heads/*",
                    "+refs/tags/*:refs/incoming/tags/*",
                    env=env,
                )
            events = _classify(archive)
            with telemetry_step("preserve"):
                _preserve(archive, source_id, events)
            with telemetry_step("mirror"):
                _commit_mirror(archive, events)
            return CaptureResult(events)
