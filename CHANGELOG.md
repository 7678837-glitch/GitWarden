# Changelog

## 20261010.1 — 2026-10-10

First public release candidate.

- One-way Git backups over HTTPS, SSH, or local Git paths, with multiple targets per source.
- Preserve captured history before following force pushes, rewritten tags, and deleted references. Keep preserved objects locally and in a separate target history repository.
- Require empty target repositories at binding time and verify preserved history before replacing target references.
- Download recovery bundles and audit target history.
- Optional Gitea and Forgejo discovery, include/exclude rules, and target provisioning.
- Shared scheduling with bounded concurrency, adaptive intervals, failure backoff, and inactivity detection.
- English and Simplified Chinese WebUI, task status, run details, and activity logs.
- English-first documentation, deployment guides, Wiki sources, and screenshots using demonstration data.
- Container builds for Linux amd64 and arm64, Docker Compose and Docker deployment, and GitHub Actions validation and image publishing.
- Redact credentials inside serialized status parameters before storing logs.
- Keep target bindings during credential rotation and show newly provisioned targets as pending until their first sync.
- Fix narrow-screen credential layout, rule-mode labels, and task editing navigation.

Git LFS payloads and submodule repositories are detected but are not backed up. Only Git history already captured by GitWarden can be preserved. This release does not migrate databases from development snapshots.
