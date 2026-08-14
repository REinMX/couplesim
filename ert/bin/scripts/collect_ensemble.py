#!/usr/bin/env python3
"""Aggregate a coupled ERT ensemble into results CSV + P10/P50/P90 summary.

Scans the runpath tree written by ERT (realization-<IENS>/iter-<ITER>) for
the coupled case, reads the per-realization GEN_KW parameters, the coupling
convergence history and the final slave rates, and writes:

  ensemble_results.csv   one row per realization:
                         parameters, iterations, converged, final residual,
                         final q_liq/q_gas/p_bhp per well-year
  ensemble_summary.csv   per well-year: P10/P50/P90 (nearest-rank),
                         mean, min, max, count

Usage:
    python3 collect_ensemble.py --case-dir output/02_coupled
    python3 collect_ensemble.py --case-dir output/02_coupled --iter 0
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


def percentile_nearest_rank(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: index = ceil(fraction * n) - 1, 1-based rank.

    P10/P50/P90 with n realizations: the rank is ceil(0.1n), ceil(0.5n),
    ceil(0.9n) counting from the smallest value (P10 = low case, P90 = high).
    """
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError(f"fraction must be in [0, 1]: {fraction}")
    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


def parse_param(runpath: Path, name: str) -> float | None:
    path = runpath / f"{name}.txt"
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--")):
            continue
        return float(line.split()[-1])
    return None


def read_convergence(runpath: Path) -> tuple[int, float]:
    path = runpath / "coupling" / "convergence_history.csv"
    if not path.is_file():
        return 0, math.nan
    rows: list[list[str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 2:
        return 0, math.nan
    last = rows[-1]
    return int(last[0]), float(last[1])


def read_slave_rates(runpath: Path, model: str) -> list[dict[str, Any]]:
    path = runpath / "coupling" / f"slave_rates_{model}.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {
            "well": row["well"],
            "year": int(row["year"]),
            "q_liq_sm3d": float(row["q_liq_sm3d"]),
            "q_gas_sm3d": float(row.get("q_gas_sm3d") or 0.0),
            "p_bhp_bar": float(row.get("p_bhp_bar") or 0.0),
        }
        for row in rows
    ]


def load_slaves(runpath: Path) -> list[str]:
    """Slave models of a realization, from the coupling config copy the
    driver writes into each runpath."""
    path = runpath / "coupling_config.json"
    if not path.is_file():
        return []
    coupling = json.loads(path.read_text(encoding="utf-8"))
    return list(coupling.get("slaves", {}))


def collect_realizations(case_dir: Path, iter_filter: int | None) -> list[dict[str, Any]]:
    realizations: list[dict[str, Any]] = []
    for real_dir in sorted(case_dir.glob("realization-*"), key=lambda p: int(p.name.split("-")[1])):
        iterations = sorted(
            (p for p in real_dir.glob("iter-*") if p.is_dir()),
            key=lambda p: int(p.name.split("-")[1]),
        )
        for iter_dir in iterations:
            iter_no = int(iter_dir.name.split("-")[1])
            if iter_filter is not None and iter_no != iter_filter:
                continue
            if not (iter_dir / "OK").is_file():
                continue
            entry: dict[str, Any] = {
                "realization": int(real_dir.name.split("-")[1]),
                "iter": iter_no,
                "network_choke": parse_param(iter_dir, "network_choke") or 1.0,
                "converged": True,
            }
            for model in load_slaves(iter_dir):
                entry[f"q0_mult_{model}"] = parse_param(iter_dir, f"q0_mult_{model}")
                if entry[f"q0_mult_{model}"] is None:
                    entry[f"q0_mult_{model}"] = parse_param(iter_dir, "q0_mult")
            iterations_used, final_residual = read_convergence(iter_dir)
            entry["iterations"] = iterations_used
            entry["final_residual"] = final_residual
            for model in load_slaves(iter_dir):
                for rate in read_slave_rates(iter_dir, model):
                    key = f"{rate['well']}_{rate['year']}"
                    entry[f"{key}_q_liq_sm3d"] = rate["q_liq_sm3d"]
                    entry[f"{key}_q_gas_sm3d"] = rate["q_gas_sm3d"]
                    entry[f"{key}_p_bhp_bar"] = rate["p_bhp_bar"]
            realizations.append(entry)
    return realizations


def write_results(realizations: list[dict[str, Any]], out_dir: Path) -> Path:
    if not realizations:
        raise ValueError(f"no completed realizations found in {out_dir}")
    fieldnames = list(realizations[0])
    for entry in realizations[1:]:
        for name in entry:
            if name not in fieldnames:
                fieldnames.append(name)
    results_path = out_dir / "ensemble_results.csv"
    with results_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(realizations)
    return results_path


def write_summary(realizations: list[dict[str, Any]], out_dir: Path) -> Path:
    metric_keys = sorted(
        {
            name
            for entry in realizations
            for name in entry
            if name.endswith("_q_liq_sm3d")
        }
    )
    summary_path = out_dir / "ensemble_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["well", "year", "n", "p10_sm3d", "p50_sm3d", "p90_sm3d", "mean_sm3d", "min_sm3d", "max_sm3d"]
        )
        for key in metric_keys:
            suffix = "_q_liq_sm3d"
            stem = key[: -len(suffix)]
            well, year_text = stem.rsplit("_", 1)
            values = [float(entry[key]) for entry in realizations if key in entry]
            if not values:
                continue
            writer.writerow(
                [
                    well,
                    year_text,
                    len(values),
                    round(percentile_nearest_rank(values, 0.1), 3),
                    round(percentile_nearest_rank(values, 0.5), 3),
                    round(percentile_nearest_rank(values, 0.9), 3),
                    round(statistics.mean(values), 3),
                    round(min(values), 3),
                    round(max(values), 3),
                ]
            )
    return summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=Path("output/02_coupled"),
        help="runpath tree root (default: output/02_coupled)",
    )
    parser.add_argument("--iter", type=int, default=None, help="only this iteration number")
    parser.add_argument("--out-dir", type=Path, default=None, help="output directory (default: case-dir)")
    args = parser.parse_args()

    case_dir = args.case_dir.resolve()
    if not case_dir.is_dir():
        raise SystemExit(f"case directory not found: {case_dir}")
    realizations = collect_realizations(case_dir, args.iter)
    if not realizations:
        raise SystemExit(f"no completed (OK) realizations under {case_dir}")
    out_dir = (args.out_dir or case_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = write_results(realizations, out_dir)
    summary_path = write_summary(realizations, out_dir)

    print(f"collected {len(realizations)} completed realization(s) from {case_dir}")
    print(f"ensemble_results.csv : {results_path}")
    print(f"ensemble_summary.csv : {summary_path}")
    print()
    with summary_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            print(
                f"  {row['well']:>6} {row['year']}: "
                f"P10={row['p10_sm3d']:>9}  P50={row['p50_sm3d']:>9}  "
                f"P90={row['p90_sm3d']:>9}  mean={row['mean_sm3d']:>9} sm3/d "
                f"(n={row['n']})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
