# Spike 004: real OPM Flow network master

**Status:** complete
**Verdict:** VALIDATED — a Flow 2025.10 NETWORK deck serves as the coupled
master: four real slave wells load the shared trunk, GSATPROD satellites
register in group totals, per-iteration rate inputs render into WCONPROD,
and per-well BHP constraints are extracted back to the slaves. The all-real
realization (Flow master + Flow model_n + Flow model_hdn) converges in 12
iterations.

## Question

Can a Flow NETWORK deck replace the dummy master's pressure calculation in
the ERT coupling loop — reading both slaves' rates and the prescribed
GSATPROD profile, solving the shared subsea network with real well VFPs, and
writing `network_constraints_<slave>.csv` in the existing exchange schema?

Spike 002 proved the building blocks (real wells load trunk VFPs; GSATPROD
registers in group totals but does NOT load branch VFPs on 2025.10). This
spike assembles them into a working master backend.

## Approach

`opm_flow_master_adapter.py` per coupling iteration:

1. reads `slave_rates_model_n.csv` + `slave_rates_model_hdn.csv` (initial
   guesses on iteration 1) and parses the prescribed GSATPROD profile;
2. renders `MASTER_FLOW.DATA` — 4 real wells (N-P1/N-P2/H-P1/H-P2 under
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

## Mechanisms discovered on Flow 2025.10 (all empirical)

1. **GSATPROD is a block keyword**: the record needs a closing `/` line
   after the data row, like WELSPECS — otherwise Flow consumes the next
   keyword (`Cannot create UDA value from string 'PLAT'`).
2. **GSATPROD record layout**: `'GROUP' OIL WATER GAS /` (four fields —
   omitting the water rate breaks parsing).
3. **WELLDIMS item 4** must cover the well children of the largest group
   (PLAT with 4 wells → `4 1 4 4`).
4. **The branch VFP value IS the upstream node pressure.** With the trunk
   table values all set to 1.0, the manifold read exactly 1.0 bar. The
   downstream axis indexes the operating column; the rate axis is not
   consulted as naively expected (values written flat across the rate axis
   pinned the manifold to one value regardless of total rate). The practical
   calibration: values gently rising with the downstream axis
   (`base + 0.2 × downstream`) give a mild, monotone manifold response
   (26.9 → 35.8 bar across 474 → 4740 sm³/d totals) that keeps every
   constraint safely below both slave pressure caps.
5. **With a high-productivity master reservoir the well BHP is
   reservoir-dominated** (WBHP ≈ p_res − drawdown, identical across well
   VFP tables); the well VFP tables only matter through the node-pressure
   solve. The slave constraints therefore use the network node pressure +
   hydrostatic, not WBHP (which the report still records).
6. **The master's own reservoir must be large enough** to sustain three
   years at Flow-scale rates: at 2000×2000×200 m cells the master tank
   depleted ~46 bar by 2026 and the low-rate wells shut in (delivered 0,
   flagged as cutbacks). 4000×4000×400 m cells (~3 bar depletion) keep all
   wells delivering — zero cutbacks.
7. **All-real convergence needs a touch more damping**: the year-2026
   sub-map of the all-real system is marginally expanding at the default
   relaxation 0.6 (residual plateaued at ~8%); with relaxation 0.4 and the
   steeper trunk response the residual contracts monotonically
   (455% → 0.48%).

## Run

```bash
python3 spikes/004-opm-flow-master/opm_flow_master_adapter.py \
  --rates-model-n output/demo/realization-0/coupling/slave_rates_model_n.csv \
  --rates-model-hdn output/demo/realization-0/coupling/slave_rates_model_hdn.csv \
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
├── network_constraints_model_n.csv
├── network_constraints_model_hdn.csv
└── master_report.json        # requested/delivered rates, constraints, checks
```

## Verified evidence

Standalone probes (2024 row) at three rate levels — manifold responds
monotonically, all BHPs stay below the slave caps (350 model_n / 315
model_hdn):

| Case | Simulated total sm³/d | Manifold bar | N-P1 BHP bar | H-P1 BHP bar | Delivered |
|---|---:|---:|---:|---:|---|
| low | 474 | 26.9 | 285.4 | 268.8 | yes |
| mid | 1580 | 30.2 | 288.7 | 272.0 | yes |
| high | 4740 | 35.8 | 294.3 | 277.6 | no (network cutback) |

Full all-real realization (Flow master + Flow model_n + Flow model_hdn,
relaxation 0.4, max_iterations 20, Q0_MULT=1.0): **converged in 12 of 20
iterations** (tolerance 0.005), residual 455% → 0.48%, zero master cutbacks.

| Iteration | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Residual | 455% | 77% | 33% | 22% | 12% | 8.1% | 5.3% | 3.4% | 2.1% | 1.3% | 0.79% | 0.48% |

Final slave rates (sm³/d) decline with depletion; final master constraints
(2024) honour the network: N-P1 BHP 306.5, N-P2 ~326, H-P1 289.8, H-P2
~302 bar — all below the 350/315 bar slave caps.

## Scope and limitations

- GSATPROD satellites still do **not** load the trunk VFP on Flow 2025.10
  (Spike 002) — the master's pressures carry the simulated wells' hydraulic
  load only; for the demo's ~200 sm³/d satellites the missing trunk term is
  small, but large prescribed profiles still need the external solver.
- The slave constraints are network node pressure + wellbore hydrostatic
  (TVD from `input/master_network/simspec.json`), not Flow's WBHP.
- The master deck is synthetic (single 5-cell tank, calibrated VFP tables);
  production decks under a future Eclipse licence replace the tables with
  real lift curves.

## Next experiment

None required for the licence-free path: the all-real hybrid
(master + both slaves on Flow, prescribed GSATPROD source) is the complete
demonstration. When the Eclipse licence arrives (or Flow ≥ 2026.04 closes
the GSATPROD↔network gap), re-run Spike 002/004 and swap the synthetic VFP
tables for the real master's lift curves.
