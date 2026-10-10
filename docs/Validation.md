# Release validation — 20261010.1

Validation used local disposable Git repositories, SQLite databases, Gitea and Forgejo containers, and synthetic credentials. Public screenshots in [images](images/) use demonstration names and example domains.

## Automated checks

| Check | Result |
| --- | --- |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check .` | 67 files passed |
| `uv run pytest -q` and the four local-network cases with port-binding permission | 175 passed in total; 28 opt-in integration cases skipped |
| `GW_IT=1 uv run pytest -q tests/integration` | 28 passed against real local Gitea and Forgejo |
| Affected UI, credential-redaction and target-rotation tests after browser fixes | 48 passed |
| Platform provisioning and force-push regression after the initial-status fix | 2 passed, one per platform |
| OSV query for all 27 locked Python packages | No known vulnerability returned at validation time |

The credential-redaction regression first failed for all three tested quoted/backslash secrets, then passed. The target-credential rotation regression first failed because the target lost its binding, then passed against real Git repositories and SQLite. The browser also reproduced the rotation failure before the fix.

## Actual browser acceptance

The running application was operated in a real browser; the results below are separate from request-level tests.

| Area | Observed behavior |
| --- | --- |
| Authentication and language | Wrong-password feedback, successful sign-in, authenticated and anonymous language switching, logout, and protected-page redirect |
| Backup wizard | Manual URLs and platform creation, returning to earlier steps, retained inputs, required confirmations, creation, binding and first successful run |
| Credentials | HTTPS token and SSH key creation; hidden saved secrets; in-use deletion disabled; unused deletion; target credential rotation followed by successful sync |
| Tasks | Editing, enable/disable, all five bulk actions, name search, status/management/completeness filters, and next/previous pagination using 58 fixture tasks |
| Targets | Add and bind, Gitea/Forgejo provisioning, independent retry, audit, editing, removal while remote repositories remain intact |
| Discovery | Instance/access creation, owner scope, include/exclude mode switching, preview, discard, save/apply, immediate discovery, excluded tasks, source-not-visible/source-gone, and access removal retaining tasks |
| History and recovery | Force push, tag rewrite and branch deletion; both remote targets confirmed 3/3 preserved records; browser-downloaded bundle restored the exact old commit and passed `git fsck` using the documented recovery method |
| Logs | Run and activity tabs, task/result/period/category filters, ref search, pagination, nested step/ref/raw-output expansion |
| Visual checks | Desktop screenshots of the seven main views; 390-pixel viewport checks in Chinese; no page-level horizontal overflow after the credential-table fix |

Browser findings addressed include credential rotation breaking binding, premature healthy status after provisioning, stale rule labels, closed edit/add-target sections behind navigation links, misleading backup-attention wording, completed-run progress displaying an irrelevant Git probe error, and credential-page overflow. Wide data tables retain their own horizontal scroll area on narrow screens.

## Containers and publication boundary

Linux amd64 and arm64 images were built locally. Both passed health/login checks, non-root UID verification, and persisted database contents after container removal and recreation. The repository's Compose configuration also passed startup with `.env`, a named volume, and a non-default port. Temporary test containers and volumes were removed.

The GitHub Actions workflow is prepared to run lint, tests, both architecture builds and startup checks before publishing versioned GHCR images. GitHub-hosted execution, registry publication and a separately published GitHub Wiki have not been performed. The public repository identity still requires a maintainer decision. GitWarden uses [Apache License 2.0](../LICENSE). No Git or container image was pushed.
