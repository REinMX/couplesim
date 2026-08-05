#!/usr/bin/env python3
"""Run a stateful three-year OPM Flow model_n chain with annual BHP constraints.

Each year is one Flow run: 2024 starts fresh from EQUIL, 2025 and 2026
continue from the previous year's unified restart file via the Eclipse
RESTART keyword. Year-end WOPR/WWPR/WBHP/FPR/FOPT/FWPT are extracted with
OPM's `summary -r` CLI and emitted in the existing slave_rates_model_n.csv
exchange schema, plus a restart_report.json containing the statefulness
checks (cumulative production continuity, restart-state pressure carry,
WBHP-versus-constraint agreement).
"""

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
BASE_TEMPLATE = HERE / "MODEL_N_BASE.DATA.tmpl"
CONTINUE_TEMPLATE = HERE / "MODEL_N_CONTINUE.DATA.tmpl"
EXPECTED_WELLS = ("N-P1", "N-P2")
EXPECTED_YEARS = (2024, 2025, 2026)
INITIAL_PRESSURE_BAR = 350.0
SUMMARY_VECTORS = (
    "FOPT",
    "FWPT",
    "FPR",
    "WOPR:N-P1",
    "WWPR:N-P1",
    "WBHP:N-P1",
    "WOPR:N-P2",
    "WWPR:N-P2",
    "WBHP:N-P2",
)
# Flow numbers report steps cumulatively across a restart chain: the base run
# writes step 1, the first continuation writes step 2, and so on. The RESTART
# step for the continuation of chain position `index` is therefore `index`.
ALL_MARKERS = (
    "__START_YEAR__",
    "__N_P1_BHP_BAR__",
    "__N_P2_BHP_BAR__",
    "__YEAR_DAYS__",
    "__RESTART_CASE__",
    "__RESTART_STEP__",
)


def days_in_year(year: int) -> int:
    """Days in `year` so a Jan-1 START plus TSTEP lands on Jan-1 of the next year."""
    if year % 4 != 0:
        return 365
    if year % 100 != 0 or year % 400 == 0:
        return 366
    return 365


