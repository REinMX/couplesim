# Realization-paired two-way reservoir/network coupling with ERT

> [!WARNING]
> This is an experimental research prototype, not a production-ready or
> field-ready coupling implementation. Real Flow/ERT execution is verified, but
> known scientific and standalone-workflow limitations remain. See
> [`docs/REPOSITORY_REVIEW.md`](docs/REPOSITORY_REVIEW.md) before reuse.

This repository demonstrates a licence-free, executable coupling of three real
OPM Flow simulations inside each ERT realization:

- `master_network`: a shared Flow `NETWORK`/VFP model;
- `model_a`: a stateful restart-based Flow reservoir;
- `model_b`: a stateful restart-based Flow reservoir.

The primary path is fully two-way and contains no prescribed production
profile. Model A and Model B send their current simulated rates to the network
master. The master solves the network and returns well-pressure constraints to
both reservoirs. The process repeats until the unrelaxed fixed-point residual
converges.

The decks are small simulator-validated demonstration models, not calibrated
field models.

## Active topology

```text
ERT realization N
  |
  +-- Q0_MULT_MODEL_A[N] -> Model A_N deck permeability/productivity
  +-- Q0_MULT_MODEL_B[N] -> Model B_N deck permeability/productivity
  |
  +-- RUN_COUPLED
      |
      +-- Model A_N Flow restart chain --+
      |                                  |
      +-- Model B_N Flow restart chain --+--> current rates
                                         |
                                         v
                              Flow NETWORK master
                                         |
                          BHP constraints for A and B
                                         |
                              relax and rerun complete
                              configured restart chains
                                         |
                              converge full-horizon rate
                              and pressure vectors
```

The ERT runpath is the isolation boundary. Model A realization `N` and Model B
realization `N` run below the same `realization-N/iter-M` directory. There is no
cross-realization exchange.

## What changed from the former setup

The former primary workflow was sequential and one-way:

```text
Model B full run -> generate GSATPROD -> Model A/NETWORK full run
```

That does not allow network backpressure to alter Model B. It is no longer the
primary configuration.

The active workflow is:

```text
Model A_N rates + Model B_N rates
              -> shared network solve
              -> pressure constraints to A_N and B_N
              -> rerun the configured horizon until converged
```

`GSATPROD` is retained only in
`configs/coupling.legacy-gsatprod.json` for historical regression tests and
comparison. It is absent from `coupling.json`, from the active no-profile
master template, and from rendered primary master decks.

## Coupling algorithm

For every coupling iteration:

1. Run or continue both reservoir restart chains using the current network BHP
   constraints.
2. Read each reservoir's raw Flow rates.
3. Under-relax the rates forwarded to the master:

   ```text
   q_forwarded = q_previous + omega * (q_raw - q_previous)
   ```

4. Render and run the Flow network master with Model A and Model B slot-well
   rates.
5. Extract separate pressure constraints for both reservoir models.
6. Evaluate the unrelaxed fixed-point residual:

   ```text
   residual = max(abs(q_raw - q_previous) /
                  max(abs(q_previous), epsilon))
   ```

7. Continue until `residual <= tolerance`; otherwise fail at
   `max_iterations` so ERT cannot accept a non-converged realization.

Primary settings in `coupling.json`:

- maximum iterations: `20`;
- tolerance: `0.005`;
- relaxation: `0.4`;
- master backend: `flow`;
- Model A backend: `flow`;
- Model B backend: `flow`;
- prescribed profiles: empty.

## ERT ensemble

`ert/model/02_ensemble_coupled.ert` declares exactly 100 realizations and one
forward-model job per realization:

```ert
NUM_REALIZATIONS 100
FORWARD_MODEL RUN_COUPLED
```

Run from `ert/model` with the ERT virtual environment first on `PATH`:

```bash
export PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH

ert lint 02_ensemble_coupled.ert
ert test_run 02_ensemble_coupled.ert --disable-monitoring

ert ensemble_experiment \
  02_ensemble_coupled.ert \
  --realizations 0-2 \
  --current-ensemble twoway_selected_0_2 \
  --disable-monitoring
```

For a sparse subset, ERT reports that `MIN_REALIZATIONS` was reduced to the
number selected. That is expected: the configuration still declares 100, but
only members 0, 1, and 2 are active in the example command.

Aggregate completed members:

```bash
python3 ../../ert/bin/scripts/collect_ensemble.py \
  --case-dir ../../output/02_coupled_stepwise_verified \
  --iter 0
```

The collector writes:

- `output/02_coupled_stepwise_verified/ensemble_results.csv`;
- `output/02_coupled_stepwise_verified/ensemble_summary.csv`.

## Verified selected-realization evidence

The selected ERT experiment for members `0-2` exited with code 0. All three
members contain `OK`, use no prescribed profile, and converged below `0.005`.

| Realization | A multiplier | B multiplier | Iterations | Final residual | A-P1 2024, sm3/d | B-P1 2024, sm3/d |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0.924025 | 0.935934 | 12 | 0.004466082 | 2188.596 | 1293.750 |
| 1 | 0.939670 | 1.088120 | 13 | 0.003276808 | 2195.614 | 1408.235 |
| 2 | 0.928626 | 1.157760 | 13 | 0.003604951 | 2174.041 | 1460.327 |

The sampled Model A values reached the actual Flow decks. For example, rendered
2024 `PERMX` values were `92.402500`, `93.967000`, and `92.862600` mD for
realizations 0, 1, and 2 respectively.

