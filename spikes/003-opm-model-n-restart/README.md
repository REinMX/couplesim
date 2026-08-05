# Spike 003: stateful restart-based `model_n` backend

**Status:** complete
**Verdict:** VALIDATED — Flow 2025.10 continues a slave run across annual
coupling years through the Eclipse `RESTART` keyword, honours per-year BHP
constraints, and emits per-year rates in the existing exchange schema. The
restart chain is verifiably stateful (cumulative production is carried, not
re-initialized).

## Question

Can a stateful slave backend stage one runpath, run a fresh base case for
2024, continue 2025 and 2026 from Flow restart files, apply each year's BHP
constraints, and write one `slave_rates_<model>.csv` with all three years in
the coupling exchange schema?

This is the "stateful, restart-based backend" that Spikes 001 and 002 both
declared as the next integration step for slaves. The adapter is parameterized
per slave model (`--model model_n|model_hdn`; well names, initial pressure,
and deck stems differ), so one validated implementation serves both coupled
slaves.

## Approach

`opm_model_n_restart_adapter.py` runs one Flow simulation per year:

| Year | Deck | Initialization | Restart source |
|---|---|---|---|
| 2024 | `MODEL_N_2024.DATA` (base) | `EQUIL` | — |
| 2025 | `MODEL_N_2025.DATA` (continuation) | `RESTART 'MODEL_N_2024' 1 /` | `MODEL_N_2024.UNRST` + `.EGRID` staged beside the deck |
| 2026 | `MODEL_N_2026.DATA` (continuation) | `RESTART 'MODEL_N_2025' 2 /` | `MODEL_N_2025.UNRST` + `.EGRID` |

Year-end `WOPR`, `WWPR`, `WBHP`, `FPR`, `FOPT`, `FWPT` are extracted with
OPM's `summary -r` CLI and emitted as `slave_rates_model_n.csv` rows plus a
`restart_report.json` with automatic scientific checks.

## Mechanisms verified on Flow 2025.10

1. **Restart is a plain deck keyword.** `RESTART 'CASE' STEP /` in SOLUTION
   loads the unified restart file; Flow prints
   `This is a restarted run - skipping until report step N` and re-adds wells
   and groups from the restart file (`Adding well N-P1 from restart file`).
   The previous year's `.UNRST` **and** `.EGRID` must be copied next to the
   continuation deck.
2. **The continuation `START` must equal the restart-state date.** Restarting
   from a base run whose last report step is 31-DEC-2024 into a deck starting
   1-JAN-2025 aborts with
   `Report step 1 has start time after end time ... Possibly due to
   inconsistent RESTART/SKIPREST settings`. The clean fix is a **Jan-1 to
   Jan-1 year convention**: each year's `TSTEP` is the leap-aware day count
   (366 for 2024, 365 for 2025/2026), so every restart state sits exactly on
   1-Jan and matches the exchange contract's calendar years.
3. **Flow numbers report steps cumulatively across the chain.** The base run
   writes step 1, the first continuation writes step 2, and so on
   (continuation `.UNRST` contains only the new step). The RESTART step for
   chain position `index` is therefore `index` — hard-coding 1 breaks the
   second continuation with `Report step 1 not found in restart file`.
4. **`summary -r` skips the restart-state row** (it reports only completed
   report steps), so the carried state cannot be read back from the
   continuation summary. Statefulness is instead proven by a fresh-run
   comparison (below).
5. **The Spike-001 toy grid depletes to BHP within weeks** (FPR 350 → 301 bar
   in one year; the 321.9-bar N-P2 constraint then exceeds reservoir pressure
   and the well shuts in). The restart grid is scaled to 2000×2000×200 m
   cells so 3 years of BHP-limited production stays alive — deliberate spike
   scale, not reservoir realism.

## Run

```bash
python3 spikes/003-opm-model-n-restart/opm_model_n_restart_adapter.py \
  --constraints output/demo/realization-0/coupling/network_constraints_model_n.csv \
  --output-dir output/opm-model-n-restart-spike \
  --model model_n
```