def read_constraints_chain(path: Path) -> dict[int, dict[str, float]]:
    if not path.is_file():
        raise FileNotFoundError(f"constraint file does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"well", "year", "p_bhp_bar"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"constraint file must contain columns {sorted(required)}")
        by_year: dict[int, dict[str, float]] = {}
        for row in reader:
            try:
                row_year = int(row["year"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid constraint year: {row.get('year')!r}") from exc
            if row_year not in EXPECTED_YEARS:
                raise ValueError(
                    f"unexpected constraint year {row_year}; expected {EXPECTED_YEARS}"
                )
            well = row["well"]
            if well not in EXPECTED_WELLS:
                raise ValueError(f"unexpected model_n constraint well: {well!r}")
            year_map = by_year.setdefault(row_year, {})
            if well in year_map:
                raise ValueError(f"duplicate constraint for {well}/{row_year}")
            try:
                bhp = float(row["p_bhp_bar"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid BHP constraint for {well}/{row_year}") from exc
            if not math.isfinite(bhp) or bhp <= 0.0:
                raise ValueError(
                    f"BHP constraint must be finite positive for {well}/{row_year}: {bhp}"
                )
            if bhp >= INITIAL_PRESSURE_BAR:
                raise ValueError(
                    f"BHP constraint must be below {INITIAL_PRESSURE_BAR} bar for "
                    f"this production-only spike: {well}/{row_year}={bhp}"
                )
            year_map[well] = bhp
    missing_years = [year for year in EXPECTED_YEARS if year not in by_year]
    if missing_years:
        raise ValueError(f"missing constraint rows for years: {missing_years}")
    for year, year_map in by_year.items():
        missing = sorted(set(EXPECTED_WELLS) - year_map.keys())
        if missing:
            raise ValueError(f"missing constraints for year {year}: {missing}")
    return by_year


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(f"required executable is not on PATH: {name}")
    return str(Path(executable).resolve())


def render_deck(
    destination: Path,
    template: Path,
    replacements: dict[str, str],
) -> None:
    rendered = template.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise ValueError(f"deck template {template.name} must contain exactly one {marker}")
        rendered = rendered.replace(marker, value)
    leftover = [marker for marker in ALL_MARKERS if marker in rendered]
    if leftover:
        raise ValueError(f"deck template {template.name} left markers unreplaced: {leftover}")
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


def parse_summary(text: str) -> list[dict[str, float]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(
            f"expected at least one report-step row from summary, found {len(lines) - 1}"
        )
    header = lines[0].split()
    if tuple(header) != SUMMARY_VECTORS:
        raise ValueError(f"unexpected summary vectors: {header}")
    rows: list[dict[str, float]] = []
    for line in lines[1:]:
        values = line.split()
        if len(values) != len(header):
            raise ValueError(f"summary value count does not match header: {line}")
        parsed = {name: float(value) for name, value in zip(header, values, strict=True)}
        non_finite = {name: value for name, value in parsed.items() if not math.isfinite(value)}
        if non_finite:
            raise ValueError(f"summary contains non-finite values: {non_finite}")
        rows.append(parsed)
    return rows


def extract_summary(summary_executable: str, smspec: Path) -> tuple[list[dict[str, float]], str]:
    completed = run_checked(
        [summary_executable, "-r", str(smspec), *SUMMARY_VECTORS],
        cwd=smspec.parent,
    )
    return parse_summary(completed.stdout), completed.stdout


def stage_restart_files(previous_year_dir: Path, year_dir: Path, restart_case: str) -> None:
    for suffix in (".UNRST", ".EGRID"):
        source = previous_year_dir / "flow-run" / f"{restart_case}{suffix}"
        if not source.is_file():
            raise FileNotFoundError(
                f"previous year output missing restart artifact: {source}"
            )
        shutil.copy2(source, year_dir / f"{restart_case}{suffix}")


def run_year(
    year: int,
    constraints: dict[str, float],
    year_dir: Path,
    flow_executable: str,
    summary_executable: str,
    restart_case: str | None,
    restart_step: int | None = None,
) -> dict[str, Any]:
    year_dir.mkdir(parents=True, exist_ok=True)
    deck_name = f"MODEL_N_{year}.DATA"
    deck_path = year_dir / deck_name
    replacements = {
        "__START_YEAR__": str(year),
        "__N_P1_BHP_BAR__": f"{constraints['N-P1']:.6f}",
        "__N_P2_BHP_BAR__": f"{constraints['N-P2']:.6f}",
        "__YEAR_DAYS__": str(days_in_year(year)),
    }
    if restart_case is None:
        render_deck(deck_path, BASE_TEMPLATE, replacements)
    else:
        if restart_step is None:
            raise ValueError("restart_step is required for a continuation run")
        replacements["__RESTART_CASE__"] = restart_case
        replacements["__RESTART_STEP__"] = str(restart_step)
        render_deck(deck_path, CONTINUE_TEMPLATE, replacements)
        stage_restart_files(year_dir.parent / f"year-{year - 1}", year_dir, restart_case)

    flow_dir = year_dir / "flow-run"
    flow_command = [
        flow_executable,
        str(deck_path),
        f"--output-dir={flow_dir}",
        "--enable-terminal-output=false",
        "--output-mode=log",
    ]
    flow_completed = run_checked(flow_command, cwd=year_dir)
    (year_dir / "flow.stdout.log").write_text(flow_completed.stdout, encoding="utf-8")
    (year_dir / "flow.stderr.log").write_text(flow_completed.stderr, encoding="utf-8")

    smspec = flow_dir / f"{deck_name[:-5]}.SMSPEC"
    unsmry = flow_dir / f"{deck_name[:-5]}.UNSMRY"
    if not smspec.is_file() or not unsmry.is_file():
        raise FileNotFoundError(f"Flow did not create required summary artifacts below {flow_dir}")
    rows, summary_text = extract_summary(summary_executable, smspec)
    (year_dir / "summary.txt").write_text(summary_text, encoding="utf-8")

    end = rows[-1]
    results = {
        "year": year,
        "restart_case": restart_case,
        "report_steps": len(rows),
        "constraints": constraints,
        "wells": {
            well: {
                "wopr_sm3d": end[f"WOPR:{well}"],
                "wwpr_sm3d": end[f"WWPR:{well}"],
                "wbhp_bar": end[f"WBHP:{well}"],
                "q_liq_sm3d": end[f"WOPR:{well}"] + end[f"WWPR:{well}"],
            }
            for well in EXPECTED_WELLS
        },
        "field": {
            "fopt_sm3": end["FOPT"],
            "fwpt_sm3": end["FWPT"],
            "fpr_bar": end["FPR"],
        },
    }
    return results


def run_chain(
    constraints_path: Path,
    output_dir: Path,
    flow_name: str,
    summary_name: str,
) -> Path:
    constraints = read_constraints_chain(constraints_path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    flow_executable = require_executable(flow_name)
    summary_executable = require_executable(summary_name)

    version = run_checked([flow_executable, "--version"], cwd=output_dir).stdout.strip()

    year_results: list[dict[str, Any]] = []
    for index, year in enumerate(EXPECTED_YEARS):
        restart_case = None if index == 0 else f"MODEL_N_{EXPECTED_YEARS[index - 1]}"
        year_dir = output_dir / f"year-{year}"
        year_results.append(
            run_year(
                year,
                constraints[year],
                year_dir,
                flow_executable,
                summary_executable,
                restart_case,
                restart_step=index if index > 0 else None,
            )
        )

    checks: dict[str, Any] = {"fopt_strictly_increasing": True}
    annual_increments: list[dict[str, float]] = []
    for previous, current in zip(year_results, year_results[1:]):
        increment = current["field"]["fopt_sm3"] - previous["field"]["fopt_sm3"]
        annual_increments.append(
            {
                "year": current["year"],
                "fopt_increment_sm3": increment,
                "fwpt_increment_sm3": current["field"]["fwpt_sm3"] - previous["field"]["fwpt_sm3"],
            }
        )
        if increment <= 0.0:
            checks["fopt_strictly_increasing"] = False
            checks["fopt_gap"] = {
                "previous_year": previous["year"],
                "current_year": current["year"],
                "previous_fopt": previous["field"]["fopt_sm3"],
                "current_fopt": current["field"]["fopt_sm3"],
            }
            break
    checks["annual_increments"] = annual_increments
    checks["fwpt_non_decreasing"] = all(
        item["fwpt_increment_sm3"] >= 0.0 for item in annual_increments
    )

    wbhp_mismatches: list[dict[str, Any]] = []
    rate_issues: list[dict[str, Any]] = []
    for result in year_results:
        for well, values in result["wells"].items():
            expected = result["constraints"][well]
            if not math.isclose(values["wbhp_bar"], expected, abs_tol=1.0e-3):
                wbhp_mismatches.append(
                    {
                        "year": result["year"],
                        "well": well,
                        "expected_bar": expected,
                        "flow_bar": values["wbhp_bar"],
                    }
                )
            if values["q_liq_sm3d"] <= 0.0 or not math.isfinite(values["q_liq_sm3d"]):
                rate_issues.append(
                    {
                        "year": result["year"],
                        "well": well,
                        "q_liq_sm3d": values["q_liq_sm3d"],
                    }
                )
    checks["wbhp_matches_constraint"] = not wbhp_mismatches
    if wbhp_mismatches:
        checks["wbhp_mismatches"] = wbhp_mismatches
    checks["rates_positive"] = not rate_issues
    if rate_issues:
        checks["rate_issues"] = rate_issues

    rates_path = output_dir / "slave_rates_model_n.csv"
    rows: list[dict[str, Any]] = []
    for result in year_results:
        reservoir_pressure = round(result["field"]["fpr_bar"], 6)
        for well in EXPECTED_WELLS:
            values = result["wells"][well]
            liquid_rate = round(values["q_liq_sm3d"], 6)
            rows.append(
                {
                    "well": well,
                    "year": result["year"],
                    "q_liq_sm3d": liquid_rate,
                    "q_gas_sm3d": 0.0,
                    "p_bhp_bar": round(values["wbhp_bar"], 6),
                    "p_res_bar": reservoir_pressure,
                    "q_ipr_sm3d": liquid_rate,
                    "backpressure_limited": 1,
                    "origin": "opm_flow_restart",
                }
            )
    with rates_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "simulator": version,
        "extractor": Path(summary_executable).name,
        "years": list(EXPECTED_YEARS),
        "constraints": constraints,
        "year_results": year_results,
        "checks": checks,
        "artifacts": {
            "rates": str(rates_path),
            "years": {
                str(year): {
                    "deck": str(output_dir / f"year-{year}" / f"MODEL_N_{year}.DATA"),
                    "smspec": str(
                        output_dir / f"year-{year}" / "flow-run" / f"MODEL_N_{year}.SMSPEC"
                    ),
                    "unsmry": str(
                        output_dir / f"year-{year}" / "flow-run" / f"MODEL_N_{year}.UNSMRY"
                    ),
                    "unrst": str(
                        output_dir / f"year-{year}" / "flow-run" / f"MODEL_N_{year}.UNRST"
                    ),
                }
                for year in EXPECTED_YEARS
            },
        },
    }
    (output_dir / "restart_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return rates_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--flow", default="flow", help="Flow executable name or path")
    parser.add_argument("--summary", default="summary", help="OPM summary executable name or path")
    args = parser.parse_args()
    rates_path = run_chain(
        args.constraints.resolve(),
        args.output_dir.resolve(),
        args.flow,
        args.summary,
    )
    print(f"OPM MODEL_N RESTART CHAIN COMPLETE: {rates_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
