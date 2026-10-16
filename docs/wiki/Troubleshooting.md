# Troubleshooting

Start with the task detail page and **Logs**. A target failure does not erase the local archive; it usually means GitWarden stopped before a remote state could be proven safe.

![Run and activity logs](../images/logs.jpg)

## Service will not start

Check the container output and health endpoint:

```bash
docker compose logs --tail=200 gitwarden
curl --fail http://127.0.0.1:8080/health
```

The most common startup error is a missing, empty, or whitespace-only `GW_ADMIN_PASSWORD`. Invalid numeric settings also stop startup. Check `GW_WORKERS` (1–4), positive `GW_TICK_SECONDS` and `GW_STALL_MINUTES`, minimum interval values of at least 1, and `GW_LOG_RETENTION_DAYS` of at least 1. If changing `GW_PORT`, use that port for health checks and browser access.

If the container starts but the page is unreachable, check that the host port is free and that `GW_BIND` is the address you are opening. The application listens on `0.0.0.0` inside the container; Compose controls host exposure.

## Source fetch fails

Check the source URL and the credential selected on the task. URLs cannot contain embedded credentials and only HTTP(S), SSH, SCP-style SSH, and local absolute-path Git URLs are accepted. For HTTPS, confirm the username/token pair. For SSH, confirm the private key and the exact host key in `known_hosts`; strict host-key checking intentionally rejects an unknown or changed host.

Open the failed run in **Logs** and inspect the redacted `capture` step. Do not copy raw output containing endpoint names or secrets into a public issue. A source marked not visible or source gone is retained locally; fix access or the source endpoint and run the task again.

## Target will not bind

Binding rejects either target repository when `ls-remote` reports any ref. Use two genuinely empty repositories. Do not bind an existing working repository or an existing history repository that may contain another backup's tags. If a platform provisioning attempt left a remote repository after a later error, inspect it and either finish that setup or create a fresh pair according to your retention policy.

The history repository also carries `gw/binding`. If its binding tag does not match the local record, GitWarden reports a binding mismatch instead of adopting the remote. Confirm the target URL and the repository identity; never solve this by force-pushing over an unknown history repository.

## Target run is blocked or conflicted

| Status | Meaning | Safe next action |
| --- | --- | --- |
| `history_conflict` | A `gw/<ULID>` tag exists remotely with a different object. | Stop other writers, preserve evidence, audit the target, and recover from the local volume or an external backup if needed. GitWarden will not overwrite the tag. |
| `gate_blocked` | The main mirror's old remote value is not a fast-forward ancestor and has not been confirmed in history. | Make the history repository reachable and retry. If another writer changed the main mirror, resolve ownership before retrying. |
| `conflict` | The main mirror changed from the value GitWarden last recorded. | Remove the unexpected writer, inspect the remote refs, and retry only after the remote state is understood. |
| `audit_alert` | A previously confirmed history tag is missing or has a different object. | Treat the target as incomplete, stop writes, and restore from the local archive or an independent volume backup. |
| `error` or `failed` | Git, authentication, or a platform operation returned an error. | Read the target step in the run log, correct the reported cause, then use **Retry**. |

The main mirror is not updated when the history gate or remote-value check fails. A target can therefore remain behind while the local archive continues collecting new source states; once the target is healthy, the pending preservation tags are pushed before the main mirror catches up.

## Platform discovery or provisioning fails

Confirm that the instance base URL is a clean HTTP(S) URL and that the access uses an HTTPS credential with the expected username and token scopes. For full discovery/provisioning, use the permissions in [Platforms](Platforms.md). A partial page, missing total count, or API error aborts the discovery transaction; rerun it after fixing the platform response or token.

If a repository is unexpectedly skipped, inspect the access scope, fork/archive/visibility filters, and include/exclude patterns. Pull mirrors are always skipped. A repository owned by another access is reported as Other access rather than duplicated.

If target provisioning created one remote repository and then failed, the error lists the created names. The adapter intentionally leaves them for administrator review and does not delete or adopt them on a retry.

## Incomplete backup warning

The external-content scan checks the latest commit on each collected branch for an LFS filter in `.gitattributes` and for `.gitmodules`. GitWarden retains the Git objects and reports the affected branches, but it does not fetch LFS payloads or submodule repositories. Back up those external stores with a separate process before treating the task as complete.

## Slow, overdue, or busy tasks

The scheduler uses a small shared worker pool. A task or platform access is queued at most once, and an archive lock prevents concurrent writes to the same task. Large initial fetches can run for hours when Git continues to emit progress. `GW_STALL_MINUTES` stops only a no-progress operation. Increase the configured task interval or worker count within the 1–4 worker limit when the overview shows persistent queue pressure.

Repeated failures apply backoff, so the next run may be later than the configured interval. A successful run clears the failure backoff. Use **Run now** for an intentional immediate task run; use **Discover** for a platform access.

## Version or image mismatch after an update

Check the running container's health response rather than the result of `docker compose pull` alone:

```bash
curl --fail http://127.0.0.1:8080/health
docker compose ps
```

Compare `version` with the intended `VERSION`/`vYYYYMMDD.N` release and compare `git_commit` when the release build supplies one. If they differ, recreate the service with the intended `GW_IMAGE` or local build, then check health again.
