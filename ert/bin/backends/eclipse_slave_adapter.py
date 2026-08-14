#!/usr/bin/env python3
"""Run a stateful three-year Eclipse chain for a coupled slave model.

Same exchange contract as the Spike 003 OPM Flow restart adapter, but the
simulator is the proprietary Eclipse executable driven through Equinor's
`eclrun` launcher. Each year is one Eclipse run: the first year starts fresh
from EQUIL, later years continue from the previous year's unified restart
file via the Eclipse RESTART keyword. Year-end WOPR/WWPR/WBHP/FPR/FOPT/FWPT
are extracted with OPM's `summary -r` CLI (which reads Eclipse
SMSPEC/UNSMRY files natively) and emitted in the slave_rates_<model>.csv
exchange schema, plus a restart_report.json with the statefulness checks.

The deck templates are Eclipse-standard syntax (same skeleton the Spike 003
Flow chain renders); swap them for your FMU deck conventions when the real
models arrive.

Mock-testable without a licence: --eclrun and --summary accept any
executable, so the test suite drives this backend with scripted stand-ins
(tests/test_eclipse_backend.py). The driver resolves the same seams from
coupling.json ("eclrun"/"summary" per slave).
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import shutil
from itertools import pairwise
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESTART_ADAPTER = (
    HERE.parents[2] / "spikes" / "003-opm-model-n-restart" / "opm_model_n_restart_adapter.py"
)
BASE_TEMPLATE = HERE / "ECLIPSE_BASE.DATA.tmpl"
CONTINUE_TEMPLATE = HERE / "ECLIPSE_CONTINUE.DATA.tmpl"


def load_restart_module():
    """Load the Spike 003 restart adapter for its generic contract helpers."""
    spec = importlib.util.spec_from_file_location("opm_model_n_restart_adapter", RESTART_ADAPTER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import restart adapter from {RESTART_ADAPTER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASE = load_restart_module()

EXPECTED_YEARS = BASE.EXPECTED_YEARS
MODEL_CONFIGS = BASE.MODEL_CONFIGS


def stage_restart_files(previous_year_dir: Path, year_dir: Path, restart_case: str) -> None:
    for suffix in (".UNRST", ".EGRID"):
        source = previous_year_dir / "eclipse-run" / f"{restart_case}{suffix}"
        if not source.is_file():
            raise FileNotFoundError(
                f"previous year output missing restart artifact: {source}"
            )
        shutil.copy2(source, year_dir / f"{restart_case}{suffix}")


def run_year(
    deck_stem: str,
    wells: tuple[str, ...],
    initial_pressure_bar: float,
    year: int,
    constraints: dict[str, float],
    year_dir: Path,
    eclrun_executable: str,
    summary_executable: str,
    restart_case: str | None,
    restart_step: int | None = None,
) -> dict[str, Any]:
    year_dir.mkdir(parents=True, exist_ok=True)
    deck_name = f"{deck_stem}_{year}.DATA"
    deck_path = year_dir / deck_name
    replacements = {
        "__START_YEAR__": str(year),
        "__WELL_1__": wells[0],
        "__WELL_2__": wells[1],
        "__WELL_1_BHP_BAR__": f"{constraints[wells[0]]:.6f}",
        "__WELL_2_BHP_BAR__": f"{constraints[wells[1]]:.6f}",
        "__YEAR_DAYS__": str(BASE.days_in_year(year)),
    }
    if restart_case is None:
        replacements["__INITIAL_PRESSURE_BAR__"] = f"{initial_pressure_bar:.6f}"
        BASE.render_deck(deck_path, BASE_TEMPLATE, replacements)
    else:
        if restart_step is None:
            raise ValueError("restart_step is required for a continuation run")
        replacements["__RESTART_CASE__"] = restart_case
        replacements["__RESTART_STEP__"] = str(restart_step)
        BASE.render_deck(deck_path, CONTINUE_TEMPLATE, replacements)
        stage_restart_files(year_dir.parent / f"year-{year - 1}", year_dir, restart_case)

    # Eclipse writes its output files next to the case (cwd); keep them in
    # an eclipse-run/ subdir per year, mirroring the flow backend layout.
    run_dir = year_dir / "eclipse-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    completed = BASE.run_checked([eclrun_executable, str(deck_path)], cwd=run_dir)
    (year_dir / "eclipse.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (year_dir / "eclipse.stderr.log").write_text(completed.stderr, encoding="utf-8")

    smspec = run_dir / f"{deck_name[:-5]}.SMSPEC"
    unsmry = run_dir / f"{deck_name[:-5]}.UNSMRY"
    if not smspec.is_file() or not unsmry.is_file():
        raise FileNotFoundError(
            f"Eclipse did not create required summary artifacts below {run_dir}"
        )
    rows, summary_text = BASE.extract_summary(summary_executable, smspec, BASE.summary_vectors(wells))
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
            for well in wells
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
    eclrun_name: str,
    summary_name: str,
    model: str = "model_a",
) -> Path:
    if model not in MODEL_CONFIGS:
        raise ValueError(f"unsupported model {model!r}; expected {sorted(MODEL_CONFIGS)}")
    config = MODEL_CONFIGS[model]
    wells = config["wells"]
    initial_pressure_bar = config["initial_pressure_bar"]
    deck_stem = config["deck_stem"]
    constraints = BASE.read_constraints_chain(constraints_path, wells, initial_pressure_bar)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    eclrun_executable = BASE.require_executable(eclrun_name)
    summary_executable = BASE.require_executable(summary_name)

    try:
        version = BASE.run_checked([eclrun_executable, "--version"], cwd=output_dir).stdout.strip()
    except RuntimeError:
        # `eclrun --version` is not part of the launcher's contract; a
        # failed probe must not fail the chain.
        version = "eclrun (version probe unavailable)"

    year_results: list[dict[str, Any]] = []
    for index, year in enumerate(EXPECTED_YEARS):
        restart_case = None if index == 0 else f"{deck_stem}_{EXPECTED_YEARS[index - 1]}"
        year_dir = output_dir / f"year-{year}"
        year_results.append(
            run_year(
                deck_stem,
                wells,
                initial_pressure_bar,
                year,
                constraints[year],
                year_dir,
                eclrun_executable,
                summary_executable,
                restart_case,
                restart_step=index if index > 0 else None,
            )
        )

    checks: dict[str, Any] = {"fopt_strictly_increasing": True}
    annual_increments: list[dict[str, float]] = []
    for previous, current in pairwise(year_results):
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
                        "eclipse_bar": values["wbhp_bar"],
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

    rates_path = output_dir / f"slave_rates_{model}.csv"
    rows: list[dict[str, Any]] = []
    for result in year_results:
        reservoir_pressure = round(result["field"]["fpr_bar"], 6)
        for well in wells:
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
                    "origin": "eclipse_restart",
                }
            )
    with rates_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = {
        "simulator": version,
        "extractor": Path(summary_executable).name,
        "model": model,
        "wells": list(wells),
        "years": list(EXPECTED_YEARS),
        "constraints": constraints,
        "year_results": year_results,
        "checks": checks,
        "artifacts": {
            "rates": str(rates_path),
            "years": {
                str(year): {
                    "deck": str(output_dir / f"year-{year}" / f"{deck_stem}_{year}.DATA"),
                    "smspec": str(
                        output_dir / f"year-{year}" / "eclipse-run" / f"{deck_stem}_{year}.SMSPEC"
                    ),
                    "unsmry": str(
                        output_dir / f"year-{year}" / "eclipse-run" / f"{deck_stem}_{year}.UNSMRY"
                    ),
                    "unrst": str(
                        output_dir / f"year-{year}" / "eclipse-run" / f"{deck_stem}_{year}.UNRST"
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
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_CONFIGS),
        default="model_a",
        help="slave model whose exchange contract this chain fills",
    )
    parser.add_argument("--eclrun", default="eclrun", help="eclrun launcher name or path")
    parser.add_argument("--summary", default="summary", help="OPM summary executable name or path")
    args = parser.parse_args()
    rates_path = run_chain(
        args.constraints.resolve(),
        args.output_dir.resolve(),
        args.eclrun,
        args.summary,
        model=args.model,
    )
    print(f"ECLIPSE {args.model.upper()} RESTART CHAIN COMPLETE: {rates_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
