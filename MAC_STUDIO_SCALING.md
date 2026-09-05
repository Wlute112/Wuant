# Mac Studio scaling review

Work is on the repository's existing `Macci` branch. Review and measurements:
2026-09-05. The current host reports Apple M4, 10 logical CPUs, four performance
cores and 32 GiB RAM. Apple's [Mac Studio specifications](https://www.apple.com/mac-studio/specs/)
list the requested base M5 Ultra with 30 CPU cores and 96 GB unified memory.
The Studio is not available here: its throughput has not been measured.

## Findings and decisions

| Area | Evidence / constraint | Decision |
| --- | --- | --- |
| Optuna search | Every trial ran all folds and both cost assumptions sequentially. Five folds require ten independent full-universe backtests. | Run those backtests in persistent spawned processes; keep suggestions, scoring and pruning in their original order. |
| Stability campaigns | Seed studies share SQLite, one validation contract and a manifest. | Keep seeds sequential; each seed uses parallel fold/cost workers. Hardware flags are outside the campaign's invariant optimizer arguments so a host migration can change worker counts. |
| Robustness | Alternative boundaries, training contexts, embargoes, individual tickers and regime episodes repeatedly invoke the same backtests. | Reuse the same process pool throughout the suite. Each scenario parallelizes its folds and costs. |
| Huber feature construction | Baseline profile spends time in Python loops concatenating every historical feature row at each refit. | Vectorize lag windows and target sums with NumPy. Preserve reduction order and causal indexing; do not change fitting frequency, loss, features or training windows. |
| Cross-asset lag construction | Repeated pandas Series/shift/fill allocations for every peer and lag, including inference. | Replace shifts with NumPy slicing while preserving missing-value handling and peer alignment. |
| Regime/HMM | A 300-bar, two-symbol profile called `compute` 1,208 times, though most requests repeated the same history. | Skip rolling-state and HMM input rebuilding when no bars were added. Own the cached input to detect later caller edits. |
| Native numerical libraries | Current scikit-learn OpenMP runtime defaults to ten threads. Multiple such workers could oversubscribe the CPU. | One native thread per research worker; set Accelerate/OpenMP/BLAS environment before spawn, and apply `threadpoolctl` to loaded supported runtimes. |
| CSV / bar conversion | Existing main datasets are about 160–524 KiB; engine setup was a small portion of the first profile. | Retain per-engine loading. A shared-memory, Arrow or cross-trial bar-cache layer is not justified by these measurements. |
| Dashboard log polling | `JobManager.logs` read the entire log to return its last 200 lines. | Seek backward in 8 KiB blocks and read only the requested tail. |
| Dashboard run list | Every eight-second refresh parsed every full run artifact; existing artifacts reach about 3 MiB. | Cache at most 512 small summaries, invalidated by file identity/size/change timestamps. Full chart payloads are not retained in this cache. |
| Frontend charts | Memoized SVG traces, bounded live telemetry and polling at seconds rather than frame rate. | Retain the current rendering approach. No evidence supports a GPU/canvas rewrite for the present data volume. |
| Broker execution / risk | Event order, portfolio-wide allocation, reconciliation and broker acknowledgements are coupled. | Keep each TradingNode's strategy and risk state serial. Parallel workers own independent backtest engines, never individual symbols in one portfolio. |
| IBKR history / monitor | Remote requests, timeouts, permissions and subscriptions govern data arrival. | Retain broker concurrency and subscription bounds. CPU upgrades do not establish that higher broker request concurrency is acceptable. |
| News | RSS already uses up to eight I/O threads. Local enrichment uses one durable queue consumer and an Ollama model (`lfm2:24b`). | Retain model and queue behavior. Additional unified memory helps coexistence, but model upgrades or extra inference requests need backlog/latency evidence and would change research inputs. |
| Operations / persistence | Redis state, SQLite audit writes, fsynced telemetry, supervision, backups and readiness gates preserve execution state. | Retain durability and supervision. More RAM is not a reason to drop fsync or raise trading risk limits. |

The review covers the application subsystems in `models`, `strategies`, `run`,
`optimize`, `data`, `api`, `news`, `ops`, `scripts`, their tests, the dashboard
and launcher. Installed environments, generated data/artifacts and vendored
assistant-skill copies are not application performance targets. This is a
compute/performance review, not a new production-readiness certification.

## Measurements on the current M4

| Measurement | Serial / previous | Improved | Result |
| --- | --- | --- | --- |
| Full backtest: 600 synthetic bars each for BTC/ETH, three peer lags, two spread lags, seed 42 | Original feature functions: 3.6096 s, 3.5913 s | New feature functions: 2.5076 s, 2.5104 s | About 30% less wall time; score, turnover and all 324 counted fills identical. |
| Ten independent development fold/cost backtests: 600 synthetic bars each for BTC/ETH/SOL, five folds | Warm serial: 12.457 s, 12.672 s | Warm three-worker pool: 5.428 s, 5.422 s | About 2.3x throughput with exactly equal fold metrics. |
| Same fold workload, first iteration | Serial: 12.434 s | Three workers: 7.247 s | Spawn/import startup matters for short jobs. |
| Worker memory on that small fold fixture | — | Approximately 0.36 GiB peak RSS per process | A sample, not an upper bound for longer histories or larger universes. |

These are local timings, not promised Studio speedups. The feature comparison
restored the original three hot functions from baseline commit `2e4d636`
into the same runtime, alternating original/new runs with one native thread.
The parallel benchmark includes process startup in its first measurement and
reuses workers for later measurements. It evaluates development folds only,
writes no Optuna studies, and compares every returned metric across repeats
and worker counts. It reads/splits the supplied CSV but never runs a backtest
on the reserved newest 20%.

