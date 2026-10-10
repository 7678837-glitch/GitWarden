from pathlib import Path

from gitwarden.git import run

ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def make_source(path: Path) -> Path:
    run(None, "init", "-b", "main", str(path))
    return path


def commit(repo: Path, msg: str) -> str:
    run(repo, *ID, "commit", "--allow-empty", "-m", msg)
    return run(repo, "rev-parse", "HEAD").stdout.strip()
