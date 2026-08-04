# Spike 001: real OPM Flow `model_n` round trip

**Status:** complete
**Verdict:** proceed to a restart-based adapter, with the limitations below kept explicit.

## Question

Can the coupling prototype pass network BHP constraints for `N-P1` and `N-P2`
into a real OPM Flow run and recover simulator-produced liquid rates in the
existing `slave_rates_model_n.csv` exchange schema?

## Acceptance scenario

**Given** one `network_constraints_model_n.csv`-compatible file containing one
finite, positive BHP for each `model_n` well in a selected year,

**when** `opm_model_n_adapter.py` renders the fixed deck, runs OPM Flow, and
extracts `WOPR`, `WWPR`, `WBHP`, and `FPR` with OPM's `summary` utility,

**then** it writes real Flow-derived rates using the existing slave-rate schema,
and raising both BHP constraints reduces both production rates.

## Run

Prerequisites:

- `flow` on `PATH`;
- OPM's `summary` CLI on `PATH`;
- Python 3.10 or newer; and
- an input CSV with at least `well`, `year`, and `p_bhp_bar` columns.

From the repository root:

```bash
python3 spikes/001-opm-model-n-roundtrip/opm_model_n_adapter.py \
  --constraints output/demo/realization-0/coupling/network_constraints_model_n.csv \
  --year 2024 \
  --output-dir output/opm-model-n-spike/current-2024
```

The output directory must be absent or empty. The adapter validates all input
constraints before creating it.

## Outputs

- `MODEL_N_FLOW.DATA`: rendered deck with the supplied BHP controls;
- `flow-run/MODEL_N_FLOW.SMSPEC` and `.UNSMRY`: real Flow summary output;
- `summary.txt`: values extracted with OPM `summary -r`;
- `slave_rates_model_n.csv`: exchange-compatible Flow-derived rates;
- `roundtrip_report.json`: constraints, results, simulator version, and paths;
- Flow stdout/stderr logs and normal Flow result files.

Generated runs belong below the repository's ignored `output/` tree.

## Verified evidence

The acceptance test executes two real Flow simulations:

- lower BHP constraints: `N-P1=300 bar`, `N-P2=320 bar`;
- higher BHP constraints: `N-P1=320 bar`, `N-P2=330 bar`.

Both well liquid rates decrease when the BHP constraints increase. The adapter
was also run against the standalone coupling demo's actual 2024 constraints
using **Flow 2025.10**:

| Well | Requested BHP, bar | Flow WBHP, bar | Flow liquid rate, sm³/d |
|---|---:|---:|---:|
| N-P1 | 301.327561 | 301.327576 | 458.079295 |
| N-P2 | 321.860831 | 321.860840 | 258.982813 |

The small requested/reported BHP differences are summary-output precision.

Run the focused regression with:

```bash
python3 -m unittest tests.test_opm_model_n_spike -v
```

The real-simulator test is skipped only when either `flow` or `summary` is not
available. Validation-only tests remain active.

## Scope and limitations

This is deliberately an isolated spike, not production coupling:

- The deck is a synthetic five-cell, two-well oil-water model initialized fresh
  for one one-day report step.
- It proves BHP input, real Flow execution, summary extraction, schema mapping,
  and backpressure sensitivity.
- It does **not** preserve reservoir state between annual coupling iterations.
- It does **not** implement restart-based fixed-point coupling.
- It does **not** replace `model_n` in the main Python/ERT driver yet.
- `model_hdn`, `master_network`, and the prescribed external source remain on
  the existing deterministic dummy path.
- Gas is inactive in this model, so `q_gas_sm3d` is correctly emitted as zero.
- The production-only probe rejects BHP at or above its 350 bar initial pressure.
- The original `.DATA`, network, and GSATPROD files under `input/` remain
  illustrative and simulator-unvalidated.

## Next experiment

Implement a stateful `model_n` backend that stages a runpath, runs a base case,
uses Flow restart files for subsequent annual constraint updates, and exposes a
backend switch to the existing coupling driver. Keep the present dummy backend
as the fast standalone and CI default.
