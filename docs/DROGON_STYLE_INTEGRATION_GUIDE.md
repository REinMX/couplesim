# Drogon-style field integration guide

> **Status: hypothetical integration design, not an executed Drogon case.**
>
> This guide explains how a Drogon-shaped FMU/ERT project, or two similar field
> models, could be connected to this repository's fixed-point coupling driver.
> The repository has only been executed with its small synthetic Flow decks.
> Read [`REPOSITORY_REVIEW.md`](REPOSITORY_REVIEW.md) before attempting a field
> integration.

## 1. Intended use case

Assume two independent reservoir ensembles:

- **Model A**: a Drogon-style FMU model with its own geological uncertainty,
  realization-specific deck, restart files, and well schedule;
- **Model B**: a second Drogon-style FMU model with an independent uncertainty
  model and restartable Flow/Eclipse deck;
- **Network master**: a shared production-network/VFP model that receives rates
  from both reservoirs and returns pressure constraints to both.

The required realization contract is:

```text
ERT realization N
  |
  +-- generate/stage Model A realization N
  +-- generate/stage Model B realization N
  +-- run one coupled forward-model job
      |
      +-- Model A_N reservoir solve ----+
      |                                 |
      +-- Model B_N reservoir solve ----+--> phase rates
                                        |
                                        v
                              shared network solve
                                        |
                               well pressure limits
                                        |
                              returned to A_N and B_N
```

Model A realization `N` must never exchange with Model B realization `M` when
`N != M`.

## 2. Important boundary of the current implementation

The current code is a non-MPI fallback. During each outer fixed-point
iteration, it reruns each reservoir's complete configured restart chain and
solves the network against the resulting full-horizon rate vector. It is best
described as **full-horizon fixed-point or waveform-relaxation co-simulation**.

It is not:

- native `SLAVES` / `GRUPMAST` / `GRUPSLAV` reservoir coupling;
- synchronization at every simulator nonlinear ministep;
- sequential acceptance of one converged report step before solving the next;
- a working adapter for arbitrary Drogon decks without additional code.

If Model A must literally remain the native RC master that owns both its
reservoir and the network, this architecture is not equivalent. Use native
reservoir coupling after its runtime is proven, or explicitly accept the
neutral-network-master architecture used here.

## 3. Preconditions before using field models

Do not start field integration until these reviewed issues are resolved:

1. The exchange currently labels `WOPR + WWPR` as total liquid but renders it
   into a master `ORAT` target. Use consistent `LRAT` semantics or exchange oil
   and water separately.
2. Make schedule dates and report periods configurable; the demonstration
   adapters are effectively fixed to 2024-2026.
3. Make required restart/BHP/continuity checks fail closed.
4. Add simulator timeouts and fresh-output or hash checks.
5. Register the required responses in ERT storage rather than relying only on
   disposable runpath CSV files.

These are field-integration prerequisites, not optional polish.

## 4. Drogon conventions worth preserving

A public Drogon FMU configuration typically provides the following useful
conventions:

```text
RUNPATH .../realization-<IENS>/iter-<ITER>/
ECLBASE eclipse/model/<CASE>-<IENS>
RUN_TEMPLATE <template DATA> <ECLBASE>.DATA
NUM_REALIZATIONS 100
INCLUDE ../input/config/install_custom_jobs.ert
GEN_KW and/or DESIGN_MATRIX uncertainty
one ordered forward-model chain per realization
```

Preserve those conventions. Replace the single direct `FLOW` step with one
coupling-owner step only after both model-generation chains have staged their
realization-specific inputs.

Do not copy Drogon's RMS, observations, Webviz, dataio, SIM2SEIS, or export
workflows blindly. Retain only the parts required by each actual field model.
The coupling job should be one additional layer around established model-build
workflows, not a replacement for them.

## 5. Recommended repository/runpath layout

A practical source layout is:

```text
couplesim-field/
├── ert/
│   ├── model/two_reservoir_coupled.ert
│   ├── input/config/install_custom_jobs.ert
│   └── bin/
│       ├── jobs/RUN_COUPLED_FIELD
│       └── scripts/run_coupled_field.py
├── model_a/
│   ├── eclipse/model/
│   ├── eclipse/include/
│   ├── ert/input/
│   └── rms/                    # only if the model uses RMS
├── model_b/
│   ├── eclipse/model/
│   ├── eclipse/include/
│   ├── ert/input/
│   └── rms/
├── network/
│   ├── model/NETWORK_MASTER.DATA.tmpl
│   └── include/vfp/
├── coupling/
│   ├── field_coupling.json
│   └── schemas/
└── docs/
```

Within one ERT runpath:

```text
realization-N/iter-M/
├── model_a/eclipse/model/MODEL_A-N.DATA
├── model_a/eclipse/include/...
├── model_b/eclipse/model/MODEL_B-N.DATA
├── model_b/eclipse/include/...
├── network/model/NETWORK_MASTER.DATA
├── coupling/
│   ├── convergence_history.csv
│   ├── slave_rates_model_a.csv
│   ├── slave_rates_model_b.csv
│   ├── network_constraints_model_a.csv
│   ├── network_constraints_model_b.csv
│   └── iteration-NNN/...
├── COUPLED_REPORT.txt
└── OK
```

All exchange paths must remain below the current realization runpath.

## 6. ERT ownership pattern

The coupled ERT file should own the complete A/B/network transaction:

```ert
DEFINE <CASE_DIR> two_reservoir_coupled

NUM_REALIZATIONS 100
RANDOM_SEED 123456

RUNPATH      ../../output/<CASE_DIR>/realization-<IENS>/iter-<ITER>
ENSPATH      ../../output/<CASE_DIR>/storage
RUNPATH_FILE ../../output/<CASE_DIR>/runpath_file

QUEUE_SYSTEM LOCAL
QUEUE_OPTION LOCAL MAX_RUNNING 1

INCLUDE ../input/config/install_custom_jobs.ert

-- Illustrative only. Real projects may use many GEN_KW groups and/or
-- DESIGN_MATRIX, but every parameter name must be unique config-wide.
GEN_KW MODEL_A_PARAMS ../input/templates/model_a.tmpl model_a_params.txt ../input/distributions/model_a.dist
GEN_KW MODEL_B_PARAMS ../input/templates/model_b.tmpl model_b_params.txt ../input/distributions/model_b.dist

-- Model-specific generation/staging jobs go first when needed.
-- FORWARD_MODEL BUILD_MODEL_A
-- FORWARD_MODEL BUILD_MODEL_B

-- Exactly one step owns the complete coupled transaction.
FORWARD_MODEL RUN_COUPLED_FIELD
```

For a 100-member acceptance run, use a fail-closed realization threshold. A
sparse command such as `--realizations 0-2` should require all three selected
members, not silently succeed with only one survivor.

Do not launch independently selected Model A and Model B ensemble experiments
and try to join them afterward. That loses the atomic realization boundary.

## 7. Model adapter contract

Each reservoir needs an adapter implementing the same logical interface.

### Inputs

- realization-specific deck/include tree;
- current per-well pressure constraints;
- exact coupling/report dates;
- restart case and restart report step, when continuing;
- model-specific simulator executable/options;
- active realization parameters and provenance.

### Outputs

At every coupling date and for every mapped well:

```text
well
report_date
q_oil_sm3d
q_water_sm3d
q_gas_sm3d
q_liq_sm3d
p_bhp_bar
p_res_bar
```

Also retain:

- simulator name and exact version;
- rendered deck/include hashes;
- input parameter values;
- restart source case/report step;
- FOPT/FWPT/FGPT continuity evidence;
- final PRT health;
- adapter acceptance status.

The network interface should consume phase-consistent rates and return:

```text
well
report_date
p_manifold_bar
p_wellhead_bar
p_bhp_constraint_bar
```

Do not infer unit compatibility from column names alone.

## 8. Field-model mapping file

Keep model-specific naming outside the generic driver. A hypothetical mapping
could look like:

```json
{
  "schedule": {
    "dates": ["2030-01-01", "2030-04-01", "2030-07-01"]
  },
  "master": {
    "backend": "flow",
    "template": "network/model/NETWORK_MASTER.DATA.tmpl",
    "rate_contract": "phase_rates",
    "wells": {
      "A-PROD-1": {"source_model": "model_a", "slot": "NET-A1"},
      "B-PROD-1": {"source_model": "model_b", "slot": "NET-B1"}
    }
  },
  "slaves": {
    "model_a": {
      "backend": "flow",
      "case_template": "model_a/eclipse/model/MODEL_A.DATA",
      "wells": ["A-PROD-1"]
    },
    "model_b": {
      "backend": "flow",
      "case_template": "model_b/eclipse/model/MODEL_B.DATA",
      "wells": ["B-PROD-1"]
    }
  },
  "iteration": {
    "max_iterations": 20,
    "relaxation": 0.4,
    "relative_tolerance": 0.005,
    "rate_floor_sm3d": 1.0
  }
}
```

This is a design example, not a configuration accepted by the current parser.
The current `coupling.json` schema and adapters must be generalized before this
shape can be used.

## 9. Schedule and restart requirements

Before connecting models, prove all of the following independently:

1. Model A and Model B have the same coupling dates, or an explicit and tested
   interpolation/aggregation policy.
2. Dates are compared as dates, not only integer year labels.
3. Restart report-step numbering is correct for each simulator output.
4. The continuation deck reads the prior accepted state, not a stale file from
   another iteration or realization.
5. Cumulative oil, water, and gas production are continuous across restart
   boundaries.
6. BHP constraints land on the intended schedule date and well.
7. Every expected well/date row appears exactly once.
8. Shut wells, new wells, renamed wells, and missing wells have explicit policy.

The demonstration code reruns the full horizon during each outer iteration.
True sequential report-step acceptance would require a different state machine:
converge one date interval, freeze its accepted restart, then begin the next.

## 10. Network integration requirements

For a real network master:

- preserve the field `NETWORK`/VFP/group topology under source control;
- map every reservoir well to one unambiguous network slot/source;
- choose oil, water, gas, liquid, and reservoir-rate semantics explicitly;
- validate surface-condition and unit conventions;
- validate VFP table axes and extrapolation range;
- distinguish Flow-calculated well BHP from any adapter-computed
  wellhead-plus-hydrostatic estimate;
- prove that changing rates from either reservoir changes network pressure;
- prove that changing network backpressure changes rates in both reservoirs.

A `GSATPROD` source is acceptable only for a genuinely prescribed external
source. It is not a substitute for Model B when backpressure must change Model
B's simulated rates.

## 11. Convergence policy

The current residual is:

```text
q_forwarded = q_previous + omega * (q_raw - q_previous)

residual = max(
    abs(q_raw - q_previous) /
    max(abs(q_previous), epsilon)
)
```

For a field model, define convergence by phase, well, and date. Consider both:

- relative tolerance for normal-rate wells;
- absolute tolerance for low-rate, shut, or newly opened wells.

Recommended acceptance record:

```text
iteration
model
well
report_date
phase
raw_rate
previous_forwarded_rate
new_forwarded_rate
absolute_residual
relative_residual
```

Fail the realization if any required residual remains outside tolerance at the
iteration limit. Do not treat a small relaxation update as convergence.

## 12. Units and sign checklist

Before the first coupled run, record and test:

- deck unit system for A, B, and the network;
- oil/water surface volume units;
- gas surface volume units;
- pressure units and datum/depth convention;
- production and injection sign convention;
- liquid definition;
- standard-condition definitions;
- wellhead versus bottom-hole pressure meaning;
- whether network constraints are limits or exact controls.

A unit mismatch can converge numerically while remaining physically wrong.
Include at least one unit-conversion test with known values.

## 13. Suggested migration sequence

### Phase 0 — freeze the source models

- Record exact Model A and Model B commits/tags.
- Run each standalone realization deterministically.
- Record simulator versions and clean PRT evidence.
- Confirm restartability and required summary vectors.

### Phase 1 — adapt one model only

- Build the Model A adapter against fixed pressure constraints.
- Prove constraint sensitivity.
- Prove restart continuity.
- Prove sampled parameters change the rendered deck and simulator response.

