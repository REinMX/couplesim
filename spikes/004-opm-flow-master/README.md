# Spike 004: real OPM Flow network master

**Status:** complete
**Verdict:** VALIDATED — a Flow 2026.04 NETWORK deck serves as the coupled
master: four real slave wells load the shared trunk, GSATPROD satellites
register in group totals **and load the trunk VFP on 2026.04** (Spike 002
flips PARTIAL → VALIDATED), per-iteration rate inputs render into WCONPROD,
and per-well BHP constraints are extracted back to the slaves. The all-real
realization (Flow master + Flow model_a + Flow model_b) converges in 13
iterations.

## Question

Can a Flow NETWORK deck replace the dummy master's pressure calculation in
the ERT coupling loop — reading both slaves' rates and the prescribed
GSATPROD profile, solving the shared subsea network with real well VFPs, and
writing `network_constraints_<slave>.csv` in the existing exchange schema?

Spike 002 proved the building blocks (real wells load trunk VFPs; GSATPROD
registers in group totals; on 2025.10 satellites did NOT load branch VFPs —
on 2026.04 they do, Spike 002 VALIDATED). This spike assembles them into a
working master backend.

## Approach

`opm_flow_master_adapter.py` per coupling iteration:

1. reads `slave_rates_model_a.csv` + `slave_rates_model_b.csv` (initial
   guesses on iteration 1) and parses the prescribed GSATPROD profile;
2. renders `MASTER_FLOW.DATA` — 4 real wells (A-P1/A-P2/B-P1/B-P2 under
   PLAT), GSATPROD group SAT on MANIFOLD, trunk MANIFOLD→FIELD with VFP,
   per-year WCONPROD rates + GSATPROD records, Jan-1→Jan-1 leap-aware
   TSTEP;
3. runs Flow; extracts per-year `WOPR/WWPR/WBHP/WTHP` per well plus
   `GOPR:SAT`, `GPR:PLAT`, `GPR:MANIFOLD` via `summary -r`;
4. writes `network_constraints_<slave>.csv` with
   `p_bhp = GPR:PLAT + ρ·g·TVD` (network node pressure + wellbore
   hydrostatic, exactly the dummy's formula with Flow's network solve
   replacing the analytic friction) and `p_wh = GPR:PLAT`.

The `gen_vfp_tables.py` generator produces the three VFP tables (N wells,
H wells, trunk) from compact parameters.

## Mechanisms discovered on Flow 2026.04 (all empirical)

1. **GSATPROD is a block keyword**: the record needs a closing `/` line
   after the data row, like WELSPECS — otherwise Flow consumes the next
   keyword (`Cannot create UDA value from string 'PLAT'`).
2. **GSATPROD record layout**: `'GROUP' OIL WATER GAS /` (four fields —
   omitting the water rate breaks parsing).
3. **WELLDIMS item 4** must cover the well children of the largest group
   (PLAT with 4 wells → `4 1 4 4`).
4. **Branch VFP tables need item 2 = 0.0 in the VFPPROD header** (well
   tables use 2000.0). With 2000.0 on a branch table, GSATPROD satellite
   flow is not routed through it on 2026.04 (20× satellite profile:
   manifold unchanged to 6 decimals); with 0.0 the satellite load appears
   in the manifold (30.6 → 47.0 bar for a 20× satellite at fixed well
   rates).
5. **The branch VFP value IS the upstream node pressure**, and the rate
   axis is not consulted as naively expected: the solve indexes the
   operating column on the downstream axis, which rises with the total
   rate (including satellites on 2026.04). Practical calibration: values
   gently rising with the downstream axis (`base + 0.2 × downstream`,
   flat across rate rows) give a mild, monotone manifold response
   (30.6 → 47.3 bar across 474 → 4740 sm³/d simulated totals) that keeps
   every constraint safely below both slave pressure caps. Values flat
   across the downstream axis pin the manifold to a single value
   regardless of rate.
6. **With a high-productivity master reservoir the well BHP is
   reservoir-dominated** (WBHP ≈ p_res − drawdown, identical across well
   VFP tables); the well VFP tables only matter through the node-pressure
   solve. The slave constraints therefore use the network node pressure +
   hydrostatic, not WBHP (which the report still records).
