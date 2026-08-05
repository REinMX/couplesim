#!/usr/bin/env python3
"""Run the OPM Flow network-master spike sensitivity cases.

Cases
-----
1. two_well_network  — two real producers on a trunk with VFP table 2.
   Demonstrates that total simulated liquid rate loads the branch VFP
   and raises manifold back-pressure.

2. gsatprod_network  — one real producer + GSATPROD satellite group on
   the same trunk topology. Demonstrates that satellite rates appear in
   group totals but do NOT load the branch VFP in Flow 2025.10.

3. gconprod_gsatprod — GCONPROD field oil limit + GSATPROD satellite.
   Demonstrates that satellite rates DO count toward group control limits.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
VFP_INC = HERE / "vfp_tables.inc"
TWO_WELL_TMPL = HERE / "TWO_WELL_NETWORK.DATA.tmpl"
MASTER_GSAT_TMPL = HERE / "MASTER_GSATPROD.DATA.tmpl"
GCON_DECK = HERE / "GCONPROD_GSATPROD.DATA"

SUMMARY_TWO_WELL = (
    "WOPR:M-P1",
    "WOPR:M-P2",
    "WTHP:M-P1",
    "GPR:MANIFOLD",
)
SUMMARY_GSAT = (
    "WOPR:M-P1",
    "WTHP:M-P1",
    "GOPR:SAT",
    "GPR:MANIFOLD",
)
SUMMARY_GCON = (
    "WOPR:M-P1",
    "GOPR:SAT",
    "FOPR",
)


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(f"required executable is not on PATH: {name}")
    return executable


def run_checked(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"command failed with exit {completed.returncode}: {' '.join(command)}\n{detail}"
        )
    return completed


def parse_summary(text: str, expected: tuple[str, ...]) -> dict[str, float]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 2:
        raise ValueError(f"expected one report-step row from summary, found {len(lines) - 1}")
    header = lines[0].split()
    if tuple(header) != expected:
        raise ValueError(f"unexpected summary vectors: {header}")
    values = lines[1].split()
    if len(values) != len(header):
        raise ValueError(f"summary value count does not match header: {lines[1]}")
    parsed = {name: float(value) for name, value in zip(header, values, strict=True)}
    non_finite = {name: value for name, value in parsed.items() if not math.isfinite(value)}
    if non_finite:
        raise ValueError(f"summary contains non-finite values: {non_finite}")
    return parsed


def extract_summary(summary_exe: str, smspec: Path, vectors: tuple[str, ...]) -> dict[str, float]:
    completed = run_checked(
        [summary_exe, "-r", str(smspec), *vectors],
        cwd=smspec.parent,
    )
    return parse_summary(completed.stdout, vectors)


def render_template(template: Path, destination: Path, replacements: dict[str, str]) -> None:
    rendered = template.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise ValueError(f"template must contain exactly one {marker}")
        rendered = rendered.replace(marker, value)
    if "__" in rendered:
        raise ValueError(f"unresolved template token remains in {destination.name}")
    destination.write_text(rendered, encoding="utf-8")


def prepare_case_dir(output_dir: Path, name: str) -> Path:
    case_dir = output_dir / name
    if case_dir.exists() and any(case_dir.iterdir()):
        raise ValueError(f"case directory must be absent or empty: {case_dir}")
    case_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VFP_INC, case_dir / VFP_INC.name)
    return case_dir


def run_flow(flow_exe: str, deck: Path, case_dir: Path) -> str:
    completed = run_checked(
        [
            flow_exe,
            str(deck.name),
            "--enable-terminal-output=false",
            "--output-mode=log",
        ],
        cwd=case_dir,
    )
    (case_dir / "flow.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (case_dir / "flow.stderr.log").write_text(completed.stderr, encoding="utf-8")
    return completed.stdout


def run_two_well_cases(
    flow_exe: str,
    summary_exe: str,
    output_dir: Path,
) -> list[dict[str, Any]]:
    scenarios = (
        ("low", 50.0, 50.0),
        ("mid", 200.0, 200.0),
        ("high", 400.0, 400.0),
    )
    results: list[dict[str, Any]] = []
    for name, r1, r2 in scenarios:
        case_dir = prepare_case_dir(output_dir / "two_well", name)
        deck = case_dir / "TWO_WELL_NETWORK.DATA"
        render_template(
            TWO_WELL_TMPL,
            deck,
            {
                "__M_P1_ORAT__": f"{r1:.6f}",
                "__M_P2_ORAT__": f"{r2:.6f}",
            },
        )
        run_flow(flow_exe, deck, case_dir)
        smspec = case_dir / "TWO_WELL_NETWORK.SMSPEC"
        if not smspec.is_file():
            raise FileNotFoundError(f"Flow did not create summary: {smspec}")
        values = extract_summary(summary_exe, smspec, SUMMARY_TWO_WELL)
        results.append(
            {
                "case": name,
                "requested_orat_sm3d": {"M-P1": r1, "M-P2": r2},
                "results": values,
            }
        )
    return results


def run_gsatprod_cases(
    flow_exe: str,
    summary_exe: str,
    output_dir: Path,
) -> list[dict[str, Any]]:
    scenarios = (
        ("sat000", 0.0, 0.0),
        ("sat130", 130.0, 15600.0),
        ("sat600", 600.0, 72000.0),
    )
    results: list[dict[str, Any]] = []
    for name, oil, gas in scenarios:
        case_dir = prepare_case_dir(output_dir / "gsatprod_network", name)
        deck = case_dir / "MASTER_GSATPROD.DATA"
        render_template(
            MASTER_GSAT_TMPL,
            deck,
            {
                "__SAT_OIL_SM3D__": f"{oil:.6f}",
                "__SAT_GAS_SM3D__": f"{gas:.6f}",
            },
        )
        run_flow(flow_exe, deck, case_dir)
        smspec = case_dir / "MASTER_GSATPROD.SMSPEC"
        if not smspec.is_file():
            raise FileNotFoundError(f"Flow did not create summary: {smspec}")
        values = extract_summary(summary_exe, smspec, SUMMARY_GSAT)
        results.append(
            {
                "case": name,
                "requested_gsatprod_oil_sm3d": oil,
                "requested_gsatprod_gas_sm3d": gas,
                "results": values,
            }
        )
    return results


def run_gconprod_case(
    flow_exe: str,
    summary_exe: str,
    output_dir: Path,
) -> dict[str, Any]:
    case_dir = prepare_case_dir(output_dir, "gconprod_gsatprod")
    deck = case_dir / GCON_DECK.name
    shutil.copy2(GCON_DECK, deck)
    run_flow(flow_exe, deck, case_dir)
    smspec = case_dir / f"{GCON_DECK.stem}.SMSPEC"
    if not smspec.is_file():
        raise FileNotFoundError(f"Flow did not create summary: {smspec}")
    values = extract_summary(summary_exe, smspec, SUMMARY_GCON)
    return {
        "case": "gconprod_gsatprod",
        "requested_orat_sm3d": 200.0,
        "requested_gsatprod_oil_sm3d": 150.0,
        "field_orat_limit_sm3d": 250.0,
        "results": values,
    }


def analyse(report: dict[str, Any]) -> dict[str, Any]:
    two_well = report["two_well_network"]
    gsat = report["gsatprod_network"]
    gcon = report["gconprod_gsatprod"]

    manifold_pressures = [case["results"]["GPR:MANIFOLD"] for case in two_well]
    two_well_loads_trunk = manifold_pressures[-1] > manifold_pressures[0] + 1.0

    gsat_manifold = [case["results"]["GPR:MANIFOLD"] for case in gsat]
    gsat_oil = [case["results"]["GOPR:SAT"] for case in gsat]
    gsat_registers = all(
        math.isclose(got, want, rel_tol=0.0, abs_tol=1.0e-3)
        for got, want in zip(
            gsat_oil,
            [case["requested_gsatprod_oil_sm3d"] for case in gsat],
            strict=True,
        )
    )
    gsat_loads_trunk = max(gsat_manifold) - min(gsat_manifold) > 1.0

    gcon_values = gcon["results"]
    gcon_sees_satellite = (
        math.isclose(gcon_values["GOPR:SAT"], 150.0, abs_tol=1.0e-3)
        and math.isclose(gcon_values["FOPR"], 250.0, abs_tol=1.0e-3)
        and gcon_values["WOPR:M-P1"] < 200.0 - 1.0
    )

    return {
        "two_well_loads_trunk_vfp": two_well_loads_trunk,
        "two_well_manifold_pressure_bar": manifold_pressures,
        "gsatprod_registers_in_group_totals": gsat_registers,
        "gsatprod_loads_trunk_vfp": gsat_loads_trunk,
        "gsatprod_manifold_pressure_bar": gsat_manifold,
        "gconprod_sees_gsatprod": gcon_sees_satellite,
        "gconprod_well_cutback": {
            "WOPR:M-P1": gcon_values["WOPR:M-P1"],
            "GOPR:SAT": gcon_values["GOPR:SAT"],
            "FOPR": gcon_values["FOPR"],
        },
        "verdict": (
            "PARTIAL"
            if two_well_loads_trunk and gsat_registers and gcon_sees_satellite and not gsat_loads_trunk
            else "INVESTIGATE"
        ),
    }


def execute(output_dir: Path, flow_name: str, summary_name: str) -> dict[str, Any]:
    flow_exe = require_executable(flow_name)
    summary_exe = require_executable(summary_name)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    version = run_checked([flow_exe, "--version"], cwd=output_dir).stdout.strip()
    report: dict[str, Any] = {
        "simulator": version,
        "extractor": Path(summary_exe).name,
        "two_well_network": run_two_well_cases(flow_exe, summary_exe, output_dir),
        "gsatprod_network": run_gsatprod_cases(flow_exe, summary_exe, output_dir),
        "gconprod_gsatprod": run_gconprod_case(flow_exe, summary_exe, output_dir),
    }
    report["analysis"] = analyse(report)
    (output_dir / "network_master_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--flow", default="flow")
    parser.add_argument("--summary", default="summary")
    args = parser.parse_args()
    report = execute(args.output_dir.resolve(), args.flow, args.summary)
    print(json.dumps(report["analysis"], indent=2, allow_nan=False))
    print(f"OPM NETWORK MASTER SPIKE COMPLETE: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