Repeat independently for Model B.

### Phase 2 — validate the network master

- Run fixed rate tables from both models.
- Sweep each source independently.
- Verify pressure response, phase behavior, VFP range, and control switching.

### Phase 3 — deterministic coupled case

- Use one fixed A case and one fixed B case.
- Start with relaxed fixed-point iteration.
- Require convergence history and clean PRTs for every simulator invocation.
- Compare coupled and standalone responses physically.

### Phase 4 — one paired ERT realization

- Run `ert lint`.
- Run `ert test_run` in a fresh case namespace.
- Verify parameter propagation, exchange coverage, restart lineage, and ERT
  response storage.

### Phase 5 — sparse ensemble

Run three deliberately different members:

```bash
ert ensemble_experiment \
  two_reservoir_coupled.ert \
  --realizations 0-2 \
  --current-ensemble coupled_acceptance_0_2 \
  --disable-monitoring
```

Require three `OK` members and inspect all three. Do not let the collector hide a
failed selected member.

### Phase 6 — scale out

Only after the sparse acceptance passes:

- set scheduler concurrency from measured memory per realization;
- set runtime limits;
- run the intended ensemble size;
- require the exact expected success count;
- retain lightweight manifests and response data even if heavy simulator files
  are later archived.

## 14. Portable verification commands

From a configured execution host:

```bash
command -v flow
flow --version
command -v summary
command -v ert
ert --version

cd ert/model
ert lint two_reservoir_coupled.ert
ert test_run two_reservoir_coupled.ert --disable-monitoring
```

Project-specific Python checks should run from a clean clone using declared
dependencies, not host-specific cache paths.

For every accepted realization verify:

- `OK` exists;
- convergence is true;
- all selected realization IDs are present;
- both rate files and both pressure-constraint files are complete;
- every expected simulator invocation has a PRT;
- every final PRT summary is `Warnings 0 / Errors 0 / Problems 0`;
- sampled parameters appear in rendered inputs;
- required ERT responses exist in storage;
- no exchange crosses realization boundaries.

## 15. Example interpretation

A successful hypothetical result might read:

```text
Realization 17
  Model A geology draw: A17
  Model B geology draw: B17
  Network case: nominal
  Outer iterations: 9
  Maximum final residual: 0.0038
  All simulator PRTs: 0/0/0
  Model A rate reduced after network feedback: yes
  Model B rate reduced after network feedback: yes
  Restart/cumulative continuity: accepted
  ERT response storage: complete
```

This would prove one paired realization executed correctly. It would not by
itself validate the network hydraulics, field calibration, forecast quality, or
native reservoir-coupling equivalence.

## 16. Decision: fixed-point fallback or native RC

Use this repository's fixed-point pattern when:

- a neutral external network master is acceptable;
- report-date exchange is sufficient;
- adapters can safely rerun or continue the reservoir models;
- transparent files and provenance are more important than in-memory coupling.

Prefer native RC when:

- the simulator must synchronize master and slaves internally;
- Model A must retain literal ownership of the network;
- timestep negotiation and group controls must be native;
- the runtime has been proven on the target MPI/cluster environment.

Do not switch architecture silently. Record the ownership and synchronization
decision as part of the model's technical basis.

## 17. Related repository documentation

- [`../README.md`](../README.md): runnable synthetic example and commands.
- [`GUIA_IMPLEMENTACION_ACOPLAMIENTO_BIDIRECCIONAL.md`](GUIA_IMPLEMENTACION_ACOPLAMIENTO_BIDIRECCIONAL.md): Spanish description of the current prototype, with the accuracy note at its top.
- [`REPOSITORY_REVIEW.md`](REPOSITORY_REVIEW.md): verified findings and blockers.

Public references:

- Equinor `fmu-drogon`: <https://github.com/equinor/fmu-drogon>
- Equinor `standalones2rc`: <https://github.com/equinor/standalones2rc>

This guide intentionally describes a migration design. It does not claim that
the public Drogon model or any field model has been coupled successfully by
this repository.
