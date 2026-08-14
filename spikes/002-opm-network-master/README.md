# Spike 002: real OPM Flow network master

**Status:** complete
**Verdict:** VALIDATED on Flow **2026.04** — real wells AND GSATPROD
satellites load the trunk VFP; the Flow master can carry the full network
hydraulic load (was PARTIAL on 2025.10, where satellites registered in
group totals but did not load branch VFPs).

## Question

Given Flow is installed and the project master is currently a dummy, when we author a real Eclipse-compatible NETWORK + GSATPROD deck, then can OPM Flow simulate the shared subsea network that must receive both simulated slave rates and prescribed external satellite profiles?

## Why this matters

The coupled example currently uses `bin/eclipse_dummy.py` and an illustrative `network.inc` sketch. Spike 001 proved a real Flow slave for `model_n` (the repo's slave is `model_a` today). The next risk is the **master**: if Flow cannot honour a standard/extended network model plus GSATPROD, we either wait for an Eclipse licence or keep the dummy forever.

## Approach

Three independent probes, verified on Flow 2025.10 and **re-verified on 2026.04**:

| Probe | Deck | What it tests |
|-------|------|---------------|
| A | `TWO_WELL_NETWORK.DATA.tmpl` | Two real producers on `BRANPROP` trunk with VFP table 2. Does total simulated liquid rate load the trunk? |
| B | `MASTER_GSATPROD.DATA.tmpl` | One real producer + `GSATPROD` satellite group on the same trunk. Do satellite rates load the trunk VFP? |
| C | `GCONPROD_GSATPROD.DATA` | Field `GCONPROD` oil limit + `GSATPROD`. Do satellite rates count toward group control? |

Supporting evidence from earlier session probes (not re-run by the spike script):

- `GRUPNET` (Eclipse *standard* network) parses and converts internally to the extended network model.
- `NETWORK` / `BRANPROP` / `NODEPROP` (Eclipse *extended* network) runs.
- OPM parser recognizes `SLAVES`, `GRUPMAST`, `GRUPSLAV`, `RCMASTS`, `GSATINJE`, `NETBALAN`.
- Flow exposes `--slave=BOOLEAN` (native master/slave coupling is present in this build).

## How to run

```bash
cd /home/javier/projects/coupled-sim-eclipse
python3 spikes/002-opm-network-master/opm_network_master_spike.py \
  --output-dir output/spikes/network-master-$(date +%Y%m%d-%H%M%S)
```

Requires `flow` and `summary` on `PATH`.

## Results (Flow 2026.04, verified)

### A — real wells load the trunk VFP ✅

| Case | requested ORAT each | WOPR:M-P1 | WOPR:M-P2 | WTHP:M-P1 | GPR:MANIFOLD |
|------|---------------------|-----------|-----------|-----------|--------------|
| low  | 50 / 50             | 50.0      | 50.0      | 281.8     | **60.6**     |
| mid  | 200 / 200           | 200.0     | 200.0     | 251.1     | **145.3**    |
| high | 400 / 400           | 290.8     | 290.8     | 232.4     | **232.4**    |

Manifold pressure rises with total rate. At high rate the wells are cut back by network back-pressure (cannot deliver 400). **Network VFP is active.**

### B — GSATPROD **does** load the trunk VFP ✅ (2026.04; ❌ on 2025.10)

| Case   | GSATPROD oil | GOPR:SAT | GPR:MANIFOLD |
|--------|--------------|----------|--------------|
| sat000 | 0            | 0.0      | **87.8**     |
| sat130 | 130          | 130.0    | **123.9**    |
| sat600 | 600          | 600.0    | **266.6**    |

Satellite rates appear correctly in group totals (`GOPR:SAT`) **and now drive
the branch VFP**: manifold rises 87.8 → 123.9 → 266.6 bar for a 0→600 sm³/d
satellite swing with the well rate fixed. On 2025.10 the same probe gave
86.05 bar flat across all three cases.

### C — GCONPROD **does** see GSATPROD ✅

With well ORAT target 200, satellite oil 150, field ORAT limit 250:

```
WOPR:M-P1 = 100.0
GOPR:SAT  = 150.0
FOPR      = 250.0
```

The well is cut back so field total hits the group limit. Satellite rates participate in **group control**.

## Surprises / limitations found

1. **Oil/water-only decks abort** when a branch carries a VFP table:
   `Assertion 'rates.size() == 3' failed` in `WellGroupHelpers::computeNetworkPressures`.
   Workaround: always enable `GAS` + `DISGAS` (and consistent PVT/SGOF) for any deck that uses branch VFPs.
2. **Every well routed to the network needs a VFP table.** Missing VFP → `Nonexistent VFP table 0 referenced`.
3. **`BRANPROP` must precede `NODEPROP`** or Flow rejects the deck.
4. **`WELLDIMS` item 3** must cover all non-FIELD groups (PLAT + SAT + MANIFOLD = 3).
5. **`VFPPDIMS` axes** must match the VFP table dimensions exactly or the table is rejected as incomplete.
6. **Branch VFP tables need item 2 = 0.0 in the VFPPROD header** (well tables use 2000.0). Discovered in Spike 004: with 2000.0 on a branch table, satellite flow is not routed through it (Spike 004 probes on 2026.04: 20× satellite profile, manifold unchanged); with 0.0 the satellite load appears in the manifold.
7. The 2025.10→2026.04 behaviour change is real and verified: the 2026.04 release notes only mention GSATPROD summary accumulation, but the branch-VFP loading is what actually closes the loop for this architecture.

## Verdict: VALIDATED (2026.04)

### What worked
- Flow simulates Eclipse standard (`GRUPNET`) and extended (`NETWORK`/`BRANPROP`/`NODEPROP`) network models.
- Real simulated wells load trunk VFP and experience back-pressure cutback.
- `GSATPROD` is parsed, contributes to group totals, **loads branch VFPs on 2026.04**, and participates in `GCONPROD` limits.
- Coupling keywords (`SLAVES`/`GRUPMAST`/`GRUPSLAV`/`RCMASTS`) are recognized by the OPM parser; `--slave` flag exists.
- All of the above uses Eclipse-compatible keyword syntax, so decks prepared now remain runnable under an Eclipse licence later.

### Recommendation and current repository use

This spike establishes Flow's keyword/runtime behavior for legacy prescribed
sources. The active repository topology has since changed:

1. **Simulated slaves** use real restart-based Flow adapters (Spike 003).
2. **Primary network hydraulics** use the no-profile Flow master from Spike 004;
   current Model A and Model B rates load the network directly.
3. `GSATPROD` support remains useful for genuine external sources and the
   explicit `configs/coupling.legacy-gsatprod.json` regression fixture, but it
   is not the repository default and does not represent Model B in the primary
   two-way workflow.
4. The Python analytic solver remains a fast dummy-test fallback, not the
   active all-real default.
5. If an Eclipse licence is used later, keep the same bidirectional exchange
   contract or move to native reservoir coupling; do not silently reintroduce
   Model B as a one-way prescribed profile.

## Files

```text
spikes/002-opm-network-master/
├── README.md
├── opm_network_master_spike.py   # sensitivity runner + JSON report
├── TWO_WELL_NETWORK.DATA.tmpl    # probe A
├── MASTER_GSATPROD.DATA.tmpl     # probe B
├── GCONPROD_GSATPROD.DATA        # probe C
└── vfp_tables.inc                # well VFP #1 + trunk VFP #2
```
