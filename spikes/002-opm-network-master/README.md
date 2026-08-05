# Spike 002: real OPM Flow network master

**Status:** complete
**Verdict:** PARTIAL — proceed with an external network solver for prescribed satellite load, keep OPM Flow for the reservoir + well-VFP network of simulated wells.

## Question

Given Flow 2025.10 is installed and the project master is currently a dummy, when we author a real Eclipse-compatible NETWORK + GSATPROD deck, then can OPM Flow simulate the shared subsea network that must receive both simulated slave rates and prescribed external satellite profiles?

## Why this matters

The coupled example currently uses `bin/eclipse_dummy.py` and an illustrative `network.inc` sketch. Spike 001 proved a real Flow slave for `model_n`. The next risk is the **master**: if Flow cannot honour a standard/extended network model plus GSATPROD, we either wait for an Eclipse licence or keep the dummy forever.

## Approach

Three independent probes, all run on Flow 2025.10:

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

## Results (Flow 2025.10, verified)

### A — real wells load the trunk VFP ✅

| Case | requested ORAT each | WOPR:M-P1 | WOPR:M-P2 | WTHP:M-P1 | GPR:MANIFOLD |
|------|---------------------|-----------|-----------|-----------|--------------|
| low  | 50 / 50             | 50.0      | 50.0      | 281.8     | **60.6**     |
| mid  | 200 / 200           | 200.0     | 200.0     | 251.1     | **145.3**    |
| high | 400 / 400           | 290.8     | 290.8     | 232.4     | **232.4**    |

Manifold pressure rises with total rate. At high rate the wells are cut back by network back-pressure (cannot deliver 400). **Network VFP is active.**

### B — GSATPROD does **not** load the trunk VFP ❌

| Case   | GSATPROD oil | GOPR:SAT | WTHP:M-P1 | GPR:MANIFOLD |
|--------|--------------|----------|-----------|--------------|
| sat000 | 0            | 0.0      | 249.6     | **86.05**    |
| sat130 | 130          | 130.0    | 249.6     | **86.05**    |
| sat600 | 600          | 600.0    | 249.6     | **86.05**    |

Satellite rates appear correctly in group totals (`GOPR:SAT`), but manifold pressure is **identical** across a 0→600 sm³/d satellite swing. **GSATPROD does not contribute flow to branch VFP evaluation in Flow 2025.10.**

### C — GCONPROD **does** see GSATPROD ✅

With well ORAT target 200, satellite oil 150, field ORAT limit 250:

```
WOPR:M-P1 = 100.0
GOPR:SAT  = 150.0
FOPR      = 250.0
```

The well is cut back so field total hits the group limit. Satellite rates participate in **group control**, just not in **network hydraulic load**.

## Surprises / limitations found

1. **Oil/water-only decks abort** when a branch carries a VFP table:
   `Assertion 'rates.size() == 3' failed` in `WellGroupHelpers::computeNetworkPressures`.
   Workaround: always enable `GAS` + `DISGAS` (and consistent PVT/SGOF) for any deck that uses branch VFPs.
2. **Every well routed to the network needs a VFP table.** Missing VFP → `Nonexistent VFP table 0 referenced`.
3. **`BRANPROP` must precede `NODEPROP`** or Flow rejects the deck.
4. **`WELLDIMS` item 3** must cover all non-FIELD groups (PLAT + SAT + MANIFOLD = 3).
5. **`VFPPDIMS` axes** must match the VFP table dimensions exactly or the table is rejected as incomplete.
6. OPM 2026.04 is available on the PPA (`apt-cache` candidate) but this spike is verified on installed **2025.10**. Re-check GSATPROD↔network behaviour after upgrading before relying on it.

## Verdict: PARTIAL

### What worked
- Flow simulates Eclipse standard (`GRUPNET`) and extended (`NETWORK`/`BRANPROP`/`NODEPROP`) network models.
- Real simulated wells load trunk VFP and experience back-pressure cutback.
- `GSATPROD` is parsed, contributes to group totals, and participates in `GCONPROD` limits.
- Coupling keywords (`SLAVES`/`GRUPMAST`/`GRUPSLAV`/`RCMASTS`) are recognized by the OPM parser; `--slave` flag exists.
- All of the above uses Eclipse-compatible keyword syntax, so decks prepared now remain runnable under an Eclipse licence later.

### What didn't
- **GSATPROD satellite rates do not load network branch VFPs** in Flow 2025.10. A master that relies on prescribed external wells contributing hydraulic load to a shared riser/trunk **cannot** be a pure Flow deck today.
- Native multi-reservoir coupling (`SLAVES` runtime behaviour) was not end-to-end exercised; only parser recognition + CLI flag presence.

### Recommendation for the real build

Keep the current file-exchange coupling architecture. Promote pieces as follows:

1. **Simulated slaves** → real Flow adapters (Spike 001 path), with restart state in a later spike.
2. **Master network hydraulics for simulated + prescribed rates** → keep (or harden) the external network solver currently in `eclipse_dummy.py` / `simspec.json`. Do **not** assume a pure Flow NETWORK deck can replace it while external wells remain GSATPROD satellites.
3. **Optional Flow master** only for the *reservoir* side of the master (dummy well M-P1) and any well-level VFP that does not need satellite hydraulic contribution.
4. When the Eclipse licence arrives, the same GRUPNET/NETWORK/GSATPROD decks are the production path; the external solver becomes a fallback.
5. After upgrading to Flow 2026.04, re-run this spike before changing the architecture — the GSATPROD↔network gap may close.

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
