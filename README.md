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

OPM Flow spikes — validated in isolation; Spike 003's restart backend and
Spike 004's network master are wired into the ERT driver as optional
backends (see "Hybrid mode" below):

- [Spike 001 — real `model_n` BHP→rate round trip](spikes/001-opm-model-n-roundtrip/README.md)
- [Spike 002 — real network master / GSATPROD limits](spikes/002-opm-network-master/README.md)
  (VALIDATED on Flow 2026.04: Flow simulates NETWORK + well VFP, honours
  GSATPROD in group totals/`GCONPROD`, and **satellites load trunk branch
  VFPs** — was PARTIAL on 2025.10, where prescribed rates stayed out of the
  network hydraulics)
- [Spike 003 — stateful restart-based `model_n` backend](spikes/003-opm-model-n-restart/README.md)
  (VALIDATED: Flow 2025.10 continues a slave across annual years via the
  Eclipse `RESTART` keyword with per-year BHP constraints, carries cumulative
  state, and emits all years in the exchange schema)
- [Spike 004 — real OPM Flow network master](spikes/004-opm-flow-master/README.md)
  (VALIDATED on Flow 2026.04: a Flow NETWORK deck serves as the coupled
  master — four real slave wells load the trunk, GSATPROD registers in group
  totals and loads the trunk, and the all-real realization converges in 13
  iterations)

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

## Hybrid mode: real OPM Flow slave backends (the default)

The restart-based backend from Spike 003 is wired into the driver as an
optional backend for **each** slave (`model_n` and `model_hdn`), and is the
**repo default** (`coupling.json` ships with both slaves on `flow`). The
topology stays hybrid: real Flow restart chains for both slaves, dummy
`master_network`, prescribed GSATPROD source unchanged. With both slaves on
Flow, every coupled reservoir is a real simulator:

```json
"slaves": {
  "model_n": { "role": "coupled_slave", "deck": "MODEL_N.DATA", "backend": "flow" },
  "model_hdn": { "role": "coupled_slave", "deck": "MODEL_HDN.DATA", "backend": "flow" }
}
```

To run fully licence-free (no Flow required), set both slaves to
`"backend": "dummy"` in `coupling.json`. The `--backend-model-n flow` CLI
flag still forces the model_n flow backend for a standalone run.

The master also supports `flow` (`"backend": "flow"` on the master config),
which runs the Spike 004 network deck: four real slave wells with VFP tables
load a shared trunk, the prescribed profile enters as GSATPROD group totals
(**and loads the trunk VFP on Flow ≥ 2026.04**, Spike 002 VALIDATED), and
the per-well BHP constraints come from the Flow network solve
(`p_bhp = node pressure + wellbore hydrostatic`). With the master and both
slaves on flow, every coupled model is a real simulator. When any flow
backend is enabled the driver fails **before staging** if `flow` or `summary`
is not on PATH.

Per coupling iteration the driver:

1. runs the master — dummy, or the Spike 004 Flow network deck
   (`coupling/flow_master/iteration-NNN/`) — and writes
   `network_constraints_<slave>.csv`;
2. runs `spikes/003-opm-model-n-restart/opm_model_n_restart_adapter.py`
   (`--model <slave>`) for each flow slave against those constraints,
   producing a fresh 3-year 2024–2026 restart chain under
   `coupling/flow_<slave>/iteration-NNN/`;
3. applies the coupling relaxation to each raw Flow response — raw rates are
   reported as `q_ipr_sm3d` (the unrelaxed fixed-point target used by the
   convergence criterion) and the rates forwarded to the master are
   `q_liq_sm3d = q_prev + relaxation * (q_raw - q_prev)`, exactly mirroring
   what the dummy slave does internally. Without this step the steep
   rate-dependent network back-pressure at Flow-scale rates makes the raw
   fixed-point map oscillate.

