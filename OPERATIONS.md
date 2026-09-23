# Unattended Paper-Trading Operations

Priorities and remaining work are tracked in the single
[product readiness backlog](PRODUCTION_READINESS.md). This document retains
its specifications, evidence and operating procedures.

Live capital remains disabled. These controls harden IBKR equity paper trading
and collect the evidence needed for a later production review;
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

## Execution session policy

The Paper/Live dashboard's **Session policy** controls configure regular,
extended or custom entry windows, opening/closing buffers, auction-period
participation, session-end entry cancellation and overnight PnL ownership.
Custom windows use the instrument's exchange timezone and are clipped to
broker trading hours; holidays and early closes remain authoritative. Enable
**Include extended hours** for extended/custom policies so data and routing
cover those windows. Regular-only entry policy can also use extended-hours data.

API execution requests accept the same policy in `session_policy`. The CLI
accepts an inline JSON object, for example:

```bash
quant/.quant312/bin/python -m quant.run.run_live \
  --asset-class equity --tickers QQQ --port 7497 \
  --include-extended-hours \
  --session-policy-json '{"mode":"CUSTOM","custom_windows":[["10:00","12:00"],["13:00","15:00"]],"overnight_pnl_assignment":"PRIOR_SESSION"}'
```

Buffers default to 5 minutes after open, 5 minutes before close, and no new
entries in the final 15 minutes of each window. Auction-period participation
does not override buffers or request a dedicated auction order type. Resting
entries are canceled at each configured window end when cancellation is enabled;
protective and exit orders are retained. Overnight PnL can belong to the prior
or next actual trading session, skipping closed holidays/weekends. Intraday
account updates within broker trading hours belong to that exchange session.
After the last known schedule interval, the last known session key is retained
until a new broker schedule is available; midnight alone cannot reset risk.
The existing 24-hour daily-loss halt is not shortened by changing session keys.

The job retains its policy for review, and broker telemetry reports the active
policy, session key and current entry decision. Existing job status, logs and
Stop/Cancel actions apply. Invalid or conflicting policies are rejected before
broker startup. Supported TWS/Gateway validation remains outstanding.

## Short-enabled paper jobs

Shorting remains off unless the job explicitly passes `--allow-shorts` or the
dashboard switch is enabled. The runner opens a separate TWS client (default
ID 29) for live shortable shares/tier, halt and Rule-201 inputs, and IBKR
account/what-if checks. Current indicative fee and availability data come from
IBKR's `usa.txt` short-stock feed. Client IDs 1, 29, 30 and 31 are reserved by
default for execution, short controls, news and the dashboard broker monitor.

```bash
quant/.quant312/bin/python -m quant.run.run_live \
  --asset-class equity --tickers QQQ --port 7497 \
  --account-id "$TWS_ACCOUNT" --allow-shorts \
  --short-max-borrow-fee-pct 5 \
  --short-min-margin-cushion-pct 20 \
  --short-locate-buffer-ratio 1.25
```

A short entry is always a DAY limit order and is submitted only after the
inventory, fee, halt, Rule-201, PDT, account-margin and order-specific what-if
checks pass. A persistent control failure on an open short starts the configured
grace period and then requests a buy-to-cover. The detached risk
supervisor independently freezes entries when the control infrastructure is
unhealthy and flattens when telemetry reports an active short-control breach.

Manual audited controls are available in the Paper dashboard under
**Live telemetry → Operator safety controls** and through the CLI below.
Configure `QUANT_CONTROL_TOKEN` with a cryptographically random secret of at
least 32 characters in the API server environment, and optionally set
`QUANT_CONTROL_OPERATOR` to the operator name (default `local-operator`).
Restart the API to load its environment, then enter the token in the panel.
The panel holds the credential only in memory and clears it when locked or when
the selected execution job changes. Never put it in frontend build variables,
URLs, saved strategy parameters or source control.

Controls are restricted to localhost and registered paper jobs. The token does
not enable remote administration or live capital. Every command records the
server-configured operator and a reason. Resume, cancel-all, flatten and kill
require typing the displayed action and exact strategy target. Safety requests
can be queued while a running strategy has a stale heartbeat, but remain visibly
pending until it responds. Resume requires a fresh heartbeat and passing
interlocks both at request time and when the node processes it; it expires after
30 seconds. Resume cannot override uncertainty, a daily halt or permanent kill.

