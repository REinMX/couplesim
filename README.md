# Coupled reservoir + network simulation example (Eclipse-style, ERT/FMU)

An **example** of how to simulate two (three, actually) reservoir models
"at the same time" in one ERT realization, following the FMU conventions
of your existing setups, **without needing an Eclipse licence**.

```
                 ┌───────────────────────────────────┐
                 │  master_network  (master, dummy)  │
                 │  reservoir + subsea NETWORK model │
                 └────────────────┬──────────────────┘
                                  │ GSATPROD tables written per coupling
                                  │ iteration: coupling/gsatprod_model_n.inc
                                  ▼
                 ┌───────────────────────────────────┐     ┌──────────────────────────────────┐
                 │  model_n  (coupled slave)         │     │  model_hdn  (static slave)       │
                 │  subsea wells N-P1, N-P2          │     │  subsea wells H-P1, H-P2         │
                 │  profiles come from the network   │     │  profiles stay in GSATPROD file  │
                 └────────────────┬──────────────────┘     └────────────────┬─────────────────┘
                                  │ actual rates back:                      │ static profile:
                                  │ coupling/slave_rates_model_n.csv        │ input/static_profiles/
                                  └─────────────── loop until converged ◀───┘
```

| Model | Role | Production profiles |
|---|---|---|
| `master_network` | master — dummy reservoir + network model | solves the network (manifold pressure + riser friction + hydrostatics) |
| `model_n` | coupled slave | GSATPROD table **rewritten every coupling iteration** by the master |
| `model_hdn` | static slave | GSATPROD table stays **static** (the "one model remains in gsatprod" mode) |

## The coupling loop

Each ERT realization runs the three models through one forward-model step,
`RUN_COUPLED` (`ert/bin/scripts/run_coupled.py`):

1. **Master** reads the slave wells' demanded rates (previous iteration, or
   the initial guess in `coupling.json`), solves the network and writes a
   `GSATPROD` production-profile table for each **coupled** slave:
   `coupling/gsatprod_model_n.inc`.
2. **Slaves** simulate with their GSATPROD table (coupled = the one just
   written; static = the file copied from `input/static_profiles/`) and
   report their actual rates back: `coupling/slave_rates_<model>.csv`.
3. **Convergence check**: max relative change of the coupled slaves' rates
   vs the previous iteration. Below `coupling.tolerance` → converged;
   otherwise iterate (up to `coupling.max_iterations`).

This is a fixed-point (explicit sequential) coupling on well rates — the
same pattern used to couple a network simulator to one or more reservoir
models. It converges in 2–3 iterations for this example; real coupled
setups use the same loop with tighter controls and per-timestep exchange.

## Folder layout

```
coupled-sim-eclipse/
├── README.md
├── coupling.json              # coupling protocol: models, modes, wells, tolerance
├── run_demo.sh                # standalone demo, no ERT needed
├── bin/
│   └── eclipse_dummy.py       # stand-in "simulator" (plays eclipse100/flow role)
├── ert/
│   ├── model/01_coupled.ert   # the ERT config
│   └── bin/
│       ├── jobs/RUN_COUPLED   # ERT job definition (INSTALL_JOB)
│       └── scripts/run_coupled.py  # forward-model driver (ERT job + demo runner)
├── input/
│   ├── master_network/        # MASTER.DATA + network/ + simspec.json
│   ├── model_n/               # MODEL_N.DATA + simspec.json (coupled slave)
│   ├── model_hdn/             # MODEL_HDN.DATA + simspec.json (static slave)
│   ├── static_profiles/       # gsatprod_model_hdn.inc (static GSATPROD table)
│   ├── templates/q0_mult.tmpl # GEN_KW template
│   └── distributions/         # GEN_KW priors (UNIFORM 0.8 1.2)
└── output/                    # generated runs (gitignored)
```

## Quick start

**1. Standalone demo (no ERT, no licence):**

```bash
./run_demo.sh
# writes output/demo/realization-0/ ... COUPLED_REPORT.txt
```

**2. With ERT** (example was verified against `ert 23.0.1`; the venv in
`/home/javier/projects/ert_fmu/.venv` has it). Remember the PATH trap:
launch ERT with the venv on `PATH` so `fm_dispatch.py` resolves:

```bash
cd ert/model
PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH ert test_run 01_coupled.ert
# or, for the full ensemble:
PATH=/home/javier/projects/ert_fmu/.venv/bin:$PATH ert ensemble_experiment 01_coupled.ert
```

