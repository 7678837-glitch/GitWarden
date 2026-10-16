# Configuration

GitWarden resolves settings in this order:

1. environment variables;
2. the TOML file at `$GW_DATA_DIR/config/gitwarden.toml` (or the path in `GW_CONFIG`);
3. built-in defaults.

In the container, `GW_DATA_DIR` defaults to `/data`. In a source checkout, the application default is `./data`. The TOML example in `data/config/gitwarden.toml.example` shows the same sections and defaults. The administrator password is the exception: it is read from `GW_ADMIN_PASSWORD` only and is never loaded from TOML.

## Container and process settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `GW_ADMIN_PASSWORD` | required | WebUI administrator password. Startup fails when it is empty or whitespace-only. Other passwords are preserved exactly, including surrounding whitespace. |
| `GW_DATA_DIR` | `/data` in the image, `./data` otherwise | SQLite database, archives, credentials, and work directory. |
| `GW_CONFIG` | `$GW_DATA_DIR/config/gitwarden.toml` | Optional TOML configuration path. |
| `GW_PORT` | `8080` | WebUI port and Compose host/container port; both default to `8080`. |
| `GW_TICK_SECONDS` | `30` | Scheduler polling tick. Must be greater than zero. |
| `GW_WORKERS` | `2` | Scheduler worker count, from 1 through 4. |
| `GW_MIN_SYNC_MINUTES` | `5` | Minimum interval accepted for a task. Minimum allowed setting is 1. |
| `GW_MIN_DISCOVERY_MINUTES` | `15` | Minimum interval accepted for a platform access. Minimum allowed setting is 1. |
| `GW_STALL_MINUTES` | `10` | Stops a Git operation after this many minutes without progress. It does not cap a progressing run. |
| `GW_LOG_RETENTION_DAYS` | `90` | Retention for run and activity records. Must be at least 1. |

Compose also reads these project-environment values:

| Variable | Used by | Meaning |
| --- | --- | --- |
| `GW_BIND` | Compose | Host address for the WebUI port mapping; defaults to `127.0.0.1`. |
| `GW_IMAGE` | Compose | Image name. Defaults to the local `gitwarden:local` build. |

`GW_BIND` and `GW_IMAGE` are Compose interpolation inputs, not application settings. Docker Compose loads `.env` in the project directory; the Python application does not automatically load that file. With plain Docker, pass the file using `--env-file .env` or export the variables yourself.

## TOML settings

Use a file such as `/data/config/gitwarden.toml` when a setting should remain with the data volume:

```toml
[server]
port = 8080

[scheduler]
tick_seconds = 30
workers = 2
min_sync_minutes = 5
min_discovery_minutes = 15
stall_minutes = 10

[logs]
retention_days = 90
```

Environment variables override the corresponding TOML values. Invalid numbers or values outside the validation range stop startup with an explanatory error. A task's sync interval and a platform access's discovery interval must also satisfy the configured minimum when saved in the WebUI.

## Scheduling behavior

Hand-created tasks default to a 60-minute sync interval. Platform-discovered tasks inherit the access's default sync interval, which is 60 minutes unless changed. Platform discovery also defaults to 60 minutes. These are task/access values; the `GW_MIN_*` settings are lower bounds.

After a run, GitWarden calculates the next due time from the run's end. The effective interval can be larger than the configured value when the recent successful runs are long, and repeated failures apply exponential backoff up to six hours. A successful run clears the failure backoff. A Git operation with progress may run for hours; `GW_STALL_MINUTES` only detects a no-progress stall.

## Data layout

The application data directory contains:

```text
/data/
├── gitwarden.db          # SQLite metadata and logs
├── archives/*.git        # bare local Git archives
├── config/               # optional TOML configuration
└── work/                 # temporary credential and bundle work files
```

The archive and SQLite database are both required for a complete local recovery. Credentials are stored in the database and are needed to contact private sources and targets; protect the volume and the `.env` file as secrets.

## WebUI language and logging

The WebUI starts in English and has a Simplified Chinese switch. Each task can enable detailed Git logging. Errors always retain redacted output; successful runs retain full redacted Git output only when detailed logging is enabled. One raw output record is limited to 64 KiB. Log retention removes run and activity records, not preservation events, archives, or Git objects.