Strict simulator-health verification covered every Flow invocation in the
selected run: 266 PRT files were found, all 266 contained the final
`Error summary`, and zero files reported a nonzero `Warnings`, `Errors`, or
`Problems` tally.

Evidence root:

```text
output/02_coupled_stepwise_verified/
├── ensemble_results.csv
├── ensemble_summary.csv
├── runpath_file
├── realization-0/iter-0/
├── realization-1/iter-0/
└── realization-2/iter-0/
```

Every completed realization includes:

```text
OK
COUPLED_REPORT.txt
coupling_config.json
coupling/
├── convergence_history.csv
├── slave_rates_model_a.csv
├── slave_rates_model_b.csv
├── network_constraints_model_a.csv
├── network_constraints_model_b.csv
├── flow_model_a/iteration-NNN/year-YYYY/...
├── flow_model_b/iteration-NNN/year-YYYY/...
└── flow_master/iteration-NNN/master_report.json
```

The active master report records both reservoirs under `requested_rates`, an
empty `prescribed_profile`, delivered network rates, and separate pressure
constraints for Model A and Model B.

## Important semantic boundary

This implementation is full-horizon fixed-point/waveform-relaxation
co-simulation using Flow restart chains. Every outer iteration reruns the
complete configured horizon; it does not accept one converged report step and
then advance. It is also not in-memory synchronization at each nonlinear
simulator ministep.

Native `standalones2rc`/Flow reservoir coupling remains the stronger reference
for simulator-synchronized exchange. A two-slave native topology was generated
and both spawned slaves reached their simulation loops on this host, but the
Open MPI dynamic-spawn job did not complete. Therefore this repository does not
claim a working native S2RC runtime.

The working fallback also moves the network topology into a dedicated neutral
master. Functionally the same network sends pressure feedback to both
reservoirs, but Model A is no longer literally the native RC master deck.

## Configurations

- `coupling.json`: primary no-profile, all-Flow, two-way configuration.
- `configs/coupling.twoway.json`: explicit no-profile all-Flow smoke fixture.
- `configs/coupling.legacy-gsatprod.json`: obsolete one-way profile fixture,
  retained only for regression/comparison.
- `configs/coupling.fast.json`: fast dummy testing fixture.

## Main implementation files

- `ert/model/02_ensemble_coupled.ert`: 100-member paired ensemble.
- `ert/bin/jobs/RUN_COUPLED`: one complete coupled job per realization.
- `ert/bin/scripts/run_coupled.py`: orchestration, relaxation, convergence, and
  provenance.
- `spikes/003-opm-model-n-restart/opm_model_n_restart_adapter.py`: stateful
  Flow reservoir restart chain.
- `spikes/003-opm-model-n-restart/MODEL_BASE.DATA.tmpl`: initial report-period
  deck with realization-dependent permeability.
- `spikes/003-opm-model-n-restart/MODEL_CONTINUE.DATA.tmpl`: continuation deck.
- `spikes/004-opm-flow-master/opm_flow_master_adapter.py`: network solve and
  pressure-constraint extraction.
- `spikes/004-opm-flow-master/MASTER_FLOW_TWOWAY.DATA.tmpl`: no-profile network
  template.
- `ert/bin/scripts/collect_ensemble.py`: selected-member aggregation.
- `tests/test_twoway_flow_no_gsatprod.py`: primary coupling contract and real
  Flow sensitivity tests.

## Validation commands

Final validation result for this coupling change:

- tracked-tree unit/integration suite: 93 tests passed, one expected skip;
- scoped Ruff: `All checks passed!`;
- `git diff --check`: passed;
- `ert lint`: `Found no errors`;
- selected realizations `0-2`: exit code 0;
- final `ert test_run`: exit code 0;
- strict PRT gate: 266/266 final summaries, all `0/0/0`.

From the repository root:

```bash
python3 -m unittest discover -s tests -v

/home/javier/.cache/uv/archive-v0/0LSKh4TgPfgLZmVD/bin/ruff check \
  ert/bin/backends/eclipse_slave_adapter.py \
  ert/bin/scripts/collect_ensemble.py \
  ert/bin/scripts/run_coupled.py \
  spikes/003-opm-model-n-restart/opm_model_n_restart_adapter.py \
  spikes/004-opm-flow-master/gen_vfp_tables.py \
  spikes/004-opm-flow-master/opm_flow_master_adapter.py \
  tests/test_twoway_flow_no_gsatprod.py \
  tests/test_coupled_workflow.py \
  tests/test_ensemble_setup.py \
  tests/test_exchange_validation.py \
  tests/test_flow_master_spike.py \
  tests/test_flow_model_n_backend.py \
  tests/test_opm_model_n_restart_spike.py \
  tests/test_eclipse_backend.py
```

Repository-wide Ruff currently also scans unrelated historical spikes and WIP
UI code. Use the scoped command above as the coupling-change gate unless that
separate lint debt is intentionally included in the task.

## Corrected guide

The detailed Spanish guide is:

`docs/GUIA_IMPLEMENTACION_ACOPLAMIENTO_BIDIRECCIONAL.md`

The hypothetical Drogon-style/field-model migration guide is:

`docs/DROGON_STYLE_INTEGRATION_GUIDE.md`

It explicitly supersedes the earlier guide that documented the sequential
`Model B -> GSATPROD -> Model A` workflow.
