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
upload alongside a saved Optuna result selection. The form includes the
required format. Low-level runner replay can also carry embedded evidence. Supply every selected equity/ETF's qualified broker conId and
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


## Drift and execution-quality review (P2-02)

Open the workspace menu → **Drift review**, then unlock it with the existing
`QUANT_CONTROL_TOKEN`. The API accepts review controls only from localhost with
the configured operator token. Credentials stay in component memory and are
cleared on lock, navigation or reload. This workflow does not start or stop trading.

1. Import a newly generated standalone backtest or collect a registered paper
   session. Existing artifacts without `review_contract` must be regenerated;
   restart older paper sessions to publish a model fingerprint. Optimization and
   holdout artifacts are deliberately excluded from this diagnostic import path.
2. Review the source, observation count, dataset/execution provenance and exact
   model fingerprint. Capture a baseline with a reason. The immutable snapshot
   includes its source evidence and SHA-256; later telemetry cannot rewrite it.
3. Select that baseline in **Review settings**, enter a review reason, set the
   sample/window limits, freshness limit and six adverse-change thresholds, then
   save. Saved settings are append-only versions attributed to the operator.
4. Import comparable recorded shadow/research evidence or run a paper session
   with the same resolved alpha, universe and bar/session contract. **Run review
   now** collects running paper jobs and records comparisons. Enabled review
   repeats every 15 seconds while the API is running. **Pause automatic review**
   stops future cycles and retains evidence. It never cancels the trading node.
5. Review results by source and regime. Each metric shows its baseline/current
   mean, sample counts, approximate 95% interval, change, threshold and review
   state. Select a recorded review to inspect an earlier report; a result from
   earlier settings is labeled historical. Alert history records state transitions,
   including insufficient evidence, stale/incompatible sources, breaches and
   recovery; repeated identical reviews do not create repeated alerts.

The fingerprint hashes resolved alpha/strategy parameters, source implementation,
symbol universe, asset class, bar basis and extended-hours setting. Runtime
accounts, broker venue IDs and file paths are excluded. A parameter or code
change requires new evidence and an explicitly reviewed baseline; the review
service cannot retune, approve or promote a model. Fingerprints identify the
code/parameter contract, not a frozen Huber fit: normal past-only refits continue.

Research imports use OOS forward-log-return observations retained in the saved
backtest. Paper imports use completed-bar predictions and realized targets only
after the configured horizon. The comparison thins overlapping target intervals
per ticker. Against an earlier snapshot of the same paper source, only new IDs
at or after the baseline's final target instant are eligible. Other same-stage
sources with overlapping baseline dates are rejected. Comparisons across stages
may use the same dates; they remain separate sources and are never pooled.

Slippage is signed adverse basis points versus the last completed decision close,
averaged per terminal entry order; it is **not** an NBBO/arrival-price execution
study and includes market movement between decision and fill. Buy fills above
the reference and sell fills below it are adverse. Actual average fills include
the execution ledger's corrections; order IDs prevent repeated polls from
inflating counts. Pending orders do not enter rejection rates. Missing prices,
reference bars or execution rows stay unavailable. Peer-input age measures the
oldest required peer bar at the decision instant; no peer features means zero,
and a missing required peer means unknown. It does not measure every news/model
feature's freshness. Research artifacts currently lack comparable per-order
execution/freshness observations, so those comparisons need a declared recorded
paper/shadow baseline instead of assumed zeros.

Intervals are descriptive normal standard-error intervals, not calibrated
significance tests. They assume independent observations and do not correct for
serial/cross-symbol dependence or repeated comparisons. Minimum sample counts
gate alerts but do not establish reliability. Source freshness is based on its
snapshot time; market/peer age remains a separate measurement. Stopped or stale
paper/shadow evidence must not be interpreted as an active healthy monitor.

Use **Export [source]** to download the exact normalized schema. **Import recorded
evidence JSON** accepts the same envelope for a recorded shadow run, with a new
`source_id`, the exact `contract`, a dated `reference`, an `as_of` timestamp and
`samples`. Each sample has an ID, timezone-aware decision `ts`, `ticker`, `regime`
and any available `prediction`, `actual`, `target_ts`, `slippage_bps`, `rejected`
(numeric 0 or 1) and `feature_age_seconds`. Prediction/actual are log returns;
an actual requires a later `target_ts` no later than `as_of`. Omit unknown values.
Duplicate IDs/forecast decisions, invalid/future timestamps, nonfinite values,
out-of-universe symbols and modified model hashes are rejected. Recorded imports
are immutable; provenance remains supplied evidence, not independent certification.

