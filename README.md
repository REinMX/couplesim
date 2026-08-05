# Coupled reservoir/network simulation example with ERT

A licence-free, executable example of one ERT realization coordinating:

- a **dummy reservoir master that hosts the shared subsea network**;
- **model_n**, a simulated reservoir slave;
- **model_hdn**, a simulated reservoir slave; and
- prescribed **GSATPROD-style production profiles for external wells** that
  are inputs to the network in addition to both slaves' simulation results.

The Python dummy makes the orchestration and exchange files testable without
an Eclipse licence. The included `.DATA`, `NETWORK`, and `GSATPROD` records under
`input/` are illustrative scaffolds, **not simulator-validated production
decks**.

Isolated OPM Flow spikes (not yet wired into the ERT driver):

- [Spike 001 — real `model_n` BHP→rate round trip](spikes/001-opm-model-n-roundtrip/README.md)
- [Spike 002 — real network master / GSATPROD limits](spikes/002-opm-network-master/README.md)
  (PARTIAL: Flow simulates NETWORK + well VFP and honours GSATPROD in group
  totals/`GCONPROD`, but satellite rates do **not** load trunk branch VFPs on
  Flow 2025.10 — keep the external network solver for prescribed hydraulic load)

## Intended topology

```text
 prescribed external profiles
 GSATPROD-style input
 (EXT-P1, EXT-P2)
           │
           │ immutable rates every coupling iteration
           ▼
 ┌─────────────────────────────────────────────────────┐
 │ master_network                                      │
 │ dummy reservoir + shared subsea network             │
 │                                                     │
 │ network input = prescribed profiles                 │
 │               + model_n simulated rates             │
 │               + model_hdn simulated rates           │
 └──────────────┬──────────────────────┬───────────────┘
                │ pressure constraints │ pressure constraints
                ▼                      ▼
      ┌──────────────────┐   ┌───────────────────┐
      │ model_n          │   │ model_hdn         │
      │ reservoir slave  │   │ reservoir slave   │
      │ N-P1, N-P2       │   │ H-P1, H-P2        │
      └────────┬─────────┘   └─────────┬─────────┘
               │ simulated rates       │ simulated rates
               └────────────► network ◄┘
                       next iteration
```

The external profile wells are deliberately disjoint from the four simulated
slave wells. This proves that the network receives **three independent source
categories**, rather than treating one slave as a static profile.

## One ERT realization

ERT launches one installed forward-model job, `RUN_COUPLED`. The driver at
`ert/bin/scripts/run_coupled.py` stages all three model folders and performs a
fixed-point loop:

1. Start with rate guesses for the four slave wells.
2. `master_network` reads:
   - `input/master_network/profiles/gsatprod_external.inc`;
   - `coupling/slave_rates_model_n.csv`; and
   - `coupling/slave_rates_model_hdn.csv`.
3. The master combines all rates in one shared-network calculation and writes:
   - `coupling/network_constraints_model_n.csv`; and
   - `coupling/network_constraints_model_hdn.csv`.
4. Both slaves simulate against their network BHP constraints and overwrite
   their `slave_rates_*.csv` results.
5. Repeat until the maximum relative **unrelaxed fixed-point residual**—the
   difference between the rates supplied to the network and the slaves' raw
   IPR target rates—is below the configured tolerance. This criterion is
   independent of the relaxation factor, so tiny relaxation cannot create
   false convergence. A non-converged run exits nonzero so ERT cannot silently
   accept a partial result.

The dummy uses annual 2024–2026 report steps, a linear IPR for each slave well,
shared trunk pressure loss based on **total liquid network rate**, per-well
branch loss, hydrostatic head, and under-relaxation. The gas-rate, prescribed
PWH/BHP, and GSAT columns are retained in exchange artifacts for provenance but
do not drive this synthetic pressure calculation. It is an orchestration
example, not a calibrated or multiphase network model.

## Inspectable exchange contract

Every runpath contains evidence of the data flow:

```text
coupling/
├── slave_rates_model_n.csv
├── slave_rates_model_hdn.csv
├── network_constraints_model_n.csv
├── network_constraints_model_hdn.csv
├── convergence_history.csv
└── exchange/
    ├── network_request_iteration_001.json
    ├── network_response_iteration_001.json
    ├── slave_result_model_n_iteration_001.json
    ├── slave_result_model_hdn_iteration_001.json
    └── ... one set per coupling iteration
```

Each `network_request_iteration_*.json` separates:

```json
{
  "sources": {
    "prescribed_profiles": {
      "external_satellite": {"keyword": "GSATPROD", "rows": []}
    },
    "simulated_slaves": {
      "model_n": [],
      "model_hdn": []
    }
  },
  "totals_by_year": []
}
```

That file is the primary acceptance artifact for the required topology.

## Repository layout

```text
coupled-sim-eclipse/
├── README.md
├── coupling.json
├── run_demo.sh
├── bin/
│   └── eclipse_dummy.py
├── ert/
│   ├── model/01_coupled.ert
│   └── bin/
│       ├── jobs/RUN_COUPLED
│       └── scripts/run_coupled.py
├── input/
│   ├── master_network/
│   │   ├── MASTER.DATA
│   │   ├── simspec.json
│   │   ├── network/network.inc
│   │   └── profiles/gsatprod_external.inc
│   ├── model_n/
│   │   ├── MODEL_N.DATA
│   │   └── simspec.json
│   ├── model_hdn/
│   │   ├── MODEL_HDN.DATA
│   │   └── simspec.json
│   ├── templates/q0_mult.tmpl
│   └── distributions/q0_priors.txt
└── tests/
    └── test_coupled_workflow.py
```

