# Contributing

Read the [user guide](docs/wiki/Home.md) before changing backup behavior. GitWarden's primary contract is to preserve captured Git history before mirroring a destructive upstream change.

## Local development

Use Python 3.14, Git, OpenSSH, and [uv](https://docs.astral.sh/uv/). Docker with the Compose plugin is required for platform integration tests.

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
GW_IT=1 uv run pytest -q tests/integration
```

The final command creates disposable Gitea and Forgejo containers on localhost ports 18300, 18301, 18322, and 18323, then removes their containers and volumes. Do not run multiple integration suites at once.

To start a local instance, set `GW_ADMIN_PASSWORD` in your shell, then run:

```sh
export GW_DATA_DIR=./data
uv run python -m gitwarden
```

The application binds to all interfaces when run directly. For local-only access, use the Docker deployment's default loopback port mapping. The Python application does not read `.env` itself.

## Changes and tests

- Keep changes scoped to a concrete behavior. Add a failing regression test before changing established logic.
- Use real temporary Git repositories and SQLite databases for backup and persistence tests.
- Keep `gitwarden/i18n/en.json` and `gitwarden/i18n/zh_CN.json` in sync when adding UI text.
- Update both READMEs and the relevant guide when public behavior changes.
- Run the affected tests during development and the full checks before submitting a pull request.

Never include credentials, runtime databases, repository archives, private hostnames, personal contact details, or unrelated background material in a contribution. Use example domains and disposable test credentials.

Release instructions and repository metadata are in [Publishing](docs/Publishing.md).