State is in `jobs/drift.sqlite3` with WAL and full synchronization. Automatic
collection retains at most 10,000 observations per source, so missed telemetry
windows cannot reconstruct an entire broker archive. Baselines/settings/reports/
alerts are append-only and grow over time; include the database in normal backups
and monitor disk use. The dashboard lists the newest 100 baselines/alerts and
newest 20 reports (the database retains older records). It
shows missing comparison stages explicitly. API downtime stops collection and
review; an overdue review is announced. No P0 gate is approved by these reports.

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


## Strategy modularity audit — 2026-11-08

The system has reusable research and execution infrastructure, but currently
constructs one concrete Huber strategy. Adding a second strategy still requires
changes outside `strategies/`; this is not yet a strategy plugin platform.

| Ownership | Components to retain across strategy changes | Boundary to preserve |
|---|---|---|
| Market data | `data/ibkr_fetch.py`, provenance archives, research preflight, exchange calendars and corporate actions | Versioned bars and universe/session declarations; no strategy-specific feature calculation in ingestion. |
| Research orchestration | `api/jobs.py`, job worker, progress, cancellation, logs and compute pool | Jobs invoke an identified strategy contract; lifecycle and worker limits remain shared. |
| Optimization method | Optuna TPE studies, storage, seeds, pruning and resume lifecycle in `optimize/optimize.py` | Shared search engine calls a strategy-owned suggestion function. Parameter names, bounds and conditional dimensions belong to the strategy. |
| Validation | Purged walk-forward folds, embargo, outer holdout, normal/stressed costs, multi-seed comparison and promotion | Common evaluation protocol; strategy declares required history and prediction horizon. Compare only compatible validation contracts. |
| Backtest execution | Venue/instrument construction, fee models, equity simulation, event replay and liquidation | Shared engine accepts a strategy factory instead of constructing `MLStrategy` directly. |
| Risk and broker operations | `strategies/risk.py`, sector checks, short controls, sessions, reconciliation, supervisor and live-readiness gates | Account and portfolio limits apply independently of alpha. Optuna must not optimize away these limits. |
| Results and dashboard | Artifact storage, run library, benchmark-relative performance, drawdown, trades, optimization history, progress and logs | A common result schema feeds common panels; optional diagnostics depend on strategy capabilities. |
| Strategy implementation | Huber prediction, lag/regime/news/industry features, entries, exits and parameter search space | Replace with the selected strategy; do not copy these assumptions into API schemas or the dashboard. |

### Findings and disposition

- **P1 — Sampled parameters could be silently replaced. Fixed.** The optimizer
  applied structural overrides after `trial.suggest_*`, allowing a manually
  pinned lag to differ from the trial's recorded parameters. The optimizer now
  rejects collisions with the strategy's tuned keys, including CLI structural
  JSON. The existing fifteen search dimensions and distributions are preserved.
- **P1 — Client-owned parameter payloads obscured provenance. Fixed for the
  dashboard/job API.** Manual feature settings, lag-mode switches and strategy
  JSON upload are removed. The API rejects these fields rather than silently
  ignoring stale clients. Replays select `source_run_id`, load the winner on the
  server, check strategy/profile/universe/cadence/session compatibility, and
  retain its fixed fit settings. Missing, partial and non-qualifying results
  fail with a recovery message. Existing complete Huber artifacts without a
  strategy ID remain compatible; older incomplete artifacts require a new sweep.
- **P1 — Default backtests bypassed optimization. Fixed for the dashboard/job
  API.** With no saved result selected, backtest launch starts the existing
  forty-trial Optuna workflow and its outer-test report. The UI switches to the
  optimization hub with progress, cancellation and result review. A selected
  result replays without a new search. Search budget, validation, capital,
  simulation assumptions and risk limits remain operational controls.
- **P2 — Strategy construction is concrete. Open.** `build_engine` and
  `run_live.build_node` import and instantiate `MLStrategyConfig`/`MLStrategy`.
  Introduce a strategy registry/factory before adding a second strategy, keeping
  backtest/paper/live on the same selected implementation.
- **P2 — Model diagnostics reconstruct this alpha. Open.** `run/artifacts.py`
  creates `PredictionEngine` and regime/model chart data; `ModelDecisionTape`
  and model panels expect Huber forecasts and HMM state. Extract a diagnostics
  provider and capability metadata; retain the common performance/trade panels.
- **P2 — Profile and strategy defaults are mixed. Open.**
  `strategy_defaults_for_asset` combines venue-oriented defaults with alpha
  feature defaults; `optimize/stability.py` names Huber feature-adoption and
  conditional parameters. Move these definitions into a strategy descriptor.
- **P2 — Identity is only an initial boundary. Open.** Optimization artifacts
  now record `huber_mean_reversion_v1`, but campaigns, model registry, backtest
  artifacts and job/study contracts need strategy/schema/code-version identity
  throughout before multiple strategies can coexist safely.

