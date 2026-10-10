# Backup and recovery

GitWarden keeps collected Git objects in a local bare archive. Each target holds the current refs in its main mirror and overwritten or deleted versions in a separate history repository. SQLite records tasks, events, and remote confirmations. Back up the complete application volume and protect both target repositories.

## What happens during a run

For each task, GitWarden:

1. fetches branch and tag refs into an incoming namespace;
2. classifies creates, fast-forwards, force pushes, tag rewrites, and deletions;
3. creates a local preservation tag for every old object that would be overwritten or deleted;
4. commits the current mirror refs in the local archive;
5. pushes new preservation tags to the history repository and confirms them with `ls-remote`;
6. checks the main mirror's remote values and updates it only when the old value is a fast-forward ancestor or has a confirmed matching preservation object;
7. audits the history repository against the local confirmation records.

Preservation tags are named `gw/<ULID>`. Their annotated message records the source task, original ref, old and new object IDs, event type, and time. A target binding tag named `gw/binding` identifies the history repository expected by the local record. GitWarden does not overwrite an existing `gw/<ULID>` tag; a changed or missing remote tag is a target conflict or audit alert.

![Backup wizard](../images/backup-wizard.jpg)

## Recover a preserved version from a target

The history repository contains preserved versions only. It does not contain every current branch and tag, so clone the main mirror when you need the current collected state and use the history repository for an older version.

Clone the history repository and list its preservation tags:

```bash
git clone https://example.org/invalid/history.git sample-history
git -C sample-history tag --list 'gw/*'
read -r -p "Preservation tag (gw/...): " preserved_tag
git -C sample-history show "$preserved_tag"
```

For a preserved commit or branch tip, create a working branch from the peeled object:

```bash
git -C sample-history checkout -b restore-gw "${preserved_tag}^{}"
```

For a rewritten annotated tag, inspect `git show gw/<ULID>` first. The preservation tag wraps the original tag object; `gw/<ULID>^{}` peels it to the underlying commit for a working-tree restore. Preserve the original tag object separately when its signature or annotation is part of the recovery requirement.

The task detail page prints the exact history URL and `gw/<ULID>` for every event with a preservation record. Use that command block instead of guessing a tag from an event timestamp.

## Download an event bundle

Open the event on the task detail page and choose **Download bundle**. The bundle is generated from the exact local preservation ref for that event. Verify and fetch it into an empty recovery repository:

```bash
mkdir recovered
git -C recovered init
git -C recovered bundle verify ../event.bundle
preserved_ref=$(git bundle list-heads event.bundle | awk '{print $2}')
git -C recovered fetch ../event.bundle "$preserved_ref:$preserved_ref"
git -C recovered show "$preserved_ref"
git -C recovered checkout -b restore-gw "${preserved_ref}^{}"
```

Replace `event.bundle` with the downloaded filename. Each event bundle contains one preservation ref; the command reads that ref directly from the bundle. The bundle endpoint requires a logged-in WebUI session and is available only for events that have a preserved ref.

## Back up the GitWarden volume

Stop the service so SQLite and the bare archives are quiescent, archive the named volume, and start the service again:

```bash
docker compose stop
docker run --rm \
  --volume gitwarden-data:/data:ro \
  --volume "$PWD":/out \
  alpine:3.20 \
  sh -c 'umask 077; tar czf /out/gitwarden-data-backup.tgz -C /data .'
docker compose start
```

Back up `.env` separately through the operator's secret-management process. It contains the administrator password and is not stored in `gitwarden-data`. Check the archive before relying on it:

```bash
tar tzf gitwarden-data-backup.tgz | head
```

The same approach works with plain Docker by replacing `docker compose stop/start` with `docker stop/start gitwarden`.

## Restore a volume backup

Restore into a **new volume** so the current data remains available. Stop the service and unpack the backup:

```bash
docker compose stop
docker volume create gitwarden-restored
docker run --rm \
  --volume gitwarden-restored:/data \
  --volume "$PWD":/in:ro \
  alpine:3.20 \
  tar xzf /in/gitwarden-data-backup.tgz -C /data
```

Create `compose.restore.yml` in the project directory:

```yaml
volumes:
  gitwarden-data:
    name: gitwarden-restored
```

Start against the restored volume and inspect the health response, task statuses, and target audits:

```bash
docker compose -f compose.yml -f compose.restore.yml up -d --no-build
curl --fail http://127.0.0.1:8080/health
```

Continue using both Compose files when operating the restored deployment. The original `gitwarden-data` volume remains unchanged. Existing enabled schedules resume when the service starts; perform recovery drills in an isolated environment when you do not intend to contact the configured repositories.

## Recovery choices

| Available data | What it can recover |
| --- | --- |
| Main mirror only | The current refs that were last mirrored there. Older rewritten or deleted refs require the separate history repository or local archive. |
| History repository only | The versions represented by its `gw/*` tags and the underlying Git objects. It is not a complete current mirror. |
| GitWarden data volume | The local archive, all collected preservation records, task metadata, and stored credentials needed to reconnect to private endpoints. |
| Event bundle | The single preservation ref selected when the bundle was downloaded. |

When a source disappears, do not delete the task or archive. The task can be marked source-gone or not-visible by discovery while its collected archive, events, and target history remain available for recovery.