The dummy master's friction parameters were recalibrated for real-simulator
flow scale (trunk `0.0002/5e-8`, branch `0.0005/1e-7` bar·sm³/d terms; the
previous values were tuned for the dummy's 250–900 sm³/d rates). At the old
values the combined Flow-scale total rate drove trunk/branch pressure above
the hdn reservoir pressure (H-P2 BHP 316.4 bar vs 315 bar initial) — the
adapter's fail-fast guard caught it, and the fix is a network property, not a
deck hack. The Flow master's own VFP tables are calibrated separately
(Spike 004).

Verified on Flow **2026.04** — **all-real** (master + both slaves on flow,
relaxation 0.4, Q0_MULT=1.0): converged in 13 of 20 iterations, tolerance
0.005:

| Iteration | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Residual | 455% | 77% | 33% | 22% | 12% | 8.1% | 5.3% | 3.4% | 2.1% | 1.3% | 0.81% | 0.50% | 0.31% |

Final Flow rates show the expected depletion decline across years; final
master constraints (2024) honour the network with all wells delivering:
N-P1 BHP 306.9, N-P2 303.6, H-P1 290.2, H-P2 287.0 bar — all below the
350/315 bar slave caps:

| Well | 2024 | 2025 | 2026 |
|---|---:|---:|---:|
| N-P1 q_liq sm³/d | 2286.0 | 1848.3 | 1577.3 |
| N-P2 q_liq sm³/d | 1006.5 | 679.6 | 457.4 |
| H-P1 q_liq sm³/d | 1330.2 | 1150.9 | 1043.6 |
| H-P2 q_liq sm³/d | 559.3 | 446.7 | 368.7 |

(The all-real numbers are unchanged between Flow 2025.10 — 12 iterations,
0.48% — and 2026.04 — 13 iterations, 0.31%; the 2026.04 satellite-loading
behaviour is covered by Spike 002's re-verified probe.)

`COUPLED_REPORT.txt` names the backends (`slave backends: model_n=flow,
model_hdn=flow`), and `coupling/exchange/slave_result_<slave>_iteration_NNN.json`
records the raw/relaxed rate paths and the relaxation factor. The repo
default (dummy master + both flow slaves) converges in **7 iterations at
relaxation 0.6** — verified via ERT `test_run` on 2026.04 with the sampled
Q0_MULT.

Note: `Q0_MULT` scales the dummy slaves' productivity; the Flow backend decks
have fixed productivity, so with the default config (both slaves on flow)
`Q0_MULT` does not change the slave response. It only matters when a slave
is on the dummy backend.

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
├── coupling.json                # hybrid default: dummy master + both Flow slaves
├── configs/coupling.fast.json   # all-dummy smoke config (no Flow needed)
├── run_demo.sh
├── bin/
│   └── eclipse_dummy.py
├── ert/
│   ├── model/
│   │   ├── 01_coupled.ert               # legacy two-realization smoke
│   │   ├── 02_ensemble_coupled.ert      # THE coupled ensemble (per-model GEN_KW)
│   │   ├── master_network.ert           # standalone ensemble, master alone
│   │   ├── model_n.ert                  # standalone ensemble, model_n alone
│   │   └── model_hdn.ert                # standalone ensemble, model_hdn alone
│   └── bin/
│       ├── jobs/RUN_COUPLED
│       ├── jobs/RUN_MASTER
│       ├── jobs/RUN_MODEL_N
│       ├── jobs/RUN_MODEL_HDN
│       └── scripts/
│           ├── run_coupled.py           # coupled driver (one realization = 3 models)
│           ├── run_standalone.py        # one-model driver (per-model ensembles)
│           └── collect_ensemble.py      # ensemble_results.csv + P10/P50/P90
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
│   ├── templates/          # one GEN_KW template per parameter
│   └── distributions/      # one priors file per parameter group
└── tests/
    ├── test_coupled_workflow.py
    └── test_ensemble_setup.py
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

## Ensembles (ERT + FMU pattern)

The example follows the FMU convention of **one independent ERT file per
model**, with the model inputs in project folders under `input/<model>/`:

| Config | What one realization runs |
|---|---|
| `ert/model/master_network.ert` | the master alone (network hydraulics, `NETWORK_CHOKE`) |
| `ert/model/model_n.ert` | `model_n` alone (productivity, `Q0_MULT_MODEL_N`) |
| `ert/model/model_hdn.ert` | `model_hdn` alone (productivity, `Q0_MULT_MODEL_HDN`) |
| `ert/model/02_ensemble_coupled.ert` | **the coupled system**: master + both slaves in one realization |

The standalone configs are the per-model quality checks (the role your
100-realization FMU setups play for each model). The coupled config is the
production ensemble: every realization draws an **independent** sample from
each parameter group and runs the full master/slave fixed-point loop with the
chosen simulator backends:

```text
GEN_KW Q0_MULT_MODEL_N    -> model_n simspec q0     (slave 1 productivity)
GEN_KW Q0_MULT_MODEL_HDN  -> model_hdn simspec q0   (slave 2 productivity)
GEN_KW NETWORK_CHOKE      -> master simspec network (shared friction loss)
```

Backends come from `coupling.json`: the repo default is the hybrid — dummy
master network solver + both slaves on **real OPM Flow** (Eclipse-compatible
decks, so the same files run under Eclipse when the licence arrives). Set
`"backend": "dummy"` per slave (or use `configs/coupling.fast.json` via
`COUPLING_CONFIG`) for the licence-free path.

Run the coupled ensemble (from `ert/model/`):

```bash
cd ert/model
PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH \
  ert test_run 02_ensemble_coupled.ert        # 1 realization, parse+exec gate
PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH \
  ert ensemble_experiment 02_ensemble_coupled.ert   # all realizations
# fast smoke without Flow (all-dummy config):
COUPLING_CONFIG=/absolute/path/configs/coupling.fast.json \
  PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH \
  ert ensemble_experiment 02_ensemble_coupled.ert
```

`COUPLING_CONFIG` is an absolute path (the driver resolves it from the
realization runpath). `NUM_REALIZATIONS` is `10`; set `100` when you plug in
the real FMU decks.

Aggregate the completed realizations (params + iterations + final rates +
nearest-rank P10/P50/P90):

```bash
cd /home/javier/projects/coupled-sim-eclipse
python3 ert/bin/scripts/collect_ensemble.py --case-dir output/02_coupled
```

Verified on ERT 23.0.1 + flow 2026.04:

- `ert test_run 02_ensemble_coupled.ert` (hybrid, real Flow slaves): 1/1
  finished, converged in 7/12 iterations (residual 4.17 -> 0.0034, tol 0.005);
- `ert ensemble_experiment` (3 realizations, dummy smoke): 3/3 finished, 9
  iterations each, `ensemble_results.csv` + `ensemble_summary.csv` produced;
- standalone `ert test_run` for all three per-model configs: 1/1 each; the
  `model_n` standalone with the real Flow backend runs the full 2024-2026
  restart chain against static reference-IPR constraints.


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

The "Hybrid mode" section above implements this loop end-to-end for `model_n`
against the dummy master (steps 1–7, with the annual Flow restart chain
standing in for step 2's continue-to-coupling-date), using the same exchange
artifacts a real Eclipse adapter would consume.

OPM Flow 2026.04 is installed at `/usr/bin/flow`. Verified by spikes:

- Flow runs Eclipse standard (`GRUPNET`) and extended (`NETWORK`/`BRANPROP`/
  `NODEPROP`) network models; real wells load trunk VFP and see back-pressure.
- `GSATPROD` is parsed, contributes to group totals, and participates in
  `GCONPROD` limits — and on Flow **2026.04** satellites also load the
  network branch VFPs (Spike 002 re-verified, VALIDATED).
- Therefore the illustrative `input/master_network` decks remain scaffolds;
  the production coupling path can now use either the external network
  solver (`eclipse_dummy.py`, the repo default) or the Flow NETWORK master
  (Spike 004) — the latter carries the full hydraulic load including the
  GSATPROD satellites on Flow ≥ 2026.04. The stateful restart-based slave
  backend was validated in [Spike 003](spikes/003-opm-model-n-restart/README.md)
  and the real network master in [Spike 004](spikes/004-opm-flow-master/README.md);
  both are wired into the ERT driver as optional backends, so the all-real
  realization (Flow master + Flow model_n + Flow model_hdn, prescribed
  GSATPROD source with real hydraulic load) runs licence-free today. The
  remaining production steps are deck substitution (real lift curves instead
  of calibrated VFP tables) and the licence-time master swap.

## Configuration reference

`coupling.json` defines:

- annual schedule and report years;
- fixed-point tolerance, maximum iterations, and relaxation;
- prescribed GSATPROD profile sources;
- initial slave-rate guesses;
- the master model (backend `dummy` by default, `flow` optional); and
- both required coupled slaves (backend `flow` by default — the repo
  default is the hybrid; set `"backend": "dummy"` for the fully
  licence-free path).

The master and slave backends are validated before staging: unknown
backends, missing Spike adapter files, or missing `flow`/`summary`
executables fail the realization early.

`input/*/simspec.json` contains only dummy physics. It is not a substitute for
FMU model configuration.

## ERT path rules used here

- `GEN_KW` input paths are relative to `ert/model/`.
- `RUNPATH`, `ENSPATH`, and `RUNPATH_FILE` are relative to `ert/model/`.
- the job `EXECUTABLE` is relative to `ert/bin/jobs/<JOB>`.
- `GEN_KW` writes `<group>.txt` into each realization runpath; the drivers
  resolve `q0_mult_model_n.txt`, `q0_mult_model_hdn.txt`, `network_choke.txt`
  (legacy shared `q0_mult.txt` still works).
- **GEN_KW parameter names must be unique across the whole config**: a priors
  file listing two names cannot be shared by two groups — one priors file per
  group (`input/distributions/q0_mult_model_n_priors.txt`, etc.).
- **`--` starts a comment in ERT job syntax**: a flag in `ARGLIST` must be
  quoted (`ARGLIST "--model" model_n`), exactly like the ert_fmu lab's
  `MAKE_RELPERM` job.
- the job script must be executable (`chmod +x`).
- `COUPLING_CONFIG` overrides the coupling JSON for the drivers; it must be an
  absolute path because it is resolved from the realization runpath.
- launch ERT with its venv on `PATH` so `fm_dispatch.py` is available.
- use `ert test_run` as the real parse/execution gate; `ert lint` alone is not
  sufficient for this ERT version.
- ERT owns the outer ensemble parallelism; the Python forward-model job owns
  the inner, per-realization model coupling.