The initial strategy contract is `strategies/parameters.py`: strategy ID, tuned
and fixed parameter keys, Optuna suggestions and collision validation. This
extracts ownership without rewriting the engine or claiming multi-strategy
support. Future descriptor interfaces should supply `suggest_parameters`,
`build_strategy`, history/horizon requirements and optional diagnostics. Keep
Optuna and the dashboard as shared services.

The public restriction applies to dashboard and job API configuration. The
low-level Python replay runners retain parameter-file inputs for internal job
handoff, model-registry replay and developer diagnostics; they are not a
security boundary against someone who can edit local source/artifact files.
No real optimization campaign or broker orders were launched for this change.
Validation uses real Optuna trial generation with controlled fold evaluations,
existing simulator/research regressions, and isolated browser API fixtures.
Follow-up work is tracked only in `PRODUCTION_READINESS.md`.


## Daily paper execution and historical charts — 2026-11-08

Completed daily equity signals in broker mode are scheduled for the next
eligible entry window from IBKR's exchange calendar. The window honors session
policy, opening/closing buffers, holidays, early closes and DST. The existing
one-second supervision loop dispatches eligible signals using a fresh current
market mark, preserving the forecast and ATR from the completed signal bar.
Signals compete by conviction through the existing order/risk path. They expire
at the target window's entry cutoff and are not carried indefinitely.

Pending daily intent is included in the persisted strategy state with a
configuration fingerprint. Changed strategy/account/session contracts invalidate
it; expired or malformed intent is discarded. Explicit operator freeze/resume,
cancellation, emergency exit, corporate-action recovery and stop invalidate
pending intent. Temporary supervisor freeze/recovery retains bounded intent but
cannot execute it until all existing execution and data-health checks pass.
The Live telemetry panel shows scheduled-signal count and next window. Audit
events record scheduling, unavailable schedules, dispatch and expiry.
Backtests retain their existing next-book simulation; no objective, search space
or Huber feature calculation changed. Broker parity still needs a supervised
paper-session test; no readiness gate was approved by this implementation.

Daily dashboard candles request one-time IBKR historical snapshots with
`keepUpToDate=False`, refreshed at most every five minutes while viewed. The
snapshot refresh merges recent bars without discarding older history. IBKR
warning 2188 retains the request until historical callbacks finish; it does not
turn delivered history into an error. Intraday continuous subscriptions fall
back to snapshots if their live permission is denied. Genuine historical-data
permission errors remain visible. Each request has a timeout, callbacks from
replaced requests are ignored, and disconnect/reconnect preserves the cached
chart with explicit status. Snapshots show receipt time and are labeled
historical rather than live; they do not satisfy execution's fresh-quote checks.
The paper form follows a connected paper Gateway's host/port unless the operator
edits that connection in the current form.

Read-only Gateway verification on 2026-11-09 UTC: connection to localhost port
4002 succeeded; QQQ `1 day` historical request returned 251 bars through session
2026-11-06 despite warning 2188, with `status=historical` and no chart error.
No orders or market-data subscriptions were purchased. This verifies historical
access for that instrument, not every exchange or real-time execution access.
Regression evidence includes 277 targeted Python tests, 49 isolated Chromium
browser tests, 26 JavaScript tests and a successful production build. Desktop
and narrow chart screenshots were reviewed. Direct access to the user's browser
was unavailable; broker transport was verified through the read-only API.


## Optimization without a qualifying candidate — 2026-11-08

A completed study can produce no usable candidate. Only completed trials with a
finite objective above the rejection sentinel can become the incumbent; the
stability objective still owns fold eligibility. Penalized negative scores above
the sentinel remain eligible. Pruned, rejected and nonfinite trials cannot meet
a goal target or supply winning parameters.

When no trial qualifies, the optimizer records `NO_QUALIFYING_CANDIDATE` in the
study, progress snapshot and diagnostic run artifact. It leaves `--out-params`
unchanged, does not lock a candidate, and returns before evaluating the outer
holdout. The holdout remains `UNTOUCHED`, and the same study can accept more
trials under its existing immutable data/validation contract. Empty/all-pruned
searches use the same outcome. Completed means the search process ended, not
that a candidate was approved. Artifact/progress errors remain process failures.

The run library labels the diagnostic result "No candidate". The research hub
shows rejected-trial history, explains that the holdout was not evaluated, and
provides the study ID for the existing New sweep → Resume study field. No
selected parameters or fictitious performance metrics are supplied. Diagnostic
runs are excluded from backtest/paper winner selectors. Seed campaigns record
this outcome explicitly rather than marking a rejected seed complete.

