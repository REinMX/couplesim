# Repository review and known limitations

> **Status: experimental research prototype — not production-ready.**
>
> The primary two-way Flow workflow executes and converges, but the repository
> has known scientific, standalone-workflow, documentation, and publication
> limitations. This document records them so the initial GitHub publication is
> transparent.

## Scope reviewed

The review covered commit `3016b46` and its changes relative to `38766bd`, with
particular attention to:

- fixed-point coupling and convergence;
- restart/state handling;
- Flow/Eclipse adapters and ERT integration;
- rate, pressure, unit, and report-period semantics;
- standalone model configurations;
- tests, lint, repository hygiene, safety, and documentation.

A separate one-way Model B -> GSATPROD -> Model A profile-bridge/S2RC
experiment exists outside this Git repository. It is deliberately not included
here and must not be confused with this repository's primary two-way
fixed-point architecture.

## Executed verification

The following checks were run on the reviewed tree:

| Check | Result |
|---|---|
| Tests from files tracked at `HEAD` | 93 passed, 1 expected skip |
| Full working-directory discovery | 101 passed, 1 expected skip; includes an untracked test module |
| Documented scoped Ruff command | Passed |
| Ruff over every tracked Python file | Failed: three unused imports |
| `git diff --check HEAD^ HEAD` | Passed |
| `ert lint 02_ensemble_coupled.ert` | Passed |
| Real `ert test_run 02_ensemble_coupled.ert` | Passed |
| Realization-0 convergence | 12 iterations, residual `0.004466082`, tolerance `0.005` |
| Realization-0 simulator health | 84/84 PRT files had final `Warnings 0 / Errors 0 / Problems 0` |
| `ert test_run master_network.ert` | Failed with the default Flow-master configuration |
| Standalone Model A multiplier probe | `0.8` and `1.2` produced identical decks and rate CSVs |

The expected test skip covers the no-Flow failure path because Flow is installed
on the verification host.

## What is verified

- One ERT realization owns Model A, Model B, and one network-master solve loop.
- The primary path uses real Flow Model A and Model B restart chains.
- Neither reservoir is replaced by a GSATPROD prescribed profile in the active
  two-way configuration.
- Network pressure constraints are returned to both reservoir slaves.
- The coupled driver fails on non-convergence.
- The fixed-point residual is evaluated against the unrelaxed target.
- Coupled-mode productivity multipliers reach rendered Flow decks and affect
  rates.
- Exchange CSV validation rejects missing and non-finite values.
- Repository source trees are protected from runpath deletion.
- Flow acceptance checks use the final authoritative PRT `Error summary`.

## Blocking scientific and functional findings

### 1. Total liquid is rendered as an oil-rate target

The slave exchange reports:

```text
q_liq = WOPR + WWPR
```

The Flow network master renders this value into `WCONPROD ORAT`. `ORAT` is an
oil-rate target, not a total-liquid target. This is nearly invisible in the toy
case because water rates are small, but it becomes incorrect at material water
cut and can overstate network loading.

Required resolution:

- use a consistent total-liquid control such as `LRAT`; or
- exchange and apply phase rates explicitly;
- add a high-water-cut integration test that would fail under the current
  mapping.

Relevant files:

- `spikes/003-opm-model-n-restart/opm_model_n_restart_adapter.py`
- `spikes/004-opm-flow-master/MASTER_FLOW_TWOWAY.DATA.tmpl`

### 2. Standalone productivity uncertainty does not reach real simulators

`run_standalone.py` reads the per-model multiplier and changes a staged
`simspec.json`, but the real Flow adapter does not consume that staged file and
the standalone invocation does not pass `--productivity-multiplier`.

An executed probe with multipliers `0.8` and `1.2` produced:

- identical `PERMX = 100` rendered decks;
- byte-identical rate CSVs;
- SHA-256
  `5046f1419a3a57068504b50957672924ad5daad2b4ab3adabbfd20d7138940c0`
  for both rate files.

The Eclipse standalone path has the same conceptual gap because its adapter
also does not consume the staged multiplier.

Relevant file:

- `ert/bin/scripts/run_standalone.py`

### 3. The standalone network-master ERT configuration does not run by default

