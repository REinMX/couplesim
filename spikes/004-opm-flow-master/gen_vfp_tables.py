#!/usr/bin/env python3
"""Generate the Spike 004 master VFP tables.

Three VFPPROD tables calibrated for the coupled demo's flow scale:

- Table 1: model_n wells (N-P1/N-P2). BHP ~322-380 bar depending on THP,
  mildly declining with rate; used by the model_n slave wells.
- Table 2: model_hdn wells (H-P1/H-P2). Identical to table 1: with the
  node-pressure + hydrostatic constraint extraction the well curve only
  shapes the network node solve, and both models share one PLAT node, so
  separate curves are unnecessary. Kept as a separate table so a future
  deck can differentiate the models.
- Table 3: trunk MANIFOLD->FIELD. Values ARE the upstream node pressure
  (empirically the branch VFP value); written flat across the rate axis
  and gently rising with the downstream axis so the manifold responds
  mildly to total rate while keeping node + hydrostatic below the slave
  BHP caps (350 model_n / 315 model_hdn).

Layout mirrors the validated Spike 002 vfp_tables.inc (axis order and
row shape); values were calibrated against Flow probe runs (2025.10 first,
re-verified and re-tuned on 2026.04, where satellites also load branch
VFPs and the trunk header needs item 2 = 0.0).
"""

from __future__ import annotations

import argparse
from pathlib import Path


def fmt_table(
    table_number: int,
    first_axis: list[float],
    second_axis: list[float],
    wct_axis: list[float],
    gor_axis: list[float],
    values: dict[tuple[int, int, int], list[float]],
    header_item2: float = 2000.0,
) -> str:
    """values[(thp_index_1based, wct_index_1based, gor_index_1based)] = 5 values.

    header_item2: VFPPROD record-1 item 2. Wells use 2000.0 (Spike 002);
    the validated 002 trunk table uses 0.0, and Flow 2026.04 only loads
    satellite flow on branches whose table has 0.0 here.
    """
    lines = [
        f"VFPPROD",
        f"  {table_number} {header_item2:.1f} 'LIQ' 'WCT' 'GOR' 'THP' ' ' 'METRIC' 'BHP' /",
        "  " + " ".join(f"{value:.1f}" for value in first_axis) + " /",
        "  " + " ".join(f"{value:.1f}" for value in second_axis) + " /",
        "  " + " ".join(f"{value:.1f}" for value in wct_axis) + " /",
        "  " + " ".join(f"{value:.1f}" for value in gor_axis) + " /",
        "  0.0 /",
    ]
    for thp_index in range(1, len(first_axis) + 1):
        for wct_index in range(1, len(wct_axis) + 1):
            for gor_index in range(1, len(gor_axis) + 1):
                row_values = values[(thp_index, wct_index, gor_index)]
                lines.append(
                    "  "
                    + " ".join(
                        str(index)
                        for index in (thp_index, wct_index, gor_index, 1)
                    )
                    + " "
                    + " ".join(f"{value:.1f}" for value in row_values)
                    + " /"
                )
    return "\n".join(lines) + "\n"


def well_table(
    table_number: int,
    base_bhp_at_rate: list[float],
    thp_axis: list[float],
    rate_axis: list[float],
    thp_step: float = 10.0,
) -> str:
    """Well lift table: BHP grows with THP and falls with liquid rate."""
    wct_axis = [0.0, 0.5]
    gor_axis = [0.0, 300.0]
    values: dict[tuple[int, int, int], list[float]] = {}
    for thp_index in range(1, len(thp_axis) + 1):
        for wct_index in range(1, 3):
            for gor_index in range(1, 3):
                values[(thp_index, wct_index, gor_index)] = [
                    base_bhp_at_rate[rate_index - 1]
                    + thp_step * (thp_index - 1)
                    + 5.0 * (wct_index - 1)
                    - 3.0 * (gor_index - 1)
                    for rate_index in range(1, len(rate_axis) + 1)
                ]
    return fmt_table(table_number, thp_axis, rate_axis, wct_axis, gor_axis, values)


def trunk_table(
    table_number: int,
    rate_axis: list[float],
    downstream_axis: list[float],
    base_upstream: float,
    upstream_per_downstream: float,
    header_item2: float = 0.0,
) -> str:
    """Trunk branch table: upstream node pressure by branch liquid rate.

    The header's second item must be 0.0 (not 2000.0 like the well tables):
    the validated Spike 002 trunk uses 0.0, and Flow 2026.04 only routes
    GSATPROD satellite flow through branch tables with 0.0 there.

    The branch VFP rate axis is still not consulted directly: the solve
    indexes the operating column on the downstream axis, which rises with
    the total rate (and now includes the satellite flow on 2026.04). Values
    are therefore written gently rising with the downstream axis
    (base + per_downstream x downstream) and flat across the rate rows, so
    the manifold responds mildly to the total load while keeping node
    pressure + wellbore hydrostatic below the slave BHP caps (350 model_n
    / 315 model_hdn).
    """
    wct_axis = [0.0, 0.5]
    gor_axis = [0.0, 300.0]
    values: dict[tuple[int, int, int], list[float]] = {}
    for rate_index in range(1, len(rate_axis) + 1):
        for wct_index in range(1, 3):
            for gor_index in range(1, 3):
                values[(rate_index, wct_index, gor_index)] = [
                    base_upstream + upstream_per_downstream * downstream
                    + 1.0 * (wct_index - 1)
                    for downstream in downstream_axis
                ]
    return fmt_table(
        table_number,
        rate_axis,
        downstream_axis,
        wct_axis,
        gor_axis,
        values,
        header_item2=header_item2,
    )


def generate() -> str:
    parts = [
        "-- Spike 004 master VFP tables (generated by gen_vfp_tables.py).",
        "-- Table 1: model_n wells. Table 2: model_hdn wells. Table 3: trunk.",
        "-- Well tables: BHP vs THP x liquid rate (falling with rate, rising with THP).",
        "-- Trunk table: upstream node pressure vs downstream x branch liquid rate.",
    ]
    parts.append(
        well_table(
            1,
            base_bhp_at_rate=[345.0, 340.0, 335.0, 330.0, 322.0],
            thp_axis=[10.0, 50.0, 100.0, 200.0, 400.0],
            rate_axis=[100.0, 500.0, 1000.0, 2000.0, 4000.0],
            thp_step=30.0,
        )
    )
    parts.append(
        well_table(
            2,
            base_bhp_at_rate=[345.0, 340.0, 335.0, 330.0, 322.0],
            thp_axis=[10.0, 50.0, 100.0, 200.0, 400.0],
            rate_axis=[100.0, 500.0, 1000.0, 2000.0, 4000.0],
            thp_step=30.0,
        )
    )
    parts.append(
        trunk_table(
            3,
            rate_axis=[100.0, 500.0, 1000.0, 3000.0, 8000.0],
            downstream_axis=[10.0, 20.0, 40.0, 80.0, 160.0],
            base_upstream=25.0,
            upstream_per_downstream=0.2,
            header_item2=0.0,
        )
    )
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("vfp_tables_master.inc"))
    args = parser.parse_args()
    args.output.write_text(generate(), encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
