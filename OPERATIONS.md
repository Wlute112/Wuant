# Unattended Paper-Trading Operations

Live capital remains disabled. These controls harden long-only IBKR equity
paper trading and collect the evidence needed for a later production review;
they do not prove profitability or make IBKR/TWS itself continuously available.

## Preflight

Run the launcher from the repository root:

```bash
./quant doctor
./quant doctor --require-ibkr --ibkr-port 7497
```

The remaining `python -m quant...` examples are run from the directory that
contains the `quant/` repository, matching the existing project commands.

The command fails on the wrong Python version, dependency conflicts, low disk,
SQLite corruption, unavailable Redis, or Redis without AOF durability. The IBKR
socket is a warning unless `--require-ibkr` is supplied. macOS clock sync is
reported as a manual verification because macOS does not expose a dependable
unprivileged NTP-status API.

## Dashboard paper jobs

Every paper job started by the dashboard now creates two detached jobs:

1. `quant.run.run_live`, with Redis strategy persistence and the shared SQLite
   operations database at `quant/jobs/operations.sqlite3`.
2. `quant.ops.supervisor`, which reads independently written telemetry, emits a
   heartbeat, records paper-campaign observations, sends alerts, and writes
   durable commands back to the strategy.

The strategy requires the supervisor heartbeat after its startup grace period.
A missing/stale heartbeat freezes entries. The supervisor independently checks
drawdown, daily loss, gross leverage, execution certainty, reconciliation,
telemetry freshness, market-data age, and data-quality state. Depending on the
failure it issues `FREEZE_ENTRIES`, `FLATTEN`, or permanent `KILL`. Canceling the
paper job also cancels its companion supervisor before stopping Nautilus.

Manual audited controls are available locally:

```bash
quant/.quant312/bin/python -m quant.scripts.operations_control \
  --target strategy:paper_0123456789 \
  --action FREEZE_ENTRIES \
  --operator local-user \
  --reason "operator inspection"

quant/.quant312/bin/python -m quant.scripts.operations_control \
  --target strategy:paper_0123456789 \
  --action FLATTEN \
  --operator local-user \
  --reason "manual emergency exit" \
  --confirm "FLATTEN strategy:paper_0123456789"
```

Commands survive process restarts. Cancel/flatten/kill commands remain
acknowledged until the strategy confirms there are no broker positions, open
orders, or in-flight orders.

## Audit and alerts

The operations database contains a SHA-256 chained, trigger-protected audit log,
leased control commands, heartbeats, alert cooldown state, and paper-validation
observations. The default alert sink is an fsynced JSONL file beside the
operations database. Optional external sinks use environment variables only:

```bash
export QUANT_ALERT_WEBHOOK_URL='https://...'
export QUANT_ALERT_SMTP_HOST='smtp.example.com'
export QUANT_ALERT_SMTP_PORT=587
export QUANT_ALERT_SMTP_FROM='quant@example.com'
export QUANT_ALERT_SMTP_TO='operator@example.com'
export QUANT_ALERT_SMTP_USERNAME='...'
export QUANT_ALERT_SMTP_PASSWORD='...'
```

Secrets are neither accepted in dashboard job configuration nor persisted in
the job registry/audit database.

## Paper validation and model promotion

Campaign IDs are stable across dashboard jobs with the same asset class,
sorted ticker universe, and bar cadence. Evaluate a campaign with:

```bash
quant/.quant312/bin/python -m quant.ops.validation \
  --operations-db quant/jobs/operations.sqlite3 \
  --campaign-id 'paper:equity:QQQ:1' \
  --out quant/runs/paper-validation.json
```

Defaults require 20 clean UTC days, 120 runtime hours, 99.5% healthy
observations, at least 20 fills, no rejection, no uncertain reconciliation, and
drawdown no greater than 10%. Any unhealthy observation resets the clean-period
clock.

Register immutable parameter and optimization evidence:

```bash
quant/.quant312/bin/python -m quant.ops.model_registry register \
  --params quant/optimize/best_params.json \
  --optimization quant/runs/optimize_RUN_ID.json

quant/.quant312/bin/python -m quant.ops.model_registry evaluate \
  --model-id model_ID \
  --campaign-report quant/runs/paper-validation.json \
  > quant/runs/model-evidence.json
```

Approval requires eligible evidence, artifact checksum verification, an
operator identity, and the exact phrase `APPROVE MODEL model_ID FOR LIVE`.
Approval only changes the model registry: it cannot bypass
`quant.run.readiness`, whose live-capital gate remains locked. `run_live.py`
accepts `--model-id` and rejects a non-active/non-approved model in live mode.
The registry retains the previously approved model for explicit rollback.

## Backups and restore drills

Create a consistent SQLite backup plus a type-preserving Redis logical dump:

```bash
quant/.quant312/bin/python -m quant.ops.backups create \
  --destination /Volumes/EncryptedBackup/quant --retain 56
```

Verify and restore only during an outage/change window:

```bash
quant/.quant312/bin/python -m quant.ops.backups verify --backup-dir /path/to/quant-backup-TIMESTAMP
quant/.quant312/bin/python -m quant.ops.backups restore \
  --backup-dir /path/to/quant-backup-TIMESTAMP \
  --confirm "RESTORE QUANT STATE"
```

Stop trading, the API, news ingestion, and Redis writers before a restore.
Always run `./quant doctor` afterward. Keep the destination on encrypted,
off-host or independently synced storage; a backup on the same laptop is not a
disaster-recovery copy.

Generate a six-hour macOS backup schedule (review before adding `--install`):

```bash
quant/.quant312/bin/python -m quant.scripts.install_backup_launchd \
  --destination /Volumes/EncryptedBackup/quant
```

Old terminal job logs/configs can be previewed and then removed without
touching run artifacts or active jobs:

```bash
quant/.quant312/bin/python -m quant.scripts.cleanup_jobs --older-than-days 30
quant/.quant312/bin/python -m quant.scripts.cleanup_jobs --older-than-days 30 --apply
```

## Watchdog and macOS launchd

Copy `quant/ops/services.example.json`, replace every absolute path, then test:

```bash
quant/.quant312/bin/python -m quant.ops.watchdog \
  --config /absolute/path/services.json \
  --operations-db /absolute/path/operations.sqlite3
```

Generate a launchd plist without installing it:

```bash
quant/.quant312/bin/python -m quant.scripts.install_launchd_services \
  --config /absolute/path/services.json \
  --operations-db /absolute/path/operations.sqlite3
```

Review the generated plist, then repeat with `--install`. The watchdog uses a
bounded restart budget and exponential backoff. Exhausting the budget records a
critical event and sends an alert instead of entering an infinite crash loop.

Disable macOS sleep while an unattended session is expected, use wired power
and networking where possible, and configure TWS/IB Gateway auto-restart and
paper login. No local code can recover an expired IBKR login, exchange outage,
host power loss, router failure, or unsupported broker/API behavior without
external infrastructure and real paper validation.
