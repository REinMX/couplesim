#!/usr/bin/env python3
"""Dummy Eclipse/Flow stand-in for the coupled multi-model example.

Plays the role of the reservoir simulator (eclipse100 / flow) for the
three models of the coupled setup:

  master_network -- "dummy reservoir with the network model". Reads the
                    rate demands of the coupled slave wells (from their
                    simulation results), solves a simple subsea network
                    (manifold pressure + riser friction + hydrostatics)
                    and writes GSATPROD production-profile tables for each
                    coupled slave.
  model_n        -- coupled slave. Consumes the GSATPROD table written by
                    master_network, applies a linear inflow-performance
                    (IPR) constraint and reports back the actual rates.
  model_hdn      -- coupled slave (same mechanism as model_n). A static
                    mode (old GSATPROD file, no network feedback) is kept
                    for any model via mode=static in coupling.json.

The dummy does NOT parse the .DATA decks. Each model's machine-readable
specification lives in simspec.json inside its staging folder; the .DATA
files sit next to it so the example mirrors what a real run would consume.
Swap this script for the real simulator (or `flow`) in
ert/bin/scripts/run_coupled.py and the same GSATPROD include files drive
the real decks.

Usage:
  python3 eclipse_dummy.py <model> <model_dir> <iteration>
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

GRAVITY = 9.81  # m/s2


def log(msg: str) -> None:
    print(f"[dummy:{sys.argv[1]}] {msg}", flush=True)


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def read_rates_csv(path: Path) -> dict[str, dict[int, float]]:
    """slave_rates_<model>.csv -> {well: {year: q_liq}}"""
    out: dict[str, dict[int, float]] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.setdefault(row["well"], {})[int(row["year"])] = float(row["q_liq_sm3d"])
    return out


def parse_gsatprod_inc(path: Path) -> list[dict]:
    """Parse the GSATPROD include table.

    Format written by master_network (and used for the static slave):

        GSATPROD
        --  WELL      YEAR  Q_LIQ_SM3D  Q_GAS_SM3D  P_WH_BAR  P_BHP_BAR  GSAT
            'N-P1'    2024  207.50      24900       41.90     299.98     0.342 /
        /

    Comment lines start with --. The keyword line and the closing / are
    skipped. Tokens may be single- or double-quoted.
    """
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("--") or line.startswith("GSATPROD") or line == "/":
            continue
        toks = [t.strip("'\"") for t in line.split()]
        if len(toks) < 7:
            continue
        rows.append(
            {
                "well": toks[0],
                "year": int(float(toks[1])),
                "q_liq": float(toks[2]),
                "q_gas": float(toks[3]),
                "p_wh": float(toks[4]),
                "p_bhp": float(toks[5]),
                "gsat": float(toks[6]),
            }
        )
    return rows


def run_master(spec: dict, model_dir: Path, coupling: dict) -> None:
    years = int(coupling["schedule"]["years"])
    net = spec["network"]
    initial = coupling.get("initial_rates_sm3d", {})
    coupling_dir = model_dir.parent / "coupling"
    coupling_dir.mkdir(parents=True, exist_ok=True)

    # group the network wells per slave model
    by_slave: dict[str, list[dict]] = {}
    for w in spec["wells"]:
        by_slave.setdefault(w["slave"], []).append(w)

    report = [
        f"master_network report - iteration {sys.argv[3]}",
        f"manifold pressure: {net['manifold_pressure_bar']} bar",
        "",
    ]
    for slave, wells in sorted(by_slave.items()):
        rates_csv = coupling_dir / f"slave_rates_{slave}.csv"
        if rates_csv.exists():
            demand = read_rates_csv(rates_csv)  # {well: {year: q}}
        else:
            demand = {
                w["name"]: {y: initial.get(w["name"], 250.0) for y in range(1, years + 1)} for w in wells
            }

        header = ["well", "year", "q_liq_sm3d", "q_gas_sm3d", "p_wh_bar", "p_bhp_bar", "gsat"]
        rows = []
        inc_lines = [
            "GSATPROD",
            f"-- Production profiles for subsea wells of {slave}, written by master_network",
            f"-- coupling iteration {sys.argv[3]}. Format: WELL YEAR Q_LIQ Q_GAS P_WH P_BHP GSAT",
            "--  WELL      YEAR  Q_LIQ_SM3D  Q_GAS_SM3D  P_WH_BAR  P_BHP_BAR  GSAT",
        ]
        for w in wells:
            q0 = initial.get(w["name"], 250.0)
            for y in range(1, years + 1):
                q = max(0.0, demand[w["name"]].get(y, demand[w["name"]].get(1, q0)))
                p_wh = (
                    net["manifold_pressure_bar"]
                    + net["friction_a_bar_sm3d"] * q
                    + net["friction_b_bar_sm3d2"] * q * q
                )
                p_bhp = p_wh + net["fluid_density_kg_m3"] * GRAVITY * w["md_m"] * 1e-5
                gsat = min(0.9, w.get("gsat_base", 0.30) + 0.05 * q / max(q0, 1.0))
                q_gas = q * w["gor_sm3_sm3"]
                rows.append(
                    [w["name"], y, round(q, 2), round(q_gas, 0), round(p_wh, 2), round(p_bhp, 2), round(gsat, 3)]
                )
                inc_lines.append(
                    "    '%s'  %d  %.2f  %.0f  %.2f  %.2f  %.3f /"
                    % (w["name"], y, q, q_gas, p_wh, p_bhp, gsat)
                )
                report.append(f"  {slave}/{w['name']} year {y}: q={q:.2f} sm3/d p_bhp={p_bhp:.2f} bar")
        inc_lines.append("/")
        (coupling_dir / f"gsatprod_{slave}.inc").write_text("\n".join(inc_lines) + "\n", encoding="utf-8")
        write_csv(coupling_dir / f"gsatprofiles_{slave}.csv", header, rows)
        log(f"wrote GSATPROD table for {slave} ({len(wells)} wells, {years} years)")

    (model_dir / "master_network_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")


def run_slave(spec: dict, model_dir: Path) -> None:
    coupling_dir = model_dir.parent / "coupling"
    inc_path = coupling_dir / f"gsatprod_{spec['model']}.inc"
    if not inc_path.exists():
        raise SystemExit(f"missing GSATPROD table: {inc_path}")
    rows = parse_gsatprod_inc(inc_path)

    wells = {w["name"]: w for w in spec["wells"]}
    p_res = {name: w["p_res0_bar"] for name, w in wells.items()}
    cum = {name: 0.0 for name in wells}
    out_rows = []
    report = [f"{spec['model']} report - iteration {sys.argv[3]}", ""]
    for r in rows:
        w = wells[r["well"]]
        p_bhp_net = r["p_bhp"]
        # Linear IPR with constant productivity index:
        #   J = q0 / (p_res0 - p_bhp0),  q_ipr = J * (p_res - p_bhp)
        denom = w["p_res0_bar"] - w["p_bhp0_bar"]
        q_ipr = w["q0_sm3d"] * (p_res[r["well"]] - p_bhp_net) / denom if denom > 0 else w["q0_sm3d"]
        q_ipr = max(0.0, q_ipr)
        q_out = min(r["q_liq"], q_ipr)
        choked = 1 if q_out < r["q_liq"] - 1e-6 else 0
        cum[r["well"]] += q_out * 365.0
        out_rows.append(
            [
                r["well"],
                r["year"],
                round(q_out, 2),
                round(q_out * w["gor_sm3_sm3"], 0),
                round(p_bhp_net, 2),
                round(p_res[r["well"]], 2),
                choked,
            ]
        )
        report.append(
            f"  {r['well']} year {r['year']}: q={q_out:.2f} sm3/d (network {r['q_liq']:.2f}, IPR {q_ipr:.2f})"
            + ("  CHOKED (IPR limited)" if choked else "")
        )
        # reservoir pressure depletion: fixed per year + small cumulative effect
        p_res[r["well"]] -= w.get("depletion_bar_per_year", 3.0) + 0.5 * (cum[r["well"]] / 1.0e6)

    write_csv(
        coupling_dir / f"slave_rates_{spec['model']}.csv",
        ["well", "year", "q_liq_sm3d", "q_gas_sm3d", "p_bhp_bar", "p_res_bar", "choked"],
        out_rows,
    )
    (model_dir / f"slave_{spec['model']}_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    log(f"slave {spec['model']} produced rates for {len(rows)} well-year rows")


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    model, model_dir_s, _iter_s = sys.argv[1], sys.argv[2], sys.argv[3]
    model_dir = Path(model_dir_s)
    spec = load_json(model_dir / "simspec.json")
    coupling = load_json(Path(__file__).resolve().parents[1] / "coupling.json")
    if spec["role"] == "master":
        run_master(spec, model_dir, coupling)
    else:
        run_slave(spec, model_dir)


if __name__ == "__main__":
    main()
