#!/usr/bin/env python3
"""Run one real OPM Flow model_n BHP-constraint-to-rate round trip."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "MODEL_N_FLOW.DATA.tmpl"
EXPECTED_WELLS = ("N-P1", "N-P2")
INITIAL_PRESSURE_BAR = 350.0
SUMMARY_VECTORS = (
    "WOPR:N-P1",
    "WWPR:N-P1",
    "WBHP:N-P1",
    "WOPR:N-P2",
    "WWPR:N-P2",
    "WBHP:N-P2",
    "FPR",
)


def read_constraints(path: Path, year: int) -> dict[str, float]:
    if not path.is_file():
        raise FileNotFoundError(f"constraint file does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"well", "year", "p_bhp_bar"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"constraint file must contain columns {sorted(required)}")
        constraints: dict[str, float] = {}
        for row in reader:
            try:
                row_year = int(row["year"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid constraint year: {row.get('year')!r}") from exc
            if row_year != year:
                continue
            well = row["well"]
            if well not in EXPECTED_WELLS:
                raise ValueError(f"unexpected model_n constraint well for {year}: {well!r}")
            if well in constraints:
                raise ValueError(f"duplicate constraint for {well}/{year}")
            try:
                bhp = float(row["p_bhp_bar"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid BHP constraint for {well}/{year}") from exc
            if not math.isfinite(bhp) or bhp <= 0.0:
                raise ValueError(f"BHP constraint must be finite positive for {well}/{year}: {bhp}")
            if bhp >= INITIAL_PRESSURE_BAR:
                raise ValueError(
                    f"BHP constraint must be below {INITIAL_PRESSURE_BAR} bar for "
                    f"this production-only spike: {well}/{year}={bhp}"
                )
            constraints[well] = bhp
    missing = sorted(set(EXPECTED_WELLS) - constraints.keys())
    if missing:
        raise ValueError(f"missing constraints for year {year}: {missing}")
    return constraints


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(f"required executable is not on PATH: {name}")
    return str(Path(executable).resolve())


def render_deck(destination: Path, constraints: dict[str, float], year: int) -> None:
    rendered = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "__START_YEAR__": str(year),
        "__N_P1_BHP_BAR__": f"{constraints['N-P1']:.6f}",
        "__N_P2_BHP_BAR__": f"{constraints['N-P2']:.6f}",
    }
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise ValueError(f"deck template must contain exactly one {marker}")
        rendered = rendered.replace(marker, value)
    destination.write_text(rendered, encoding="utf-8")


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


def parse_summary(text: str) -> dict[str, float]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 2:
        raise ValueError(f"expected one report-step row from summary, found {len(lines) - 1}")
    header = lines[0].split()
    if tuple(header) != SUMMARY_VECTORS:
        raise ValueError(f"unexpected summary vectors: {header}")
    values = lines[1].split()
    if len(values) != len(header):
        raise ValueError(f"summary value count does not match header: {lines[1]}")
    parsed = {name: float(value) for name, value in zip(header, values, strict=True)}
    non_finite = {name: value for name, value in parsed.items() if not math.isfinite(value)}
    if non_finite:
        raise ValueError(f"summary contains non-finite values: {non_finite}")
    return parsed


def extract_rates(summary_executable: str, smspec: Path) -> tuple[dict[str, float], str]:
    completed = run_checked(
        [summary_executable, "-r", str(smspec), *SUMMARY_VECTORS],
        cwd=smspec.parent,
    )
    return parse_summary(completed.stdout), completed.stdout


def write_rates(
    path: Path,
    year: int,
    constraints: dict[str, float],
    summary_values: dict[str, float],
) -> list[dict[str, Any]]:
    reservoir_pressure = round(summary_values["FPR"], 6)
    rows: list[dict[str, Any]] = []
    for well in EXPECTED_WELLS:
        oil_rate = summary_values[f"WOPR:{well}"]
        water_rate = summary_values[f"WWPR:{well}"]
        bhp = round(summary_values[f"WBHP:{well}"], 6)
        liquid_rate = round(oil_rate + water_rate, 6)
        numeric = (oil_rate, water_rate, bhp, reservoir_pressure, liquid_rate)
        if not all(math.isfinite(value) for value in numeric) or liquid_rate < 0.0:
            raise ValueError(f"invalid Flow result for {well}/{year}: {numeric}")
        if not math.isclose(bhp, constraints[well], abs_tol=1.0e-3):
            raise ValueError(
                f"Flow WBHP does not match constraint for {well}/{year}: "
                f"expected {constraints[well]}, got {bhp}"
            )
        rows.append(
            {
                "well": well,
                "year": year,
                "q_liq_sm3d": liquid_rate,
                "q_gas_sm3d": 0.0,
                "p_bhp_bar": bhp,
                "p_res_bar": reservoir_pressure,
                "q_ipr_sm3d": liquid_rate,
                "backpressure_limited": 1,
                "origin": "opm_flow",
            }
        )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def run_roundtrip(
    constraints_path: Path,
    year: int,
    output_dir: Path,
    flow_name: str,
    summary_name: str,
) -> Path:
    if year < 1900 or year > 9999:
        raise ValueError(f"year must be between 1900 and 9999: {year}")
    constraints = read_constraints(constraints_path, year)
    flow_executable = require_executable(flow_name)
    summary_executable = require_executable(summary_name)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    version = run_checked([flow_executable, "--version"], cwd=output_dir).stdout.strip()
    deck_path = output_dir / "MODEL_N_FLOW.DATA"
    render_deck(deck_path, constraints, year)
    flow_dir = output_dir / "flow-run"
    flow_dir.mkdir()
    flow_command = [
        flow_executable,
        str(deck_path),
        f"--output-dir={flow_dir}",
        "--enable-terminal-output=false",
        "--output-mode=log",
    ]
    flow_completed = run_checked(flow_command, cwd=output_dir)
    (output_dir / "flow.stdout.log").write_text(flow_completed.stdout, encoding="utf-8")
    (output_dir / "flow.stderr.log").write_text(flow_completed.stderr, encoding="utf-8")

    smspec = flow_dir / "MODEL_N_FLOW.SMSPEC"
    unsmry = flow_dir / "MODEL_N_FLOW.UNSMRY"
    if not smspec.is_file() or not unsmry.is_file():
        raise FileNotFoundError(f"Flow did not create required summary artifacts below {flow_dir}")
    summary_values, summary_text = extract_rates(summary_executable, smspec)
    (output_dir / "summary.txt").write_text(summary_text, encoding="utf-8")
    rates_path = output_dir / "slave_rates_model_n.csv"
    rows = write_rates(rates_path, year, constraints, summary_values)

    report = {
        "simulator": version,
        "extractor": Path(summary_executable).name,
        "year": year,
        "constraints": constraints,
        "results": rows,
        "artifacts": {
            "deck": str(deck_path),
            "smspec": str(smspec),
            "unsmry": str(unsmry),
            "rates": str(rates_path),
        },
    }
    (output_dir / "roundtrip_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return rates_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--flow", default="flow", help="Flow executable name or path")
    parser.add_argument("--summary", default="summary", help="OPM summary executable name or path")
    args = parser.parse_args()
    rates_path = run_roundtrip(
        args.constraints.resolve(),
        args.year,
        args.output_dir.resolve(),
        args.flow,
        args.summary,
    )
    print(f"OPM MODEL_N ROUNDTRIP COMPLETE: {rates_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
