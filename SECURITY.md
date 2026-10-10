# Security

GitWarden is a single-administrator service intended for trusted networks. Keep the default loopback binding, or place it behind an authenticated HTTPS access layer. Do not expose the service directly to the public internet.

Credentials are stored in the local SQLite database with restrictive file permissions; they are **not encrypted at rest**. Protect the data volume and its backups. Treat source code archives and recovery bundles as confidential when the source repository is private.

SSH connections require a supplied `known_hosts` entry and strict host-key verification. Give source credentials read access and target credentials only the access needed to write the designated backup repositories. Platform-wide discovery can require broader permissions; see [Platforms](docs/wiki/Platforms.md).

Only GitWarden should write to its target repositories. Target repositories must be private, with CI and webhooks disabled or otherwise prevented from executing mirrored code. Protect the `gw/*` tags in the history repository from other writers.

Login sessions are held in memory and are cleared when the service restarts. Stop the service before taking a consistent backup of the complete data volume.

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** feature when private reporting is enabled. Do not put credentials, exploit details involving a live service, or private repository contents in a public issue. If private reporting is unavailable, open a minimal issue requesting a private contact channel without including sensitive details.