`--model model_hdn` runs the same chain for the hdn slave (H-P1/H-P2, 315 bar
initial). Requires `flow` and `summary` on PATH. The output directory must be
absent or empty. The full chain runs in about 3 seconds on this 5-cell model.

## Outputs

```text
<output-dir>/
├── slave_rates_model_n.csv      # 3 years x 2 wells, exchange schema
├── restart_report.json          # constraints, per-year results, checks
└── year-2024|2025|2026/
    ├── MODEL_N_<year>.DATA      # rendered base or continuation deck
    ├── MODEL_N_<prev>.UNRST/.EGRID  # staged restart artifacts (cont. years)
    ├── summary.txt              # raw summary -r output
    ├── flow.stdout.log / flow.stderr.log
    └── flow-run/                # SMSPEC, UNSMRY, UNRST, EGRID, PRT
```

## Verified evidence

Chain run against the standalone demo's actual 2024–2026 network constraints
(`output/demo/realization-0/coupling/network_constraints_model_n.csv`),
Flow 2025.10:

| Year | FOPT sm³ | FWPT sm³ | FPR bar | N-P1 q_liq sm³/d (req BHP) | N-P2 q_liq sm³/d (req BHP) |
|---|---:|---:|---:|---:|---:|
| 2024 | 1,674,560 | 80.05 | 341.56 | 2482.4 (301.3276) | 1240.1 (321.8608) |
| 2025 | 2,590,557 | 169.53 | 333.55 | 1828.9 (300.9489) | 680.9 (321.5158) |
| 2026 | 3,214,907 | 243.39 | 328.08 | 1408.1 (300.5861) | 302.6 (321.1856) |

All report checks pass: FOPT strictly increasing, FWPT non-decreasing, WBHP
matches every requested constraint within 1e-3 bar, all rates positive.

**Statefulness (the core question):** a fresh 2025-only run (same BHPs, fresh
`EQUIL`) produces FOPT = 1,687,144 sm³ — one year of production. The
restarted 2025 run produces FOPT = 2,590,557 sm³, carrying 2024's cumulative
(1,674,560 sm³) plus a depleted-reservoir 2025 increment (916k sm³ vs the
fresh run's 1,687k sm³, consistent with continuation from 341.6 bar instead
of 350 bar). A silently re-initialized chain would have matched the fresh
run's ~1.0× FOPT(2024); the chain shows 1.55×. The integration test asserts
`FOPT(2025) > 1.3 × FOPT(2024)` and
`FOPT(2025) > fresh_2025 + 0.5 × FOPT(2024)`.

Regression: `python3 -m unittest discover -s tests -p 'test_opm_model_n_restart_spike.py' -v`
(validation tests always run; the real-simulator test skips without flow).

## Scope and limitations

- Synthetic 5-cell oil-water model; gas inactive (`q_gas_sm3d` = 0).
- Grid scaled up to keep the 3-year BHP demonstration alive; production
  physics is not field-calibrated.
- The adapter is standalone; it does not yet replace the dummy backend inside
  `ert/bin/scripts/run_coupled.py`.
- The dummy master still provides the BHP constraints; the restart backend
  only consumes them.
- Restart files are staged by copying `.UNRST`/`.EGRID`; a production adapter
  would manage this under the ERT runpath.

## Next experiment

Done — fully wired into the ERT driver. The adapter is selectable as the flow
slave backend for **both** `model_n` and `model_hdn`
(`slaves.<name>.backend = "flow"` in `coupling.json`, or
`--backend-model-n flow` on the CLI for model_n). The driver applies the
coupling relaxation to each raw Flow response, and the all-real realization
(both slaves on Flow, dummy master) converges in 7 iterations on the demo
config; the master network's friction was recalibrated to Flow-scale rates
(see the main README "Hybrid mode" section). Remaining integration step:
replace the dummy master when an Eclipse licence (or Flow ≥ 2026.04 with
GSATPROD network closure) is available.