`CANCEL_ALL` now freezes entries and cancels a captured set of orders only for a
flat strategy book; it does not flatten positions. A racing fill fails the
command while preserving any newly created protection. Use `FLATTEN` to exit
positions. Flatten and kill remain acknowledged until the broker cache confirms
no strategy positions or working/inflight orders. Only unclaimed `PENDING`
requests can be cancelled. Retry an uncertain submission with the panel's
**Retry request** button to reuse its durable request ID.

Operator freezes and kills are durably recorded immediately in the operations
database, keyed by account and strategy identity, so a new dashboard job or
unclean restart cannot silently erase them. Supervisor recovery cannot undo an
operator freeze. A permanent kill has no dashboard reset action.

Equivalent CLI controls:

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

### Encrypted off-host recovery (P1-07)

Paper/Live → **Live telemetry → Backup & recovery** provides local authenticated
configuration, initialization, backup, full integrity checks, remote snapshot
inventory, cancellation and isolated restore evidence. It uses the same
`QUANT_CONTROL_TOKEN` as operator safety controls. Restic and the Redis CLI tools
must be installed on the API host (`brew install restic redis` on macOS).

Configure a dedicated `sftp:user@host:/absolute/repository` destination, an
owner-only (`0600`) repository-password file outside this project, and the actual
execution Redis host/port. SSH must already work noninteractively with a verified
host key; unknown or changed host keys fail. Redis authentication uses the
server's `QUANT_RECOVERY_REDIS_USERNAME` / `QUANT_RECOVERY_REDIS_PASSWORD`, falling
back to `NAUTILUS_REDIS_USERNAME` / `NAUTILUS_REDIS_PASSWORD`. Credentials are never
submitted as browser fields or logged. Back up the encryption password and SSH
recovery credentials independently; losing the repository password loses access
to the snapshots. Configuration alone does not prove a host is physically remote.
See the [Restic SFTP and password documentation](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#sftp).

The defaults are a **6-hour recovery-point/data-loss target**, **120-minute
recovery-time target**, and **56 retained snapshots**. These are targets to be
validated, not measured guarantees. Automatic backups are opt-in and run only
while the API is running; the scheduler retries failures hourly. API downtime,
capture/transfer duration and failed jobs can exceed the target. Status measures
age from the last successfully uploaded and checked bundle's capture start.
An unknown or overdue status requires operator attention. Use an independently
supervised API for unattended scheduling; the old local-backup launchd job below
does not run the new encrypted workflow.

Each bundle includes project `data/`, `optimize/`, `models/`, `jobs/`, `runs/`,
and `evidence/` state, plus dependency locks. SQLite uses its online backup API;
Redis uses a full RDB snapshot, including all databases and Nautilus key prefixes.
Campaign JSON and SQLite holdout-consumption attributes are both included.
Per-file SHA-256, SQLite integrity and Redis RDB integrity are verified. Restic
encrypts and authenticates the upload and checks all repository data before
retention removes any Quant-tagged snapshots, then checks again after pruning.
Other repository tags are excluded from retention. A dedicated repository avoids
coupling this full check to unrelated backups.

This is **not a cross-store transaction**: the manifest records the capture
interval. Symlinks and files changing during copying fail the job. External
datasets/model paths, source checkout, virtualenvs, host configuration and secrets
need separate recovery; establish their inventory before the clean-host drill.
Stop research writers for a campaign-consistent checkpoint. Capturing a running
paper node does not replace broker reconciliation. Restoring an older snapshot
can omit a later kill or holdout consumption inside the data-loss window: never
resume execution or tuning solely because the older snapshot verifies.

**Restore procedure and evidence:**

1. Prepare a clean host with the reviewed source checkout, recorded dependency
   locks, Restic, Redis tools, repository password and SSH trust. Keep trading,
   optimization, news writers and the original host disabled during migration.
2. Open its local dashboard, configure the existing repository, run **Check remote
   integrity / refresh snapshots**, select an exact snapshot, and type the shown
   **RESTORE ISOLATED** confirmation. Restore downloads to a new private directory
   below `jobs/recovery/drills/`; it never overwrites application state or loads
   the RDB into a running Redis service. Cancellation can leave a partial drill
   directory, which is not a valid restore until all checks pass.
3. Review manifest coverage, file hashes, SQLite/RDB checks and capture dates.
   Start the restored RDB only in an isolated Redis instance and verify permanent
   kill/operator freeze, allocation/baseline and risk peak, audit history, and
   every consumed/pending/failed holdout. Check all state written after capture
   against independent audit and campaign evidence. Unknown consumption means no
   further tuning; unknown risk state means no execution.
4. Record actual clean-host identity/isolation and elapsed time through recovery,
   not just download time. The dashboard reports download/integrity duration
   separately; a different hostname is not proof of a clean host or full RTO.
5. Reconcile fresh broker positions, working orders, executions and account state
   with the restored ledger; preserve existing permanent kills and operator
   freezes. Review outstanding commands before any use of restored state. Run
   the existing doctor and supported-version paper recovery campaign before
   resuming. Dashboard restoration never activates a node, approves a readiness
   gate, or releases a freeze. Final state installation/resumption is an operator
   recovery procedure, outside the isolated restore action.

**Still required for full P1-07 completion:** provision and test the actual
off-host destination, verify retention there, and record a reviewed clean-host
restore with state preservation, measured end-to-end recovery/data-loss targets
and fresh broker reconciliation. The user deferred that external part on
2026-09-13. Local encrypted fixture drills do not close it.

### Existing local snapshots

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
## Settled-cash evidence

Paper/Live configuration displays **Settled cash (IBKR)** from the configured
monitor account in USD. The broker telemetry panel separately displays
**Settled cash (execution IBKR)** from the active node's own connection. Neither
reading substitutes available funds, total cash or buying power. Existing
connection configuration, session launch, status polling and cancellation also
manage this subscription; no extra broker client or credential is needed.

The source is IBKR's [`accountSummary` `SettledCash` field](https://interactivebrokers.github.io/tws-api/interfaceIBApi_1_1EWrapper.html).
The three-minute summary cadence has a 240-second evidence expiry. Missing,
invalid, future-dated, stale and disconnected observations never display a
current amount. Reconnects and process restarts require new callbacks, even if
Redis retains an older account cache. USD zero and negative balances are valid
observations. This is account evidence, not a claim that buying is permitted.

## Corporate-action recovery

In a paper equity session, open **Safety controls → Corporate actions & instrument
identity**. The table shows broker conIds, qualified symbols and retained aliases.
Record the broker/issuer event ID, timezone-qualified effective instant, source
reference and split ratio, dividend per share, or replacement symbol/conId.
Recording freezes entries on the next strategy control check. Pending events may
be cancelled from the recovery journal; cancellation does not release a freeze.

Use the safety controls to flatten, verify broker completion, and stop the node.
After the effective instant, restart with every affected instrument included.
Previously registered aliases resolve through the conId registry. Recovery
requires zero account positions and zero working/inflight orders, including
foreign/manual exposure visible to reconciliation. It refetches history and
rebuilds model, order and protection state, preserving allocation, risk peaks
and permanent kill state. Interrupted recovery repeats these checks and warmup.
The journal advances from PENDING to REBUILDING to APPLIED; guarded resume is
available only after warmup and the other safety checks pass.

The event journal is maintained by the operator. It does not discover events,
adjust an open book or credit synthetic dividend cash. Cash and quantities remain
broker-authoritative. Validate this workflow against supported TWS/Gateway
before relying on it for an unattended session.


## Reviewed sector exposure and broker disconnects

Paper/Live configuration accepts a separate **Reviewed sector evidence (JSON)**
upload, or evidence embedded in the strategy params file. The form includes the
required format. Supply every selected equity/ETF's qualified broker conId and
symbol, full sector weights summing to one, an HTTPS source reference, source
date, reviewer and review timestamp, and expiry within 31 days of the source
date. These are operator-reviewed declarations; validation does not independently
verify the referenced source. Research sector maps cannot authorize execution.

Set **Gross sector limit (%)** in the risk settings. This explicit setting takes
precedence over imported optimizer risk settings. The node checks broker identity,
evidence expiry and gross exposure from positions and outstanding entries, with
ETF exposure distributed by the reviewed weights. Proposed orders use at least
the current market mark. Missing classifications or stale marks block entries;
a sector breach initiates the existing cancel-and-exit workflow. Protective exits
remain available. The independent supervisor also checks reported sector amounts
against the configured equity limit.

Review **Execution adapter**, **Sector exposure**, and the expandable sector
evidence in Live telemetry. Reports include source/reviewer dates, weights,
gross exposure, limit breaches and an evidence hash. Telemetry older than 20
seconds is not displayed as current authority. A broker disconnect immediately
invalidates reconciliation, clears pending signals and blocks entries; socket
recovery alone does not re-enable trading. Stop the session and restart for fresh
reconciliation. To replace expired or incorrect evidence, stop the session,
upload replacement evidence and restart. Existing status, logs, cancellation and
safety controls cover this workflow. Live capital remains disabled by readiness.

## Research data preflight and observed-volume repair

Backtest, Optuna and seed-campaign configuration show a data preflight before
launch. Each ticker lists observed dates, volume coverage and declared source
semantics. "Unknown" means the CSV does not supply that evidence. Observed
spacing is not necessarily the requested bar width (RTH bars can be shortened
at session boundaries). Exact missing-bar coverage requires calendar evidence.

If equity data has no positive volume, import a CSV with observed traded-share
volume or open **Repair with observed IBKR trade bars**. Configure the socket
host/port (paper TWS 7497 or paper Gateway 4002), a distinct client ID, years,
bar width and session, then choose **Fetch observed replacement**. Repair
replaces the selected file/universe only after validation, retaining the old
bytes in `<file>.<sha256>.bak`. Status, cancellation, broker output, backup
location and the resulting preflight are available beside the repair control
and in the active research job view. No orders are sent by data repair.

IBKR equity LAST requests TRADES (split-adjusted prices, not dividend-adjusted;
traded-share volume). MID requests MIDPOINT, whose volume is unavailable; use
it for price diagnostics only. Source reference:
[IBKR historical data types](https://interactivebrokers.github.io/tws-api/historical_bars.html).
Partial zero-volume coverage remains visible and provides no liquidity on those
bars. The preflight does not certify fill calibration or approve live trading.

Read-only CLI diagnostics from the directory containing `quant/`:

```bash
quant/.quant312/bin/python -m quant.data.research_preflight \
  --csv quant/data/equity_bars.csv --asset-class equity --tickers QQQ
```

The same command with `--repair --port 4002 --client-id 71 --bar-hours 4`
fetches observed replacement data. Existing in-sample/out-of-sample snapshots
and consumed campaign/holdout contracts are intentionally not regenerated.


## Dataset provenance and universe review (P2-01)

Research configuration's **Dataset provenance & universe** disclosure reviews
all symbols in the input, including those outside the selected trading universe.
The Data CSV picker accepts the normal OHLCV columns plus optional declarations:

- `source`, `retrieved_at` (ISO UTC), `session`, `price_basis`, `volume_basis`;
- `con_id`, `symbol_alias` (supplier-declared lineage, never inferred from price);
- `membership_start`, `membership_end`, `membership_source` (dated evidence).

Leave unknown values empty. Repeated declarations can describe separate
membership intervals or symbol identities; each distinct supplied record is
retained. Observed first/last bars are displayed separately from membership
periods. A current constituent list does not establish a historical universe.
Membership declarations are for review, not dynamic universe selection. Include
removed/delisted instruments in the supplied data when the vendor supports them.
The IBKR fetch already records retrieval time and source/session/adjustment/volume
semantics; undeclared historical conIds and membership remain unknown.

Import, IBKR replacement/merge, and research launch retain exact CSV bytes under
`<source.csv>.provenance/<sha256>.csv`, with an integrity-checked JSON manifest.
`observations.json` records source-version observations separately, so returning
to an earlier version is visible and replaying an old snapshot does not rewind
history. Files are published atomically under a process lock. Existing `.bak`
originals from IBKR repairs remain retained. A read-only preflight does not write
an archive; it shows changes relative to the latest retained source observation.
Revision counts compare UTC ticker/bar identities, values and semantics, excluding
retrieval-time-only changes. Duplicate/invalid timestamp data has unknown revision
counts and remains subject to the execution preflight.

Backtests and optimizations read the retained file throughout computation and
reporting. Campaigns pin it before the first seed and reuse it on retry. New
validation contracts lock both CSV and manifest hashes; robustness and promotion
verify them before evaluation. Existing legacy contracts retain their CSV-hash
check, without claiming new provenance. Existing optimizer studies created before
this contract extension cannot silently resume under the changed contract; start
a new study instead. Never reset or reuse a consumed holdout.

Review source history in preflight, **Dataset & universe** under saved-run
Evidence, or campaign evidence. **Download provenance evidence** exports the
review record as JSON. Saved reports reflect evidence captured for that run,
not later source revisions. Existing launch/progress/log/cancel controls apply;
archiving is part of the admitted research or data-import operation.

Do not edit retained CSVs or manifests. Missing/corrupted evidence blocks research
or validation: restore the matching archive from backup. Archives inside the
project's data/jobs directories are covered by the existing recovery bundle;
external input directories need separate backup. Versions are not automatically
pruned because studies may reference them. Disk use grows with distinct input
versions. The UI displays up to 50 prior source observations; older observations
remain in the journal. None of this proves vendor point-in-time availability or
removes survivorship bias, and unknown historical coverage is displayed explicitly.
