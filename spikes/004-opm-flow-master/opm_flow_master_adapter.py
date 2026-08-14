#!/usr/bin/env python3
"""Run the real OPM Flow network master for one coupling iteration.

Reads the slaves' latest rates (slave_rates_model_a.csv / model_b.csv), renders
the 4-well NETWORK master deck, runs Flow, and writes pressure constraints back
to both slaves. A prescribed GSATPROD profile remains optional for legacy
external-source studies; the primary two-way mode has no prescribed profile.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "MASTER_FLOW.DATA.tmpl"
TWOWAY_TEMPLATE = HERE / "MASTER_FLOW_TWOWAY.DATA.tmpl"
VFP_INC = HERE / "vfp_tables_master.inc"
ROOT = HERE.parents[1]
DUMMY = ROOT / "bin" / "eclipse_dummy.py"

MASTER_WELLS = ("A-P1", "A-P2", "B-P1", "B-P2")
SLAVE_MODELS = ("model_a", "model_b")
YEARS = (2024, 2025, 2026)
TWOWAY_SUMMARY_VECTORS = (
    *tuple(f"WOPR:{well}" for well in MASTER_WELLS),
    *tuple(f"WWPR:{well}" for well in MASTER_WELLS),
    *tuple(f"WBHP:{well}" for well in MASTER_WELLS),
    *tuple(f"WTHP:{well}" for well in MASTER_WELLS),
    "GPR:PLAT",
    "GPR:MANIFOLD",
)
PROFILE_SUMMARY_VECTORS = (*TWOWAY_SUMMARY_VECTORS, "GOPR:SAT")
GRAVITY = 9.81  # m/s2


def load_master_spec(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"master simspec does not exist: {path}")
    spec = json.loads(path.read_text(encoding="utf-8"))
    network = spec.get("network", {})
    density = float(network.get("fluid_density_kg_m3", 850.0))
    if not math.isfinite(density) or density <= 0.0:
        raise ValueError(f"master simspec has invalid fluid density: {density}")
    tvd: dict[str, float] = {}
    well_model: dict[str, str] = {}
    for well in spec.get("wells", []):
        name = well.get("name")
        depth = well.get("tvd_m")
        if name is None or depth is None:
            raise ValueError(f"master simspec has invalid well TVD: {well}")
        try:
            depth_value = float(depth)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"master simspec has invalid well TVD: {well}") from exc
        if not math.isfinite(depth_value) or depth_value <= 0.0:
            raise ValueError(f"master simspec has invalid well TVD: {well}")
        tvd[str(name)] = depth_value
        slave = well.get("slave")
        if slave is None:
            raise ValueError(f"master simspec well is missing its slave model: {well}")
        well_model[str(name)] = str(slave)
    missing = [well for well in MASTER_WELLS if well not in tvd]
    if missing:
        raise ValueError(f"master simspec is missing TVD for wells: {missing}")
    extra = sorted(set(tvd) - set(MASTER_WELLS))
    if extra:
        raise ValueError(f"master simspec lists wells outside the master deck: {extra}")
    assigned = set(well_model.values())
    if assigned != set(SLAVE_MODELS):
        raise ValueError(
            f"master simspec slave map does not match the configured slaves: "
            f"spec={sorted(assigned)}, expected={sorted(SLAVE_MODELS)}"
        )
    return {"density_kg_m3": density, "tvd_m": tvd, "well_model": well_model}


def hydrostatic_bar(density_kg_m3: float, tvd_m: float) -> float:
    return density_kg_m3 * GRAVITY * tvd_m * 1e-5


def load_dummy_parser():
    spec = importlib.util.spec_from_file_location("eclipse_dummy", DUMMY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import dummy parser from {DUMMY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise FileNotFoundError(f"required executable is not on PATH: {name}")
    return str(Path(executable).resolve())


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


def read_slave_rates(path: Path) -> dict[tuple[str, int], float]:
    if not path.is_file():
        raise FileNotFoundError(f"slave rates file does not exist: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"slave rates file is empty: {path}")
    rates: dict[tuple[str, int], float] = {}
    for row in rows:
        try:
            well = row["well"]
            year = int(row["year"])
            rate = float(row["q_liq_sm3d"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"malformed slave rates row in {path}: {row}") from exc
        if not math.isfinite(rate) or rate < 0.0:
            raise ValueError(f"non-finite or negative slave rate in {path}: {row}")
        if (well, year) in rates:
            raise ValueError(f"duplicate slave rates row in {path}: {well}/{year}")
        rates[(well, year)] = rate
    return rates


def profile_totals(rows: list[dict[str, Any]], years: tuple[int, ...]) -> dict[int, dict[str, float]]:
    totals = {year: {"oil": 0.0, "gas": 0.0} for year in years}
    for row in rows:
        year = int(row["year"])
        if year not in totals:
            raise ValueError(f"prescribed profile has unexpected year {year}")
        totals[year]["oil"] += float(row["q_liq_sm3d"])
        totals[year]["gas"] += float(row["q_gas_sm3d"])
    for year in years:
        if totals[year]["oil"] < 0.0 or totals[year]["gas"] < 0.0:
            raise ValueError(f"negative prescribed rates for year {year}")
    return totals


def render_deck(
    destination: Path,
    rates: dict[str, dict[tuple[str, int], float]],
    prescribed: dict[int, dict[str, float]] | None,
    well_model: dict[str, str],
) -> None:
    template = TEMPLATE if prescribed is not None else TWOWAY_TEMPLATE
    rendered = template.read_text(encoding="utf-8")
    replacements: dict[str, str] = {}
    for index, year in enumerate(YEARS):
        year_key = f"Y{index + 1}"
        if prescribed is not None:
            replacements[f"__{year_key}_SAT_OIL_SM3D__"] = (
                f"{prescribed[year]['oil']:.6f}"
            )
            replacements[f"__{year_key}_SAT_GAS_SM3D__"] = (
                f"{prescribed[year]['gas']:.6f}"
            )
        for well in MASTER_WELLS:
            key = f"__{year_key}_{well.replace('-', '_')}_ORAT__"
            replacements[key] = f"{rates[well_model[well]][(well, year)]:.6f}"
    for marker, value in replacements.items():
        if rendered.count(marker) != 1:
            raise ValueError(f"template must contain exactly one {marker}")
        rendered = rendered.replace(marker, value)
    if "__" in rendered:
        raise ValueError("unresolved template token remains in master deck")
    destination.write_text(rendered, encoding="utf-8")


def parse_summary(text: str, vectors: tuple[str, ...]) -> list[dict[str, float]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(
            f"expected at least one report-step row from summary, found {len(lines) - 1}"
        )
    header = lines[0].split()
    if tuple(header) != vectors:
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
    if len(rows) != len(YEARS):
        raise ValueError(f"expected {len(YEARS)} report-step rows, found {len(rows)}")
    return rows


def extract_summary(
    summary_executable: str, smspec: Path, vectors: tuple[str, ...]
) -> tuple[list[dict[str, float]], str]:
    completed = run_checked(
        [summary_executable, "-r", str(smspec), *vectors],
        cwd=smspec.parent,
    )
    return parse_summary(completed.stdout, vectors), completed.stdout


def write_constraints(
    path: Path,
    model: str,
    wells: tuple[str, ...],
    years: tuple[int, ...],
    rates: dict[tuple[str, int], float],
    prescribed: dict[int, dict[str, float]],
    simulated_by_year: dict[int, float],
    row_by_well_year: dict[tuple[str, int], dict[str, float]],
    master_spec: dict[str, Any],
) -> list[dict[str, Any]]:
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
    records: list[dict[str, Any]] = []
    for well in wells:
        hydrostatic = hydrostatic_bar(master_spec["density_kg_m3"], master_spec["tvd_m"][well])
        for year in years:
            simulated = simulated_by_year[year]
            total = prescribed[year]["oil"] + simulated
            summary_row = row_by_well_year[(well, year)]
            node_pressure = summary_row["GPR:PLAT"]
            record = {
                "well": well,
                "year": year,
                "network_input_q_liq_sm3d": round(rates[(well, year)], 6),
                "prescribed_q_liq_sm3d": round(prescribed[year]["oil"], 6),
                "simulated_q_liq_sm3d": round(simulated, 6),
                "total_network_q_liq_sm3d": round(total, 6),
                "p_manifold_bar": round(summary_row["GPR:MANIFOLD"], 6),
                # The network node pressure at the platform is the wellhead in
                # the network sense; the slave constraint is the sandface BHP
                # = node pressure + wellbore hydrostatic (as in the dummy).
                "p_wh_bar": round(node_pressure, 6),
                "p_bhp_bar": round(node_pressure + hydrostatic, 6),
            }
            records.append(record)
            rows.append([record[column] for column in header])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return records


def execute(
    rates: dict[str, Path],
    profile: Path | None = None,
    output_dir: Path | None = None,
    flow_name: str = "flow",
    summary_name: str = "summary",
    simspec: Path | None = None,
) -> dict[str, Any]:
    dummy = load_dummy_parser()
    master_spec = load_master_spec(simspec or (ROOT / "input" / "master_network" / "simspec.json"))
    well_model = master_spec["well_model"]
    parsed_rates: dict[str, dict[tuple[str, int], float]] = {}
    for model in SLAVE_MODELS:
        if model not in rates:
            raise ValueError(f"no rates file supplied for slave {model}")
        parsed_rates[model] = read_slave_rates(rates[model])
    for model in SLAVE_MODELS:
        missing = [
            (well, year)
            for well in MASTER_WELLS
            if well_model[well] == model
            for year in YEARS
            if (well, year) not in parsed_rates[model]
        ]
        if missing:
            raise ValueError(f"slave rates for {model} are missing coverage: {missing[:5]}...")
    if profile is None:
        prescribed = {year: {"oil": 0.0, "gas": 0.0} for year in YEARS}
        rendered_profile = None
        summary_vectors = TWOWAY_SUMMARY_VECTORS
    else:
        profile_rows = dummy.parse_gsatprod_inc(profile)
        prescribed = profile_totals(profile_rows, YEARS)
        rendered_profile = prescribed
        summary_vectors = PROFILE_SUMMARY_VECTORS

    flow_executable = require_executable(flow_name)
    summary_executable = require_executable(summary_name)
    if output_dir is None:
        raise ValueError("output_dir is required")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    version = run_checked([flow_executable, "--version"], cwd=output_dir).stdout.strip()
    shutil.copy2(VFP_INC, output_dir / VFP_INC.name)
    deck_path = output_dir / "MASTER_FLOW.DATA"
    render_deck(deck_path, parsed_rates, rendered_profile, well_model)
    flow_dir = output_dir / "flow-run"
    flow_completed = run_checked(
        [
            flow_executable,
            str(deck_path),
            f"--output-dir={flow_dir}",
        ],
        cwd=output_dir,
    )
    (output_dir / "flow.stdout.log").write_text(flow_completed.stdout, encoding="utf-8")
    (output_dir / "flow.stderr.log").write_text(flow_completed.stderr, encoding="utf-8")

    smspec = flow_dir / "MASTER_FLOW.SMSPEC"
    if not smspec.is_file():
        raise FileNotFoundError(f"Flow did not create the master summary: {smspec}")
    summary_rows, summary_text = extract_summary(summary_executable, smspec, summary_vectors)
    (output_dir / "summary.txt").write_text(summary_text, encoding="utf-8")

    row_by_well_year: dict[tuple[str, int], dict[str, float]] = {}
    delivered: dict[tuple[str, int], float] = {}
    for year, summary_row in zip(YEARS, summary_rows, strict=True):
        for well in MASTER_WELLS:
            row_by_well_year[(well, year)] = summary_row
            delivered[(well, year)] = summary_row[f"WOPR:{well}"] + summary_row[f"WWPR:{well}"]
    simulated_by_year = {
        year: sum(
            parsed_rates[model][(well, year)]
            for model in SLAVE_MODELS
            for well in MASTER_WELLS
            if well_model[well] == model
        )
        for year in YEARS
    }

    constraints: dict[str, list[dict[str, Any]]] = {}
    for model in SLAVE_MODELS:
        wells = tuple(well for well in MASTER_WELLS if well_model[well] == model)
        constraints[model] = write_constraints(
            output_dir / f"network_constraints_{model}.csv",
            model,
            wells,
            YEARS,
            parsed_rates[model],
            prescribed,
            simulated_by_year,
            row_by_well_year,
            master_spec,
        )

    cutbacks = [
        {
            "well": well,
            "year": year,
            "requested_sm3d": parsed_rates[model][(well, year)],
            "delivered_sm3d": delivered[(well, year)],
        }
        for model in SLAVE_MODELS
        for well in MASTER_WELLS
        if well_model[well] == model
        for year in YEARS
        if delivered[(well, year)] < parsed_rates[model][(well, year)] - 1.0e-3
    ]
    checks = {
        "wells_deliver_requested_rates": not cutbacks,
        "cutbacks": cutbacks,
        "external_profile_used": profile is not None,
        "satellite_registers_in_group_totals": (
            all(
                math.isclose(
                    summary_rows[index]["GOPR:SAT"],
                    prescribed[year]["oil"],
                    abs_tol=1.0e-3,
                )
                for index, year in enumerate(YEARS)
            )
            if profile is not None
            else None
        ),
        "manifold_pressure_by_year": {
            str(year): round(summary_rows[index]["GPR:MANIFOLD"], 6)
            for index, year in enumerate(YEARS)
        },
    }

    report = {
        "simulator": version,
        "extractor": Path(summary_executable).name,
        "years": list(YEARS),
        "prescribed_profile": (
            {str(year): prescribed[year] for year in YEARS}
            if profile is not None
            else {}
        ),
        "requested_rates": {
            model: {
                f"{well}/{year}": parsed_rates[model][(well, year)]
                for well in MASTER_WELLS
                if well_model[well] == model
                for year in YEARS
            }
            for model in SLAVE_MODELS
        },
        "delivered_rates": {
            f"{well}/{year}": delivered[(well, year)]
            for well in MASTER_WELLS
            for year in YEARS
        },
        "constraints": constraints,
        "checks": checks,
        "artifacts": {
            "deck": str(deck_path),
            "smspec": str(smspec),
            **{
                f"constraints_{model}": str(output_dir / f"network_constraints_{model}.csv")
                for model in SLAVE_MODELS
            },
        },
    }
    (output_dir / "master_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rates",
        nargs=2,
        action="append",
        metavar=("MODEL", "PATH"),
        required=True,
        help="slave rates CSV for one coupled slave (repeatable)",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="optional prescribed GSATPROD include; omit for fully two-way mode",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--flow", default="flow", help="Flow executable name or path")
    parser.add_argument("--summary", default="summary", help="OPM summary executable name or path")
    parser.add_argument(
        "--simspec",
        type=Path,
        default=None,
        help="master simspec with well TVDs and fluid density (default: input/master_network)",
    )
    args = parser.parse_args()
    rates = {model: Path(path).resolve() for model, path in args.rates}
    report = execute(
        rates,
        args.profile.resolve() if args.profile is not None else None,
        args.output_dir.resolve(),
        args.flow,
        args.summary,
        simspec=args.simspec,
    )
    print(json.dumps(report["checks"], indent=2, allow_nan=False))
    print(f"OPM FLOW MASTER COMPLETE: {args.output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