`NUM_REALIZATIONS` is set to 2 in the config — **set it to 100** (as in your
FMU setups) once you plug in the real decks. Each realization samples
`Q0_MULT` (uniform 0.8–1.2) and scales the coupled slave's productivity, so
the ensemble shows run-to-run rate variation — the same role permeability/
rock parameters play in your real FMU setups.

## Mapping to your real FMU setup

Your existing 100-realization ERT/FMU setups for `model_n` and `model_hdn`
already contain everything that is a placeholder here:

- **Decks**: replace `input/<model>/*.DATA` (+ include trees) with your
  real decks; keep the `INCLUDE 'coupling/gsatprod_<model>.inc' /` line in
  the SCHEDULE so the driver keeps feeding the profiles.
- **Network model**: replace `input/master_network/network/network.inc`
  with your real NETWORK block; the master's `simspec.json` is only a
  stand-in for the network solve.
- **GSATPROD / GSATPTAB**: the table format in this example
  (`WELL YEAR Q_LIQ Q_GAS P_WH P_BHP GSAT`) is a documented convention —
  adapt `bin/eclipse_dummy.py`'s `parse_gsatprod_inc()`/writer to the exact
  keyword syntax your decks consume. If your setup uses `GSATPTAB`, write
  the same profile in that keyword's format; the coupling mechanism is
  identical.
- **Parameters**: `GEN_KW Q0_MULT` stands in for your real rock/permeability
  parameterization (`GEN_KW ROCK <template> <include> <priors>` style).
- **Observations**: none here; add `SUMMARY` keys + an `.obs` config
  (`SUMMARY_OBSERVATION`) exactly as in `ert_fmu/04_history_match.ert`
  when you move to history matching.

## Going real: swap the dummy simulators for Eclipse / OPM Flow

The "simulators" are `bin/eclipse_dummy.py` invocations inside
`ert/bin/scripts/run_coupled.py` (`run_dummy()`). To go real:

1. Replace the `run_dummy()` body per model with the real command, e.g.
   `eclrun eclipse100 <deck>` or `flow <deck>` (OPM Flow is already
   installed on this machine at `/usr/bin/flow`), running with cwd =
   `<runpath>/<model>/`.
2. The decks then consume the GSATPROD includes directly — that is the
   actual coupling interface; the CSV files are only for the dummy.
3. With real simulators, each model writes its own summary; point `ECLBASE`
   per model inside the runpath (FMU convention:
   `eclipse/model/<NAME>`), and uncomment the `SUMMARY` block so ERT can
   read responses. Note: one `ECLBASE` per config — for per-model summaries
   use the FMU pattern of separate summary dirs per model or `GEN_DATA`.
4. Real coupled workflows exchange **more** than profiles (BHP, lift gas,
   network pressures) and iterate within timesteps; extend the loop in
   `ert/bin/scripts/run_coupled.py` accordingly.

## Pitfalls (learned the hard way in `ert_fmu`)

- **`fm_dispatch.py` PATH trap**: launch ERT with the venv on `PATH`
  (`PATH=<venv>/bin:$PATH ert ...`) or every realization dies with
  `[Errno 2] No such file or directory: 'fm_dispatch.py'`.
- **Memory, not cores**: keep `QUEUE_OPTION LOCAL MAX_RUNNING` low
  (~240 MB per slot with real simulators, plus ERT's own ~370 MB).
- `ert lint` exits 0 regardless and misreports built-in jobs; use
  `ert test_run` as the parse/run gate.
- `GEN_KW` template placeholders must match the parameter name
  (`<Q0_MULT>` ↔ `Q0_MULT`).
- The `--` comment syntax in ERT config files means any job `ARGLIST` flag
  must be quoted.

## Files reference

| File | What it is |
|---|---|
| `coupling.json` | the coupling protocol — edit modes (`coupled`/`static`), wells, tolerance |
| `ert/bin/scripts/run_coupled.py` | per-realization driver: stage models → master → slaves → converge |
| `bin/eclipse_dummy.py` | licence-free stand-in for eclipse100/flow; reads `simspec.json` |
| `ert/model/01_coupled.ert` | ERT config (runpath layout, GEN_KW, INSTALL_JOB, FORWARD_MODEL) |
| `input/*/simspec.json` | machine-readable model specs for the dummy simulators |
| `input/static_profiles/gsatprod_model_hdn.inc` | the static GSATPROD table ("as today") |
