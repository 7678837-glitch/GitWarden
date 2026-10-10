from gitwarden.archive import capture, init_archive
from gitwarden.external import scan_external
from gitwarden.git import run
from gitwarden.i18n import msg
from tests.conftest import ID, commit, make_source

LFS_LINE = "*.bin filter=lfs diff=lfs merge=lfs -text\n"


def add_file(repo, name, text):
    (repo / name).write_text(text)
    run(repo, "add", name)
    run(repo, *ID, "commit", "-m", f"add {name}")


def scan(tmp_path, src):
    archive = tmp_path / "a.git"
    init_archive(archive)
    capture(archive, str(src), "t")
    return scan_external(archive)


def test_plain_repo_has_no_external_content(tmp_path):
    src = make_source(tmp_path / "src")
    commit(src, "one")
    assert scan(tmp_path, src) == []


def test_gitattributes_with_lfs_filter_is_detected(tmp_path):
    src = make_source(tmp_path / "src")
    add_file(src, ".gitattributes", LFS_LINE)
    assert scan(tmp_path, src) == [msg("ext.lfs", items="main")]


def test_commented_lfs_filter_is_ignored(tmp_path):
    src = make_source(tmp_path / "src")
    add_file(src, ".gitattributes", "# " + LFS_LINE + "*.txt text # filter=lfs\n")
    assert scan(tmp_path, src) == []


def test_gitmodules_is_reported_as_submodule(tmp_path):
    src = make_source(tmp_path / "src")
    add_file(src, ".gitmodules", '[submodule "x"]\n\tpath = x\n\turl = https://example.com/x.git\n')
    assert scan(tmp_path, src) == [msg("ext.submodules", items="main")]


def test_only_branch_with_lfs_is_listed(tmp_path):
    src = make_source(tmp_path / "src")
    commit(src, "one")
    run(src, "checkout", "-b", "dev")
    add_file(src, ".gitattributes", LFS_LINE)
    assert scan(tmp_path, src) == [msg("ext.lfs", items="dev")]