Consumed holdouts stay consumed: both the consumption marker and the final-test
flag block resuming such studies. In particular, this fix does not reopen or
rewrite `optimize_480e8fbb02`, whose holdout was already evaluated by the previous
workflow. Existing parameter files and historical run artifacts are not repaired
by inventing a replacement winner.

Regression checks exercise real Optuna study/storage and optimizer entrypoint
lifecycles with controlled objective values. They verify parameter-file byte
preservation, no holdout-engine invocation, persistent study markers, valid JSON
reports, pruned/empty/nonfinite outcomes, resume to a qualifier, qualifying
holdout dispatch and seed-campaign reporting. Browser checks cover outcome and
resume guidance, skipped holdout progress, rejected-result selection and narrow
layouts. No new market-data optimization campaign was launched for this fix.

### Development selection and outer-holdout rejection (2026-11-09)

A completed development trial is a candidate for the one-shot outer test, not
an execution-qualified result. The optimizer now requires executed holdout
trades and positive finite normal/stressed risk-adjusted returns and net PnL.
Rejected holdouts retain their parameters and metrics for diagnosis, carry
`REJECTED_OUTER_HOLDOUT`, and are excluded by the dashboard, job API and paper
runner. Ordinary sweeps publish `best_params.json` only after this gate passes.
Deferred campaign outputs are explicitly `DEVELOPMENT_SELECTED` and cannot
start execution. Legacy results with recorded failed evidence are rejected on
read even when their original writer omitted an outcome status.

The NVDA/AMD/JPM result `optimize_fccfe699fc` is rejected. Its first holdout
market order for JPM was rejected as "no market with no fills": the venue
drained its delayed command after NVDA's next opening book, before JPM's book
was loaded. Synthetic regressions reproduce this instrument-order dependency
in both full and streamed replay. Equity execution model version 2 schedules
closed-book commands for the target instrument's next observed auction and
flushes them after same-time books are loaded, before the next OHLC stage.
Protection commands are activated before that next stage as well. Whole-share
entry quantities are floored so rounding cannot increase the risk-sized order.

Historical results from the faulty replay are not new validation evidence.
The corrected execution version is locked into new optimizer contracts; old
studies cannot silently resume under different execution semantics. The
consumed study stays locked: neither diagnosis nor rejection authorizes more
tuning against its observed holdout. Forensic traces retain the original
metrics; no replacement performance claim was generated.

See [diagnosis and regression evidence](evidence/optimization_holdout_rejection_fix_20261109.json).

### Execution economics audit (2026-11-10)

Normal and stressed runs replay the same frozen candidate. Costs change quotes,
commissions, financing and equity; they do not automatically change prediction
thresholds or rerun parameter selection. Counts can remain equal when limits,
whole-share sizing and risk thresholds do not cross a boundary. The displayed
count is now labelled **Filled orders**: it is neither completed round trips
nor the number of individual partial-fill events. The legacy `total_trades`
field retains its filled-order meaning.

Execution model version 3 sets the daily/session risk baseline before the
synthetic opening's accrual and execution costs, including midnight-stamped
daily bars. Turnover uses executed quantity rather than requested quantity for
partially filled orders. The CLI backtest accepts a dashboard artifact's
`best_params` instead of silently dropping its tuned settings.

Research and execution now default to long-only. Equity research can explicitly
enable shorts through `allow_shorts` in the API or `--allow-shorts` in the CLI;
`--no-allow-shorts` explicitly selects long-only. The policy is recorded as
`allow_short_positions` in saved parameters and validation contracts. The
dashboard selects that policy with a saved result, and execution rejects a
different policy. Saved API results without recorded direction require a new
sweep. Identified optimizer payloads with missing direction also fail in the
CLI loaders. Crypto spot research rejects short entries. Explicit simulated
shorting models scenario borrow costs; it does not establish live borrow
availability or broker short-control equivalence.

The historical 85-order synthetic replay used implicit long/short policy. Its
normal/stressed commissions were $85/$170 and profits $557.67/$468.22. The
corrected explicit long-only replay of the same frozen parameters produced
39/39 filled orders, $39/$78 commissions and $264.55/$221.92 profit. Ten prices
and two fill times changed despite equal aggregate counts. These are synthetic
execution diagnostics, not new market validation or an optimization result.

Native regressions also prove that doubled spread can prevent a marginal limit
fill and higher commission can trigger the daily-loss halt. Independent cash,
fill and marked-inventory accounting reconciles all four native replays within
$0.00000001. The original market study stays consumed and rejected; published
market parameters were not changed. See the [execution audit, checks and
remaining limits](evidence/execution_logic_audit_20261110.json).
