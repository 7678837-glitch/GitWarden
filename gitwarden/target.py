"""目标端推送：历史仓补齐保留标签，门禁通过后再更新主镜像仓。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from gitwarden.archive import _locked, _read_refs
from gitwarden.git import rev_parse, run
from gitwarden.telemetry import step as telemetry_step

BINDING_TAG = "refs/tags/gw/binding"


class TargetNotEmpty(Exception):
    """绑定时目标仓库不是空仓库。"""


class TargetMismatch(Exception):
    """历史仓里的绑定标签与本地记录不一致。"""


class NotBound(Exception):
    """目标尚未绑定。"""


@dataclass
class Target:
    id: str
    main_url: str
    history_url: str


@dataclass
class PushResult:
    history_pushed: list[str] = field(default_factory=list)
    history_conflicts: list[str] = field(default_factory=list)
    gate_blocked: list[str] = field(default_factory=list)
    main_conflicts: list[str] = field(default_factory=list)
    main_ok: bool = False


@dataclass
class AuditResult:
    missing: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def _ls_remote(url: str, env: dict[str, str] | None = None) -> dict[str, str]:
    out = run(None, "ls-remote", "--refs", url, env=env).stdout
    refs = {}
    for line in out.splitlines():
        oid, name = line.split("\t")
        refs[name] = oid
    return refs


def _binding_ref(target: Target) -> str:
    return f"refs/pushed/{target.id}/binding"


def _history_prefix(target: Target) -> str:
    return f"refs/pushed/{target.id}/history/"


def _main_prefix(target: Target) -> str:
    return f"refs/pushed/{target.id}/main/"


def _record_name(target: Target, ref: str) -> str:
    return _main_prefix(target) + ref.removeprefix("refs/")


def _update_refs(archive: Path, commands: list[str]) -> None:
    if commands:
        run(archive, "update-ref", "--stdin", input="start\n" + "".join(commands) + "commit\n")


def forget(archive: Path, target_id: int) -> None:
    """删除档案里该目标的推送记录；目标换地址或删除后，旧记录不再对应任何远端。"""
    if not archive.exists():
        return
    with _locked(archive):
        refs = _read_refs(archive, f"refs/pushed/{target_id}/")
        _update_refs(archive, [f"delete {ref} {oid}\n" for ref, oid in refs.items()])


def bind(archive: Path, target: Target, *, env: dict[str, str] | None = None) -> None:
    for url in (target.main_url, target.history_url):
        if _ls_remote(url, env):
            raise TargetNotEmpty(url)
    blob = run(
        archive,
        "hash-object",
        "-w",
        "--stdin",
        input=f"binding: {secrets.token_hex(16)}\ntarget: {target.id}\n",
    ).stdout.strip()
    body = (
        f"object {blob}\n"
        "type blob\n"
        "tag gw/binding\n"
        f"tagger GitWarden <gitwarden@localhost> {int(datetime.now(UTC).timestamp())} +0000\n"
        "\n"
        "GitWarden binding\n"
    )
    tag_oid = run(archive, "mktag", input=body).stdout.strip()
    run(
        archive,
        "push",
        f"--force-with-lease={BINDING_TAG}:",
        target.history_url,
        f"{tag_oid}:{BINDING_TAG}",
        env=env,
    )
    run(archive, "update-ref", _binding_ref(target), tag_oid)


def _push_history(
    archive: Path, target: Target, pending: list[str], env: dict[str, str] | None
) -> None:
    """推送失败不抛错，由随后的 ls-remote 核对决定结果。"""
    args = [f"--force-with-lease=refs/tags/gw/{u}:" for u in pending]
    args += [f"refs/preserved/{u}:refs/tags/gw/{u}" for u in pending]
    run(archive, "push", target.history_url, *args, check=False, env=env)


def _gate_allows(
    archive: Path, ref: str, remote: str, want: str, confirmed_objects: set[str]
) -> bool:
    if ref.startswith("refs/heads/") and want:
        types = [
            run(archive, "cat-file", "-t", o, check=False).stdout.strip() for o in (remote, want)
        ]
        if types == ["commit", "commit"]:
            ff = run(archive, "merge-base", "--is-ancestor", remote, want, check=False)
            if ff.returncode == 0:
                return True
    return remote in confirmed_objects


def push_target(archive: Path, target: Target, *, env: dict[str, str] | None = None) -> PushResult:
    with _locked(archive):
        binding = rev_parse(archive, _binding_ref(target))
        if binding is None:
            raise NotBound(target.id)
        if _ls_remote(target.history_url, env).get(BINDING_TAG) != binding:
            raise TargetMismatch(target.history_url)

        result = PushResult()
        with telemetry_step("history"):
            local = {
                ref.removeprefix("refs/preserved/"): oid
                for ref, oid in _read_refs(archive, "refs/preserved").items()
            }
            hprefix = _history_prefix(target)
            recorded = {r.removeprefix(hprefix) for r in _read_refs(archive, hprefix)}
            pending = sorted(set(local) - recorded)
            if pending:
                _push_history(archive, target, pending, env)

            remote_hist = _ls_remote(target.history_url, env)
            confirmed: set[str] = set()
            commands = []
            for u, oid in sorted(local.items()):
                r = remote_hist.get(f"refs/tags/gw/{u}")
                if r == oid:
                    confirmed.add(u)
                    if u not in recorded:
                        result.history_pushed.append(u)
                        commands.append(f"update {hprefix}{u} {oid}\n")
                elif r is not None:
                    result.history_conflicts.append(u)
            _update_refs(archive, commands)

            confirmed_objects = {
                run(
                    archive,
                    "for-each-ref",
                    "--format=%(object)",
                    f"refs/preserved/{u}",
                ).stdout.strip()
                for u in confirmed
            }
        _reconcile_main(archive, target, confirmed_objects, result, env)
        return result


def _reconcile_main(
    archive: Path,
    target: Target,
    confirmed_objects: set[str],
    result: PushResult,
    env: dict[str, str] | None,
) -> None:
    mprefix = _main_prefix(target)
    want = _read_refs(archive, "refs/heads", "refs/tags")
    remote = {
        r: o
        for r, o in _ls_remote(target.main_url, env).items()
        if r.startswith(("refs/heads/", "refs/tags/"))
    }
    records = {
        "refs/" + r.removeprefix(mprefix): o for r, o in _read_refs(archive, mprefix).items()
    }
    to_push: list[tuple[str, str]] = []  # (ref, expected)
    with telemetry_step("gate"):
        for ref in sorted(set(want) | set(remote) | set(records)):
            w, r, e = want.get(ref, ""), remote.get(ref, ""), records.get(ref, "")
            if r == w:
                continue
            if r != e:
                result.main_conflicts.append(ref)
            elif r and not _gate_allows(archive, ref, r, w, confirmed_objects):
                result.gate_blocked.append(ref)
            else:
                to_push.append((ref, e))

    with telemetry_step("mirror"):
        _sync_records(archive, target, want, remote, records)
        if result.gate_blocked or result.main_conflicts:
            return

        updates = [(ref, e) for ref, e in to_push if ref in want]
        deletes = [(ref, e) for ref, e in to_push if ref not in want]
        if updates:
            args = [f"--force-with-lease={ref}:{e}" for ref, e in updates]
            args += [f"{ref}:{ref}" for ref, _ in updates]
            run(archive, "push", target.main_url, *args, check=False, env=env)
        if deletes:
            args = [f"--force-with-lease={ref}:{e}" for ref, e in deletes]
            args += [f":{ref}" for ref, _ in deletes]
            run(archive, "push", target.main_url, *args, check=False, env=env)

        remote = {
            r: o
            for r, o in _ls_remote(target.main_url, env).items()
            if r.startswith(("refs/heads/", "refs/tags/"))
        }
        _sync_records(archive, target, want, remote, records)
        result.main_conflicts = [
            ref
            for ref in sorted(set(want) | set(remote))
            if remote.get(ref, "") != want.get(ref, "")
        ]
        result.main_ok = not result.main_conflicts


def _sync_records(
    archive: Path,
    target: Target,
    want: dict[str, str],
    remote: dict[str, str],
    records: dict[str, str],
) -> None:
    """remote == want 的 ref：记录同步为 want（want 为空则删除）。"""
    commands = []
    for ref in set(want) | set(remote) | set(records):
        w, r = want.get(ref, ""), remote.get(ref, "")
        if r != w:
            continue
        name = _record_name(target, ref)
        if w and records.get(ref) != w:
            commands.append(f"update {name} {w}\n")
        elif not w and ref in records:
            commands.append(f"delete {name}\n")
    _update_refs(archive, commands)
    for ref in set(want) | set(remote):
        if remote.get(ref, "") == want.get(ref, ""):
            if want.get(ref):
                records[ref] = want[ref]
            else:
                records.pop(ref, None)


def audit(archive: Path, target: Target, *, env: dict[str, str] | None = None) -> AuditResult:
    with telemetry_step("audit"):
        hprefix = _history_prefix(target)
        recorded = {r.removeprefix(hprefix): o for r, o in _read_refs(archive, hprefix).items()}
        remote = {
            r.removeprefix("refs/tags/gw/"): o
            for r, o in _ls_remote(target.history_url, env).items()
            if r.startswith("refs/tags/gw/") and r != BINDING_TAG
        }
        return AuditResult(
            missing=sorted(set(recorded) - set(remote)),
            modified=sorted(u for u in recorded if u in remote and remote[u] != recorded[u]),
            unknown=sorted(set(remote) - set(recorded)),
        )
