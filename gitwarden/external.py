"""识别档案仓中不在备份范围内的外部内容（LFS 文件、子模块）。只检查各分支最新提交。"""

from __future__ import annotations

from pathlib import Path

from gitwarden.git import run
from gitwarden.i18n import msg


def _has_lfs(archive: Path, ref: str) -> bool:
    proc = run(archive, "cat-file", "-p", f"{ref}:.gitattributes", check=False)
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        if "filter=lfs" in line.split("#", 1)[0].split():
            return True
    return False


def _has_submodules(archive: Path, ref: str) -> bool:
    return run(archive, "cat-file", "-e", f"{ref}:.gitmodules", check=False).returncode == 0


def scan_external(archive: Path) -> list[str]:
    refs = run(archive, "for-each-ref", "--format=%(refname)", "refs/heads/").stdout.split()
    lfs: list[str] = []
    submodules: list[str] = []
    for ref in sorted(refs):
        name = ref.removeprefix("refs/heads/")
        if _has_lfs(archive, ref):
            lfs.append(name)
        if _has_submodules(archive, ref):
            submodules.append(name)
    notes = []
    if lfs:
        notes.append(msg("ext.lfs", items=", ".join(lfs)))
    if submodules:
        notes.append(msg("ext.submodules", items=", ".join(submodules)))
    return notes