## Worker sizing

`--workers 0` is the new CLI default, also used by dashboard optimization jobs
when no override is supplied. `--workers 1` runs serially.

Automatic sizing takes the smallest of:

- Available non-efficiency CPU cores, reserving one core on a small host or
  two on hosts with at least eight such cores.
- Research memory budget divided by estimated peak worker memory.
- Available fold/cost tasks (normally `2 * walk_forward_folds`).

The default RAM budget is total memory minus the greater of 8 GiB and 25%.
The default worker estimate is a conservative 4 GiB. On the M4 this selects
three workers and a 24 GiB budget. For the specified 96 GB / 30-core Studio,
the expected default is ten workers for five folds, a 72 GiB budget and a
24 GiB reserve for macOS, TWS, Redis, the dashboard and local inference. An
eight-fold study has sixteen tasks; a ten-fold study is capped at eighteen
workers by the default memory estimate. Do not change validation fold counts
merely to occupy more cores: folds are part of the statistical contract.

The budget is a **per-process-pool admission estimate**, not an OS-enforced
memory limit or a machine-wide scheduler. Run one large research job at a
time, or explicitly divide CPU/RAM budgets between simultaneous jobs. For
large Ollama models or other applications, reduce `--memory-budget-gb`.
Unknown host RAM falls back to one worker unless a budget is supplied.

## Run on either Mac

From the repository root, inspect the actual host without starting services:

```bash
./quant compute
./quant benchmark --workers 1 0 --out /tmp/quant-compute.json
```

On the new Studio, compare several worker counts using representative data
(CSV paths below are resolved from the directory containing `quant/`):

```bash
./quant benchmark --workers 1 4 6 8 10 --repeats 3 \
  --csv quant/data/ibkr_bars.csv --tickers BTC ETH SOL XRP DOGE \
  --out /tmp/quant-studio-compute.json
```

Choose the smallest worker count close to the best warm throughput. Increase
`--worker-memory-gb` if observed worker RSS approaches the estimate; allow
headroom for growth over a long study. Watch system memory pressure and swap
with the intended news model and TWS running. The benchmark records process
high-water RSS, not total system/Metal memory or an enforced memory ceiling.

Normal optimizer and campaign commands automatically pick up the host-sized
pool. Explicit controls work on `optimize`, `multi_seed` and `robustness`:

```bash
# From the directory containing quant/; use a new study name for a new study.
quant/.quant312/bin/python -m quant.optimize.optimize \
  --csv quant/data/ibkr_bars.csv --tickers BTC ETH SOL XRP DOGE \
  --trials 100 --seed 42 --workers 0 --refit-every-n-bars 1 \
  --defer-final-test --out-params /tmp/quant-candidate.json
```

For dashboard jobs, defaults can be set before launching the API:

```bash
QUANT_COMPUTE_WORKERS=8 QUANT_COMPUTE_MEMORY_GB=48 ./quant
```

The API also accepts `workers`, `memory_budget_gb` and `worker_memory_gb` on
`POST /api/jobs/optimize`; unset values preserve CLI/environment defaults.
Actual resolved settings are printed in the job log and persisted as
`compute_plan` metadata, outside the model and validation parameters.

## Parallelism tradeoffs and verification

Optuna still completes one trial before suggesting the next. Only independent
backtests run concurrently. This preserves exact trial caps and goal-mode
stopping, chronological intermediate reports and sampler/pruner ordering.
Workers return metrics to one coordinator; they do not write to Optuna or
share engines, fee histories, model histories or portfolio risk state. The
outer holdout and promotion gate remain in their existing serial workflow.

Speculative later folds may already be running when a trial is pruned. Queued
work is cancelled; running work is drained before the next trial. This spends
more CPU than serial early pruning, so heavily pruned or very short workloads
can favor fewer workers. There is no nested trial/seed/native-thread pool.
This approach avoids the concurrent SQLite writers and asynchronous search
ordering discussed in the [Optuna FAQ](https://optuna.readthedocs.io/en/stable/faq.html).

Ctrl-C/SIGTERM exits the research pool and reaps its workers. The pinned
CPython 3.12 executor lacks a public immediate worker-termination method;
that compatibility access is isolated in `ComputePool._terminate_workers`.
Recheck it when upgrading Python. Dashboard cancellation already signals the
research process group, with a timed force-kill fallback.

Regression coverage includes exact old-loop/new-array feature equivalence,
regime-cache invalidation, real serial/spawned Optuna suggestion/pruning/fold
equivalence, worker cleanup and cancellation, invalid memory budgets, API
forwarding, log tails and artifact-cache refresh. Existing trading, risk,
news, reconciliation and readiness tests were also run. The repository's
Nautilus/pandas timestamp-deprecation warning remains upstream.

A six-iteration check (60 independent backtests) kept peak worker RSS between
0.354 and 0.362 GiB on the same small fixture. This is not a long-study memory
bound. Numerical equality has been tested on the M4; the included benchmark
should be run on the Studio's numerical-library stack before relying on its
worker configuration for a validation campaign.

For migration, use an arm64 CPython 3.12 environment rebuilt from
`requirements.lock`, rather than copying the old virtualenv's absolute
interpreter links. Retain the existing Redis/SQLite backup-and-restore and
paper preflight procedures in [OPERATIONS.md](OPERATIONS.md). Research
parallelism does not require new broker sessions or a PostgreSQL deployment.