Generated runpaths are written below `output/` and ignored by Git.

## Quick start without Eclipse or ERT

```bash
cd /home/javier/projects/coupled-sim-eclipse
./run_demo.sh
```

The final report is:

```text
output/demo/realization-0/COUPLED_REPORT.txt
```

Inspect the final network request with, for example:

```bash
python3 -m json.tool \
  output/demo/realization-0/coupling/exchange/network_request_iteration_008.json
```

The exact final iteration can vary if the tolerance or relaxation is changed.

## Automated tests

The suite uses only the Python standard library:

```bash
python3 -m unittest discover -s tests -v
```

It verifies that:

- the network request contains the prescribed profile and both slave outputs;
- prescribed and simulated wells do not overlap;
- network totals equal prescribed plus simulated rates;
- both slaves receive network constraints for 2024–2026;
- shared manifold pressure uses prescribed plus simulated flow;
- malformed or incomplete prescribed-profile coverage is rejected;
- non-finite profile, exchange, model, and ERT parameter values are rejected;
- profile keywords not implemented by the dummy parser are rejected;
- invalid coupling controls and unsafe model identifiers are rejected before
  runpath changes;
- convergence uses the unrelaxed fixed-point residual, so tiny relaxation
  cannot create false convergence;
- missing or malformed ERT parameters are not silently defaulted;
- non-convergence fails the forward model; and
- the report states all three network-input categories.

## ERT execution

The example was developed against ERT 23.0.1 in the existing local venv:

```bash
cd ert/model
PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH \
  ert test_run 01_coupled.ert

PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH \
  ert ensemble_experiment 01_coupled.ert
```

Verification on ERT 23.0.1:

- `ert test_run`: 1/1 realization passed;
- `ert ensemble_experiment`: 2/2 realizations passed;
- realization 0 converged in 9/12 iterations and realization 1 in 8/12,
  using the unrelaxed fixed-point residual; and
- sampled `Q0_MULT` values `0.836549` and `0.999312` propagated through both
  reservoir slaves while the prescribed external profile remained unchanged.

`NUM_REALIZATIONS` is intentionally `2` for a cheap example. Change it to
`100` when integrating the real FMU ensembles. `Q0_MULT` is sampled from
`UNIFORM 0.8 1.2` and scales the productivity of both reservoir slaves; the
prescribed external profiles remain unchanged.

ERT owns the outer ensemble parallelism. The Python forward-model job owns the
inner, per-realization model coupling.

## Mapping to the real FMU/Eclipse setup

Do **not** replace `eclipse_dummy.py` with a single `flow MODEL.DATA` call and
assume coupling is complete. A real adapter must implement each boundary:

1. Stage the selected realization of `model_n`, `model_hdn`, and the master.
2. Run/continue both slave simulators to the coupling date.
3. Extract the required well rates and other network-bound quantities from
   their summary/restart outputs.
4. Render those dynamic values into the master/network inputs **while also
   retaining the prescribed GSATPROD/GSATPTAB profile sources**.
5. Run/continue the master/network model.
6. Extract network-calculated constraints such as wellhead pressure, BHP,
   choke setting, lift-gas allocation, or group limits.
7. Render those constraints back into each slave's supported control format.
8. Iterate at the same coupling date, then advance when converged.
9. Fail the realization when a simulator fails, exchange coverage is
   incomplete, or coupling does not converge.

The concrete Eclipse coupling keywords, restart commands, summary vectors, and
GSATPROD/GSATPTAB record layouts depend on the Eclipse version and your current
FMU deck conventions. They must be copied from your working setup/manual; this
repository does not claim to validate proprietary keyword syntax.

OPM Flow 2025.10 is installed at `/usr/bin/flow`. Verified by spikes:

- Flow runs Eclipse standard (`GRUPNET`) and extended (`NETWORK`/`BRANPROP`/
  `NODEPROP`) network models; real wells load trunk VFP and see back-pressure.
- `GSATPROD` is parsed, contributes to group totals, and participates in
  `GCONPROD` limits — but does **not** load network branch VFPs on 2025.10.
- Therefore the illustrative `input/master_network` decks remain scaffolds; the
  production coupling path keeps the external network solver for prescribed
  satellite hydraulic load until either Flow gains that behaviour or an Eclipse
  licence is available. The next integration step for slaves is a stateful,
  restart-based backend.

## Configuration reference

`coupling.json` defines:

- annual schedule and report years;
- fixed-point tolerance, maximum iterations, and relaxation;
- prescribed GSATPROD profile sources;
- initial slave-rate guesses;
- the master model; and
- both required coupled slaves.

`input/*/simspec.json` contains only dummy physics. It is not a substitute for
FMU model configuration.

## ERT path rules used here

- `GEN_KW` input paths are relative to `ert/model/`.
- `RUNPATH`, `ENSPATH`, and `RUNPATH_FILE` are relative to `ert/model/`.
- the job `EXECUTABLE` is relative to `ert/bin/jobs/RUN_COUPLED`.
- `GEN_KW` writes `q0_mult.txt` into each realization runpath.
- launch ERT with its venv on `PATH` so `fm_dispatch.py` is available.
- use `ert test_run` as the real parse/execution gate; `ert lint` alone is not
  sufficient for this ERT version.