`master_network.ert` uses the default `coupling.json`, whose master backend is
Flow. `run_standalone.py` supports only the dummy master in standalone mode and
therefore exits with:

```text
ValueError: standalone master supports the dummy network solver only;
the Flow master needs the full coupled 02_ensemble_coupled.ert
```

The configuration must either select the dummy smoke config explicitly,
implement standalone Flow-master behavior, or be removed/renamed as
non-runnable.

## Important accuracy and reproducibility findings

### 4. The algorithm is full-horizon fixed point, not sequential report-step acceptance

The documentation currently says that the driver repeats the current report
step, accepts a converged restart, and advances to the next period. The code
instead reruns the complete 2024-2026 restart chain during every outer
iteration and updates the full vector of annual rates and constraints.

That is a full-horizon fixed-point/waveform-relaxation method. It may be a valid
engineering choice, but it must not be described as report-step-by-report-step
restart acceptance unless the implementation is changed accordingly.

Affected documentation includes the root README, the Spanish implementation
guide, and the case name `02_coupled_stepwise_verified`.

### 5. The documented 101-test result is not reproducible from tracked HEAD

A clean tree containing only tracked files runs 93 tests. The 101-test result
comes from an untracked test module in the local working directory. Publication
claims must report the tracked-tree result or commit the intended tests and
their implementation as a separately reviewed change.

### 6. The scoped lint gate omits tracked lint failures

The documented scoped Ruff command passes, but Ruff over all tracked Python
files reports unused imports in:

- `ert/bin/scripts/run_standalone.py` (`json`, `sys`);
- `spikes/001-opm-model-n/opm_model_n_adapter.py` (`sys`).

The quality gate should lint all tracked Python or explicitly document and
justify exclusions.

### 7. Adapter health checks are recorded but not enforced

Restart adapters calculate checks including cumulative continuity, BHP versus
constraint agreement, and positive rates. These are written into
`restart_report.json`, but the adapter and coupled driver do not fail when they
are false.

Required checks should become acceptance gates. Diagnostic-only checks should
be labelled as such.

### 8. Configurable schedules are not fully supported by real adapters

The coupling JSON exposes schedule start and duration, while the real adapter
templates and restart chain are effectively fixed to 2024-2026. Unsupported
horizons should fail early, or the adapters should become genuinely
schedule-driven.

### 9. Existing runpaths can contain stale artifacts

ERT verification warns when reusing existing runpaths. Simulator wrappers do
not prove that every required artifact was created by the current invocation.
Final scientific acceptance should use a fresh case namespace or verify output
freshness and hashes.

## Publication and operational findings

- The local working tree contains unrelated untracked control/gas-lift/UI work.
  It must not be included through a broad `git add .` without a separate review.
- Top-level `logs/` is not ignored.
- The repository has no root license.
- The repository has no CI workflow or Python tool/dependency manifest.
- README commands contain host-specific executable and environment paths.
- Simulator subprocesses do not define timeouts.
- The full 100-member ensemble was not rerun as part of this review.

No credentials were identified in reviewed tracked source. Subprocesses use
argument arrays rather than `shell=True`.

## Publication contract

Until the blocking findings are resolved, this repository should be described
as:

> A validated experimental Flow/ERT coupling prototype demonstrating
> realization-paired two-way fixed-point reservoir/network exchange on small
> synthetic models. It is not a field-ready, volume/phase-complete, or native
> Eclipse reservoir-coupling implementation.

It must not be represented as:

- production-ready field coupling;
- native MPI/Eclipse RC;
- sequential report-step restart acceptance;
- a validated standalone Flow/Eclipse uncertainty ensemble.

## Recommended remediation order

1. Correct liquid/oil phase semantics and add a high-water-cut test.
2. Propagate standalone multipliers into every active real backend.
3. Repair or remove the default standalone network-master ERT configuration.
4. Align implementation and documentation on full-horizon versus stepwise
   coupling.
5. Make adapter acceptance checks fail closed where scientifically required.
6. Align schedule configuration with adapter capabilities.
7. make the tracked test and lint gates reproducible from a clean clone.
8. Add repository license, portable setup, CI, and stricter ignore rules.
9. Run final acceptance in a fresh namespace: tracked tests, complete lint,
   ERT lint, selected realizations, strict all-PRT health, and exact expected
   realization counts.