7. **The master's own reservoir must be large enough** to sustain three
   years at Flow-scale rates: at 2000×2000×200 m cells the master tank
   depleted ~46 bar by 2026 and the low-rate wells shut in (delivered 0,
   flagged as cutbacks). 4000×4000×400 m cells (~3 bar depletion) keep all
   wells delivering — zero cutbacks.
8. **All-real convergence needs a touch more damping**: the year-2026
   sub-map of the all-real system is marginally expanding at the default
   relaxation 0.6 (residual plateaued at ~8%); with relaxation 0.4 and the
   calibrated trunk response the residual contracts monotonically
   (455% → 0.48% on 2025.10, 0.31% on 2026.04).

## Run

```bash
python3 spikes/004-opm-flow-master/opm_flow_master_adapter.py \
  --rates model_a output/demo/realization-0/coupling/slave_rates_model_a.csv \
  --rates model_b output/demo/realization-0/coupling/slave_rates_model_b.csv \
  --profile input/master_network/profiles/gsatprod_external.inc \
  --output-dir output/flow-master-spike
```

Requires `flow` and `summary` on PATH. The output directory must be absent
or empty.

## Outputs

```text
<output-dir>/
├── MASTER_FLOW.DATA          # rendered 4-well network deck
├── vfp_tables_master.inc
├── flow-run/                 # SMSPEC, UNSMRY, PRT, ...
├── summary.txt               # raw summary -r output
├── network_constraints_model_a.csv
├── network_constraints_model_b.csv
└── master_report.json        # requested/delivered rates, constraints, checks
```

## Verified evidence (Flow 2026.04)

Standalone probes (2024 row) at three rate levels — manifold responds
monotonically, all BHPs stay below the slave caps (350 model_a / 315
model_b):

| Case | Simulated total sm³/d | Manifold bar | A-P1 BHP bar | B-P1 BHP bar | Delivered |
|---|---:|---:|---:|---:|---|
| low | 474 | 30.6 | 289.2 | 272.4 | yes |
| high | 4740 | 47.3 | 305.9 | 289.1 | yes |
| high + 20× satellite | 9140 | 60.7 | 319.2 | 302.4 | yes |

The satellite load now contributes to the manifold on 2026.04: at fixed
low well rates, a 20× prescribed profile (220 → 4400 sm³/d) moves the
manifold 30.6 → 47.0 bar (Spike 002 flips PARTIAL → VALIDATED).

Full all-real realization (Flow master + Flow model_a + Flow model_b,
relaxation 0.4, max_iterations 20, Q0_MULT=1.0): **converged in 13 of 20
iterations** (tolerance 0.005), residual 455% → 0.31%, zero master cutbacks.

| Iteration | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Residual | 455% | 77% | 33% | 22% | 12% | 8.1% | 5.3% | 3.4% | 2.1% | 1.3% | 0.81% | 0.50% | 0.31% |

Final slave rates (sm³/d) decline with depletion; final master constraints
(2024) honour the network: A-P1 BHP 306.9, A-P2 303.6, B-P1 290.2, B-P2
287.0 bar — all below the 350/315 bar slave caps.

## Scope and limitations

- GSATPROD satellites **do** load the trunk VFP on Flow 2026.04 (verified
  in this spike and Spike 002) — the master's pressures carry the full
  hydraulic load, simulated + prescribed. The branch VFP header needs
  item 2 = 0.0 for satellite flow to be routed (see Mechanisms, item 4).
- The slave constraints are network node pressure + wellbore hydrostatic
  (TVD from `input/master_network/simspec.json`), not Flow's WBHP.
- The master deck is synthetic (single 5-cell tank, calibrated VFP tables);
  production decks under a future Eclipse licence replace the tables with
  real lift curves.

## Next experiment

None required for the licence-free path: the all-real hybrid
(master + both slaves on Flow, prescribed GSATPROD source now carrying
hydraulic load) is the complete demonstration on Flow ≥ 2026.04. When the
Eclipse licence arrives, swap the synthetic VFP tables for the real
master's lift curves.
