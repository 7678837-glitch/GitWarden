# GitWarden

> Mirror the present. Preserve the past.

GitWarden is a self-hosted Git history mirror. It periodically fetches a source repository into a local archive and pushes the collected state to one or more writable Git targets. It does not require a hosting-platform API for the core workflow, so a source or target can be any supported Git URL, including a manually configured GitHub repository.

[简体中文](README.zh-CN.md)

![GitWarden overview](docs/images/overview.jpg)

## What it protects

Each backup task has one source, a durable local archive, and one or more targets. A target has two repositories:

- a **main mirror** containing the current `refs/heads/*` and `refs/tags/*` state;
- a private **history repository** containing GitWarden's `gw/*` preservation tags.

When a collected branch is force-pushed or deleted, or a collected tag is rewritten, GitWarden preserves the old Git object in the local archive before changing its mirror references. It then publishes that preserved object to the history repository before the main mirror update is allowed. A normal fast-forward needs no extra preservation tag. The process is automatic; there is no approval queue for ordinary Git changes.

GitWarden protects states it has actually collected. A change that appears and disappears between polling runs cannot be recovered. Git objects are in scope; LFS payloads and submodule repositories are not copied. The UI detects these references and marks the task as an incomplete backup so that the limitation is visible.

The history repository is not a second full mirror. It contains preserved versions, while the current refs remain in the main mirror and the local archive. Keep the main mirror or the local data volume when you need the latest collected state.

## Quick start with Docker Compose

Get a checkout of this repository and run the following from its project directory:

```bash
cp .env.example .env
```

Set the required administrator password in `.env`:

```dotenv
GW_ADMIN_PASSWORD=replace-this-before-start
```

Start the service and open <http://127.0.0.1:8080>:

```bash
docker compose up -d --build
docker compose logs -f gitwarden
```

The Compose configuration uses the project `.env`, the named volume `gitwarden-data`, and `127.0.0.1:8080`. The default image is built locally as `gitwarden:local`; Compose selects the host architecture automatically. The container runs as UID/GID `10001` and stores the application database, archives, credentials, and temporary work data under `/data`.

The first public release image will be pushed to GHCR only after its `vYYYYMMDD.N` release tag passes CI. Until a public owner/repository and release image have been assigned, use the local build above. Once the published image path is known, set `GW_IMAGE` and pull without rebuilding:

```bash
GW_IMAGE=ghcr.io/sample/gitwarden:20261010.1 docker compose pull
GW_IMAGE=ghcr.io/sample/gitwarden:20261010.1 docker compose up -d --no-build
```

The `sample` path is an example only. Replace it with the published path for the release you intend to run.

For a non-Compose deployment, see [Deployment](docs/wiki/Deployment.md). For configuration, persistent-volume backups, and recovery, see the [Wiki source index](docs/wiki/Home.md).

## Create the first backup

1. Log in with `GW_ADMIN_PASSWORD`. The WebUI defaults to English and can switch to Simplified Chinese.
2. Open **Credentials** and save an HTTPS username/token or an SSH private key plus the matching `known_hosts` entry when the source or target is private. Leave the credential empty only for an anonymous endpoint. Never put credentials in a Git URL.
3. Choose **Back up a repository**. Step 1 records the source URL and sync interval. Step 2 either enters two manual target URLs or provisions a pair through a configured Gitea/Forgejo access. Step 3 requires confirming the target safety assumptions before creation.
4. Run the task once, then check the target card and **Logs**. The target card shows whether the main mirror is in sync, how many preservation records are confirmed in the history repository, and when the last audit ran.

For manual targets, both repositories must already exist and be empty when they are bound. Use a private main mirror and a private history repository. Give GitWarden the only write path to both repositories, and prevent CI or webhooks from reacting to backup pushes. A platform provisioned by GitWarden creates `<name>` and `<name>-history`, makes both private, disables repository features such as Actions, and protects `gw/*` on the history repository before binding them.

![Task detail and history status](docs/images/task-detail.jpg)

## Supported endpoints and platform extensions

The core accepts anonymous, HTTPS, SSH, SCP-style SSH, and local absolute-path Git endpoints. HTTPS credentials are passed through Git's askpass mechanism; SSH credentials use a temporary key file, strict host-key checking, and are removed after the operation.

The optional platform integration currently supports **Gitea** and **Forgejo**. It can discover repositories, apply owner and pattern filters, create backup targets, harden them, and report repository/organization/system webhook metadata available to the configured token. A platform access requires an HTTPS credential with a username and token. The integration token is expected to have `read:admin`, `write:repository`, `write:organization`, and `write:user` as needed by the selected discovery and provisioning operations.

GitHub has no discovery or provisioning adapter. Add a GitHub source and its two target URLs manually in the three-step wizard, then use the same empty-target and single-writer rules.

## Operating model

The scheduler defaults to two workers, a five-minute minimum task interval, a fifteen-minute minimum discovery interval, and a ten-minute no-progress stall timeout. The interval can extend when recent runs are large and backs off after repeated failures. A run with Git progress may continue for as long as needed; only the no-progress threshold stops it.

The overview highlights incomplete tasks, target conflicts, gate blocks, source visibility changes, and overdue work. **Logs** contains run steps, ref changes, redacted Git output, and administrator activity. Run and activity records are retained for 90 days by default; preservation events, Git archives, and Git data are not removed by that cleanup.

Use `/health` for a read-only release check:

```bash
curl --fail http://127.0.0.1:8080/health
```

The response includes the application version and the build's Git commit when the image was built with one.

## Documentation

- [Wiki home](docs/wiki/Home.md)
- [Getting started](docs/wiki/Getting-started.md)
- [Deployment](docs/wiki/Deployment.md)
- [Configuration](docs/wiki/Configuration.md)
- [Backup and recovery](docs/wiki/Backup-and-recovery.md)
- [Platforms](docs/wiki/Platforms.md)
- [Troubleshooting](docs/wiki/Troubleshooting.md)

[Release validation](docs/Validation.md)

## License

GitWarden is licensed under the [Apache License 2.0](LICENSE). Third-party dependencies retain their own licenses.

The public owner/repository URL is still to be assigned before publishing repository and image links.
