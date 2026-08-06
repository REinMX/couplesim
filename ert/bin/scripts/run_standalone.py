#!/usr/bin/env python3
"""Run ONE model of the coupled example as a standalone ERT realization.

The per-model ERT configs (master_network.ert, model_n.ert, model_hdn.ert)
mirror the FMU convention of one independent ERT file per model. Each
realization stages ``input/<model>/``, applies that model's own GEN_KW
parameter (``Q0_MULT_MODEL_<N|HDN>`` for the slaves, ``NETWORK_CHOKE`` for
the master) and runs the model ONCE against static boundary conditions:

  slave  : fixed network constraints from the simspec reference BHP
           (p_bhp = p_bhp0, i.e. no backpressure) and initial rates
  master : fixed slave rates from coupling.json ``initial_slave_rates_sm3d``

This is the per-model quality check (like your 100-realization FMU runs for
model_n/model_hdn alone). The coupled loop -- master and both slaves
iterating to convergence in one realization -- belongs to
02_ensemble_coupled.ert / run_coupled.py.

The slave backend is taken from coupling.json (default: real OPM Flow for
both slaves; ``dummy`` keeps the licence-free path). The standalone master
uses the external network solver (eclipse_dummy.py); the Spike 004 Flow
master requires the full coupled context of 02_ensemble_coupled.ert.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_coupled import (  # noqa: E402
    DEFAULT_CONFIG,
    FLOW_ADAPTER,
    ROOT,
    apply_network_choke,
    apply_q0_mult,
    initial_rate_rows,
    load_json,
    parse_network_choke,
    schedule_years,
    slave_q0_multipliers,
    stage_model,
    validate_runpath,
    validate_topology,
)

ALLOWED_MODELS = ("master_network", "model_n", "model_hdn")


def log(message: str) -> None:
    print(f"[run_standalone] {message}", flush=True)


def static_constraint_rows(
    runpath: Path, coupling: dict[str, Any], model: str, years: list[int]
) -> list[list[Any]]:
    """Unconstrained (reference-IPR) boundary conditions for a solo slave run."""
    spec = load_json(runpath / model / "simspec.json")
    initial = coupling["initial_slave_rates_sm3d"]
    header = [
        "well",
        "year",
        "network_input_q_liq_sm3d",
        "prescribed_q_liq_sm3d",
        "simulated_q_liq_sm3d",
        "total_network_q_liq_sm3d",
        "p_manifold_bar",
        "p_wh_bar",
        "p_bhp_bar",
    ]
    rows: list[list[Any]] = []
    for well in spec["wells"]:
        name = well["name"]
        if name not in initial:
            raise ValueError(f"missing initial rate for {model}/{name}")
        rate = float(initial[name])
        p_bhp0 = float(well["p_bhp0_bar"])
        if not all(math.isfinite(value) for value in (rate, p_bhp0)) or rate < 0.0:
            raise ValueError(f"non-finite standalone constraint for {model}/{name}")
        for year in years:
            rows.append([name, year, rate, rate, rate, rate, 0.0, 0.0, p_bhp0])
    return rows


def run_flow_slave_standalone(runpath: Path, model: str) -> None:
    """Run the real OPM Flow slave once against the static constraints."""
    if not FLOW_ADAPTER.is_file():
        raise FileNotFoundError(
            f"flow backend requires the Spike 003 adapter at {FLOW_ADAPTER}"
        )
    constraints = runpath / "coupling" / f"network_constraints_{model}.csv"
    run_dir = runpath / "coupling" / f"flow_{model}" / "standalone"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    subprocess.run(
        [
            sys.executable,
            str(FLOW_ADAPTER),
            "--constraints",
            str(constraints),
            "--output-dir",
            str(run_dir),
            "--model",
            model,
        ],
        check=True,
        cwd=runpath,
    )
    raw_rates = run_dir / f"slave_rates_{model}.csv"
    if not raw_rates.is_file():
        raise FileNotFoundError(f"flow backend did not produce slave rates: {raw_rates}")
    shutil.copy2(raw_rates, runpath / "coupling" / f"slave_rates_{model}.csv")
    log(f"{model} (flow backend, standalone): {runpath / 'coupling' / f'slave_rates_{model}.csv'}")


def write_slave_report(runpath: Path, model: str, multiplier: float, backend: str) -> str:
    rates_path = runpath / "coupling" / f"slave_rates_{model}.csv"
    with rates_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"standalone slave produced no rates: {rates_path}")
    lines = [
        "STANDALONE SLAVE REPORT",
        "=======================",
        f"model      : {model}",
        f"backend    : {backend}",
        f"q0_mult    : {multiplier}",
        f"boundary   : static reference IPR (p_bhp = p_bhp0, no backpressure)",
        "",
        "Simulated rates (sm3/d):",
    ]
    for row in sorted(rows, key=lambda r: (r["well"], int(r["year"]))):
        lines.append(
            f"  {row['well']} {row['year']}: q_liq={float(row['q_liq_sm3d']):.2f}, "
            f"q_gas={float(row.get('q_gas_sm3d') or 0.0):.2f}, "
            f"p_bhp={float(row['p_bhp_bar']):.2f} bar"
        )
    report = "\n".join(lines) + "\n"
    (runpath / "STANDALONE_REPORT.txt").write_text(report, encoding="utf-8")
    return report


def write_master_report(runpath: Path, choke: float, slaves: list[str]) -> str:
    lines = [
        "STANDALONE MASTER REPORT",
        "=========================",
        f"model           : master_network",
        f"backend         : dummy (external network solver)",
        f"network_choke   : {choke}",
        f"boundary        : static slave rates from coupling.json initial_slave_rates_sm3d",
        "",
        "Network constraints by slave (sm3/d, bar):",
    ]
    for slave in slaves:
        path = runpath / "coupling" / f"network_constraints_{slave}.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            raise ValueError(f"standalone master produced no constraints for {slave}: {path}")
        lines.append(f"  {slave}:")
        for row in sorted(rows, key=lambda r: (r["well"], int(r["year"]))):
            lines.append(
                f"    {row['well']} {row['year']}: q={float(row['network_input_q_liq_sm3d']):.2f}, "
                f"p_manifold={float(row['p_manifold_bar']):.2f}, p_wh={float(row['p_wh_bar']):.2f}, "
                f"p_bhp={float(row['p_bhp_bar']):.2f}"
            )
    report = "\n".join(lines) + "\n"
    (runpath / "STANDALONE_REPORT.txt").write_text(report, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=list(ALLOWED_MODELS), required=True)
    parser.add_argument("--runpath", help="explicit output runpath (default: cwd)")
    parser.add_argument(
        "--config",
        default=os.environ.get("COUPLING_CONFIG") or str(DEFAULT_CONFIG),
        help="coupling configuration JSON (default: coupling.json, or $COUPLING_CONFIG)",
    )
    args = parser.parse_args()

    coupling = load_json(Path(args.config))
    validate_topology(coupling)
    runpath = Path(args.runpath).resolve() if args.runpath else Path.cwd()
    validate_runpath(runpath)
    runpath.mkdir(parents=True, exist_ok=True)
    coupling_dir = runpath / "coupling"
    coupling_dir.mkdir(parents=True, exist_ok=True)
    # The dummy slave writes its exchange JSON into coupling/exchange/, a
    # directory the coupled master normally creates first.
    (coupling_dir / "exchange").mkdir(parents=True, exist_ok=True)
    (runpath / "coupling_config.json").write_text(
        json.dumps(coupling, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )

    stage_model(runpath, args.model)
    years = schedule_years(coupling)

    if args.model == "master_network":
        if coupling["master"].get("backend", "dummy") != "dummy":
            raise ValueError(
                "standalone master supports the dummy network solver only; "
                "the Flow master needs the full coupled 02_ensemble_coupled.ert"
            )
        choke = parse_network_choke(runpath, allow_default=True)
        apply_network_choke(runpath, args.model, choke)
        for slave in ("model_n", "model_hdn"):
            stage_model(runpath, slave)
            initial_rate_rows(runpath, coupling, slave, years)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "bin" / "eclipse_dummy.py"),
                args.model,
                str(runpath / args.model),
                "1",
            ],
            check=True,
            cwd=runpath,
        )
        report = write_master_report(runpath, choke, list(coupling["slaves"]))
        print(report, end="")
        print("STANDALONE MASTER COMPLETE")
        return 0

    multipliers = slave_q0_multipliers(runpath, allow_default=True)
    multiplier = multipliers[args.model]
    apply_q0_mult(runpath, args.model, multiplier)
    backend = str(coupling["slaves"][args.model].get("backend", "dummy"))
    if backend not in ("dummy", "flow"):
        raise ValueError(f"slave {args.model} has unsupported backend {backend!r}")

    header = [
        "well",
        "year",
        "network_input_q_liq_sm3d",
        "prescribed_q_liq_sm3d",
        "simulated_q_liq_sm3d",
        "total_network_q_liq_sm3d",
        "p_manifold_bar",
        "p_wh_bar",
        "p_bhp_bar",
    ]
    constraints_path = runpath / "coupling" / f"network_constraints_{args.model}.csv"
    constraints_path.parent.mkdir(parents=True, exist_ok=True)
    with constraints_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(static_constraint_rows(runpath, coupling, args.model, years))

    if backend == "flow":
        run_flow_slave_standalone(runpath, args.model)
    else:
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "bin" / "eclipse_dummy.py"),
                args.model,
                str(runpath / args.model),
                "1",
            ],
            check=True,
            cwd=runpath,
        )
    report = write_slave_report(runpath, args.model, multiplier, backend)
    print(report, end="")
    print("STANDALONE SLAVE COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
