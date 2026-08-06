#!/usr/bin/env python3
"""Licence-free stand-in for the master network and two reservoir slaves.

The exchange directions are intentionally explicit:

* prescribed GSATPROD rows are immutable inputs to ``master_network``;
* ``model_n`` and ``model_hdn`` write simulated rates to the network;
* the network combines all three source categories and returns calculated
  pressure constraints to each simulated slave.

The ``.DATA`` decks are documentation scaffolds only. This dummy consumes the
machine-readable ``simspec.json`` and CSV/JSON exchange artifacts so the
coupling topology can be exercised without an Eclipse licence.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any

GRAVITY = 9.81  # m/s2


def log(model: str, message: str) -> None:
    print(f"[dummy:{model}] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"required coupling artifact is missing: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"required coupling artifact is empty: {path}")
    return rows


def schedule_years(coupling: dict[str, Any]) -> list[int]:
    schedule = coupling["schedule"]
    if int(schedule.get("steps_per_year", 1)) != 1:
        raise ValueError("dummy example currently supports exactly one coupling step per year")
    first_year = date.fromisoformat(schedule["start"]).year
    years = int(schedule["years"])
    if years < 1:
        raise ValueError("schedule.years must be positive")
    return list(range(first_year, first_year + years))


def parse_gsatprod_inc(path: Path) -> list[dict[str, Any]]:
    """Parse the example's illustrative GSATPROD row convention strictly."""
    rows: list[dict[str, Any]] = []
    saw_header = False
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        if line == "GSATPROD":
            saw_header = True
            continue
        if line == "/":
            continue
        if not saw_header:
            raise ValueError(f"missing GSATPROD header before data at {path}:{line_number}")
        tokens = [token.strip("'\"") for token in line.split() if token != "/"]
        if len(tokens) != 7:
            raise ValueError(f"malformed GSATPROD example row at {path}:{line_number}: {raw}")
        try:
            rows.append(
                {
                    "well": tokens[0],
                    "year": int(tokens[1]),
                    "q_liq_sm3d": float(tokens[2]),
                    "q_gas_sm3d": float(tokens[3]),
                    "p_wh_bar": float(tokens[4]),
                    "p_bhp_bar": float(tokens[5]),
                    "gsat": float(tokens[6]),
                }
            )
        except ValueError as exc:
            raise ValueError(f"invalid GSATPROD value at {path}:{line_number}: {raw}") from exc
    if not saw_header:
        raise ValueError(f"missing GSATPROD header in {path}")
    if not rows:
        raise ValueError(f"no GSATPROD rows found in {path}")
    return rows


def validate_rows(
    rows: list[dict[str, Any]], expected_wells: set[str], expected_years: set[int], label: str
) -> None:
    keys = [(str(row["well"]), int(row["year"])) for row in rows]
    expected = {(well, year) for well in expected_wells for year in expected_years}
    actual = set(keys)
    if len(keys) != len(actual):
        raise ValueError(f"{label} contains duplicate well/year rows")
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        raise ValueError(f"{label} has incomplete coverage; missing={missing}, extra={extra}")
    for row in rows:
        rate = float(row["q_liq_sm3d"])
        if not math.isfinite(rate) or rate < 0.0:
            raise ValueError(
                f"{label} requires a finite non-negative liquid rate for "
                f"{row['well']}/{row['year']}: {rate}"
            )
        if "q_gas_sm3d" in row:
            gas_rate = float(row["q_gas_sm3d"])
            if not math.isfinite(gas_rate) or gas_rate < 0.0:
                raise ValueError(
                    f"{label} requires a finite non-negative gas rate for "
                    f"{row['well']}/{row['year']}: {gas_rate}"
                )


def validate_prescribed_profile_rows(
    rows: list[dict[str, Any]], expected_years: set[int], label: str
) -> None:
    wells = {str(row["well"]) for row in rows}
    if not wells:
        raise ValueError(f"prescribed profile {label} contains no wells")
    validate_rows(rows, wells, expected_years, f"prescribed profile {label}")
    for row in rows:
        for field in ("p_wh_bar", "p_bhp_bar", "gsat"):
            value = float(row[field])
            if not math.isfinite(value):
                raise ValueError(
                    f"prescribed profile {label} requires finite {field} for "
                    f"{row['well']}/{row['year']}: {value}"
                )
        if not 0.0 <= float(row["gsat"]) <= 1.0:
            raise ValueError(
                f"prescribed profile {label} requires GSAT in [0, 1] for "
                f"{row['well']}/{row['year']}"
            )


def slave_rate_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_csv(path):
        rows.append(
            {
                "well": row["well"],
                "year": int(row["year"]),
                "q_liq_sm3d": float(row["q_liq_sm3d"]),
                "q_gas_sm3d": float(row.get("q_gas_sm3d") or 0.0),
                "origin": row.get("origin", "simulation_output"),
            }
        )
    return rows


def staged_profile_path(model_dir: Path, master_model: str, configured_path: str) -> Path:
    source = Path(configured_path)
    prefix = Path("input") / master_model
    try:
        relative = source.relative_to(prefix)
    except ValueError as exc:
        raise ValueError(
            f"prescribed profile must live below input/{master_model}/ so it is staged with the master: {source}"
        ) from exc
    return model_dir / relative


def run_master(
    spec: dict[str, Any], model_dir: Path, coupling: dict[str, Any], iteration: int
) -> None:
    years = schedule_years(coupling)
    year_set = set(years)
    network = spec["network"]
    choke = float(network.get("choke", 1.0))
    if not math.isfinite(choke) or choke <= 0.0:
        raise ValueError(f"network choke must be a finite positive number: {choke}")
    coupling_dir = model_dir.parent / "coupling"
    exchange_dir = coupling_dir / "exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)

    by_slave: dict[str, list[dict[str, Any]]] = {}
    for well in spec["wells"]:
        by_slave.setdefault(well["slave"], []).append(well)
    configured_slaves = set(coupling["slaves"])
    if set(by_slave) != configured_slaves:
        raise ValueError(
            f"master well map does not match configured slaves: master={sorted(by_slave)}, config={sorted(configured_slaves)}"
        )

    profile_sources: dict[str, dict[str, Any]] = {}
    prescribed_by_year = {year: 0.0 for year in years}
    prescribed_wells: set[str] = set()
    for profile_name, profile_cfg in coupling["prescribed_network_profiles"].items():
        profile_path = staged_profile_path(model_dir, spec["model"], profile_cfg["path"])
        profile_rows = parse_gsatprod_inc(profile_path)
        validate_prescribed_profile_rows(profile_rows, year_set, profile_name)
        overlap = prescribed_wells & {str(row["well"]) for row in profile_rows}
        if overlap:
            raise ValueError(f"duplicate prescribed wells across profiles: {sorted(overlap)}")
        prescribed_wells.update(str(row["well"]) for row in profile_rows)
        for row in profile_rows:
            prescribed_by_year[int(row["year"])] += float(row["q_liq_sm3d"])
        profile_sources[profile_name] = {
            "keyword": profile_cfg["keyword"],
            "source_file": str(Path(profile_cfg["path"])),
            "rows": profile_rows,
        }

    simulated_sources: dict[str, list[dict[str, Any]]] = {}
    simulated_by_year = {year: 0.0 for year in years}
    dynamic_wells = {well["name"] for well in spec["wells"]}
    if prescribed_wells & dynamic_wells:
        raise ValueError(
            f"prescribed and simulated network sources overlap: {sorted(prescribed_wells & dynamic_wells)}"
        )

    rates_by_slave: dict[str, dict[tuple[str, int], float]] = {}
    for slave, wells in sorted(by_slave.items()):
        rows = slave_rate_rows(coupling_dir / f"slave_rates_{slave}.csv")
        expected_wells = {well["name"] for well in wells}
        validate_rows(rows, expected_wells, year_set, f"simulated rates for {slave}")
        simulated_sources[slave] = rows
        rates_by_slave[slave] = {
            (str(row["well"]), int(row["year"])): float(row["q_liq_sm3d"]) for row in rows
        }
        for row in rows:
            simulated_by_year[int(row["year"])] += float(row["q_liq_sm3d"])

    totals_by_year = []
    total_by_year: dict[int, float] = {}
    for year in years:
        total = prescribed_by_year[year] + simulated_by_year[year]
        total_by_year[year] = total
        totals_by_year.append(
            {
                "year": year,
                "prescribed_q_liq_sm3d": round(prescribed_by_year[year], 6),
                "simulated_q_liq_sm3d": round(simulated_by_year[year], 6),
                "network_q_liq_sm3d": round(total, 6),
            }
        )

    request = {
        "iteration": iteration,
        "sources": {
            "prescribed_profiles": profile_sources,
            "simulated_slaves": simulated_sources,
        },
        "totals_by_year": totals_by_year,
    }
    request_path = exchange_dir / f"network_request_iteration_{iteration:03d}.json"
    request_path.write_text(
        json.dumps(request, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )

    constraints_by_slave: dict[str, list[dict[str, Any]]] = {slave: [] for slave in by_slave}
    response_rows: list[dict[str, Any]] = []
    for year in years:
        total_rate = total_by_year[year]
        manifold_pressure = (
            float(network["outlet_pressure_bar"])
            + choke
            * (
                float(network["trunk_friction_a_bar_sm3d"]) * total_rate
                + float(network["trunk_friction_b_bar_sm3d2"]) * total_rate * total_rate
            )
        )
        for slave, wells in sorted(by_slave.items()):
            for well in wells:
                q_well = rates_by_slave[slave][(well["name"], year)]
                p_wh = (
                    manifold_pressure
                    + choke
                    * (
                        float(network["branch_friction_a_bar_sm3d"]) * q_well
                        + float(network["branch_friction_b_bar_sm3d2"]) * q_well * q_well
                    )
                )
                hydrostatic = float(network["fluid_density_kg_m3"]) * GRAVITY * float(well["tvd_m"]) * 1e-5
                p_bhp = p_wh + hydrostatic
                if not all(math.isfinite(value) for value in (manifold_pressure, p_wh, p_bhp)):
                    raise ValueError(
                        f"network calculation produced non-finite pressure for {well['name']}/{year}"
                    )
                row = {
                    "well": well["name"],
                    "year": year,
                    "network_input_q_liq_sm3d": round(q_well, 6),
                    "prescribed_q_liq_sm3d": round(prescribed_by_year[year], 6),
                    "simulated_q_liq_sm3d": round(simulated_by_year[year], 6),
                    "total_network_q_liq_sm3d": round(total_rate, 6),
                    "p_manifold_bar": round(manifold_pressure, 6),
                    "p_wh_bar": round(p_wh, 6),
                    "p_bhp_bar": round(p_bhp, 6),
                }
                constraints_by_slave[slave].append(row)
                response_rows.append({"slave": slave, **row})

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
    for slave, rows in constraints_by_slave.items():
        write_csv(
            coupling_dir / f"network_constraints_{slave}.csv",
            header,
            [[row[column] for column in header] for row in rows],
        )

    response = {"iteration": iteration, "constraints": response_rows}
    (exchange_dir / f"network_response_iteration_{iteration:03d}.json").write_text(
        json.dumps(response, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    report = [
        f"master_network report - iteration {iteration}",
        "network inputs:",
        f"  prescribed profiles: {', '.join(sorted(profile_sources))}",
        f"  simulated slaves: {', '.join(sorted(simulated_sources))}",
        "",
    ]
    for total in totals_by_year:
        report.append(
            "  {year}: prescribed={prescribed_q_liq_sm3d:.2f}, simulated={simulated_q_liq_sm3d:.2f}, "
            "total={network_q_liq_sm3d:.2f} sm3/d".format(**total)
        )
    (model_dir / "master_network_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    log(spec["model"], f"combined prescribed profiles and {len(simulated_sources)} simulated slaves")


def run_slave(
    spec: dict[str, Any], model_dir: Path, coupling: dict[str, Any], iteration: int
) -> None:
    years = schedule_years(coupling)
    constraints = read_csv(model_dir.parent / "coupling" / f"network_constraints_{spec['model']}.csv")
    typed_constraints: list[dict[str, Any]] = [
        {
            "well": row["well"],
            "year": int(row["year"]),
            "network_input_q_liq_sm3d": float(row["network_input_q_liq_sm3d"]),
            "prescribed_q_liq_sm3d": float(row["prescribed_q_liq_sm3d"]),
            "simulated_q_liq_sm3d": float(row["simulated_q_liq_sm3d"]),
            "total_network_q_liq_sm3d": float(row["total_network_q_liq_sm3d"]),
            "p_manifold_bar": float(row["p_manifold_bar"]),
            "p_wh_bar": float(row["p_wh_bar"]),
            "p_bhp_bar": float(row["p_bhp_bar"]),
        }
        for row in constraints
    ]
    numeric_constraint_fields = (
        "network_input_q_liq_sm3d",
        "prescribed_q_liq_sm3d",
        "simulated_q_liq_sm3d",
        "total_network_q_liq_sm3d",
        "p_manifold_bar",
        "p_wh_bar",
        "p_bhp_bar",
    )
    for constraint in typed_constraints:
        for field in numeric_constraint_fields:
            if not math.isfinite(float(constraint[field])):
                raise ValueError(
                    f"network constraints for {spec['model']} require finite {field} for "
                    f"{constraint['well']}/{constraint['year']}"
                )
    wells = {well["name"]: well for well in spec["wells"]}
    validate_rows(
        [
            {"well": row["well"], "year": row["year"], "q_liq_sm3d": row["network_input_q_liq_sm3d"]}
            for row in typed_constraints
        ],
        set(wells),
        set(years),
        f"network constraints for {spec['model']}",
    )
    relaxation = float(coupling["coupling"].get("relaxation", 1.0))
    if not 0.0 < relaxation <= 1.0:
        raise ValueError("coupling.relaxation must be in (0, 1]")

    pressure = {name: float(well["p_res0_bar"]) for name, well in wells.items()}
    cumulative = {name: 0.0 for name in wells}
    output_rows: list[list[Any]] = []
    result_rows: list[dict[str, Any]] = []
    report = [f"{spec['model']} report - iteration {iteration}", ""]
    for constraint in sorted(typed_constraints, key=lambda row: (row["well"], row["year"])):
        well = wells[constraint["well"]]
        p_res0 = float(well["p_res0_bar"])
        p_bhp0 = float(well["p_bhp0_bar"])
        q0 = float(well["q0_sm3d"])
        gor = float(well["gor_sm3_sm3"])
        depletion = float(well.get("depletion_bar_per_year", 3.0))
        if not all(math.isfinite(value) for value in (p_res0, p_bhp0, q0, gor, depletion)):
            raise ValueError(f"non-finite slave well specification for {constraint['well']}")
        if q0 < 0.0 or gor < 0.0 or depletion < 0.0:
            raise ValueError(f"negative q0, GOR, or depletion for {constraint['well']}")
        denominator = p_res0 - p_bhp0
        if denominator <= 0.0:
            raise ValueError(f"invalid IPR reference pressures for {constraint['well']}")
        productivity_index = q0 / denominator
        q_ipr = max(0.0, productivity_index * (pressure[constraint["well"]] - constraint["p_bhp_bar"]))
        q_previous = constraint["network_input_q_liq_sm3d"]
        q_output = max(0.0, q_previous + relaxation * (q_ipr - q_previous))
        gas_rate = q_output * gor
        if not all(math.isfinite(value) for value in (q_ipr, q_output, gas_rate)):
            raise ValueError(f"slave calculation produced non-finite rates for {constraint['well']}")
        backpressure_limited = int(q_ipr < q_previous - 1e-9)
        output_rows.append(
            [
                constraint["well"],
                constraint["year"],
                round(q_output, 6),
                round(gas_rate, 6),
                round(constraint["p_bhp_bar"], 6),
                round(pressure[constraint["well"]], 6),
                round(q_ipr, 6),
                backpressure_limited,
                "simulation_output",
            ]
        )
        result_rows.append(
            {
                "well": constraint["well"],
                "year": constraint["year"],
                "q_liq_sm3d": round(q_output, 6),
                "q_gas_sm3d": round(gas_rate, 6),
                "network_input_q_liq_sm3d": round(q_previous, 6),
                "p_bhp_bar": round(constraint["p_bhp_bar"], 6),
                "p_res_bar": round(pressure[constraint["well"]], 6),
                "q_ipr_sm3d": round(q_ipr, 6),
                "backpressure_limited": backpressure_limited,
            }
        )
        report.append(
            f"  {constraint['well']} {constraint['year']}: network input={q_previous:.2f}, "
            f"IPR={q_ipr:.2f}, relaxed output={q_output:.2f} sm3/d"
        )
        cumulative[constraint["well"]] += q_output * 365.0
        pressure[constraint["well"]] -= depletion + 0.5 * (
            cumulative[constraint["well"]] / 1.0e6
        )
        if not math.isfinite(pressure[constraint["well"]]):
            raise ValueError(f"non-finite reservoir pressure for {constraint['well']}")

    header = [
        "well",
        "year",
        "q_liq_sm3d",
        "q_gas_sm3d",
        "p_bhp_bar",
        "p_res_bar",
        "q_ipr_sm3d",
        "backpressure_limited",
        "origin",
    ]
    coupling_dir = model_dir.parent / "coupling"
    write_csv(coupling_dir / f"slave_rates_{spec['model']}.csv", header, output_rows)
    exchange_dir = coupling_dir / "exchange"
    (exchange_dir / f"slave_result_{spec['model']}_iteration_{iteration:03d}.json").write_text(
        json.dumps(
            {"iteration": iteration, "model": spec["model"], "rows": result_rows},
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (model_dir / f"slave_{spec['model']}_report.txt").write_text(
        "\n".join(report) + "\n", encoding="utf-8"
    )
    log(spec["model"], f"simulated {len(output_rows)} well-year rows")


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: eclipse_dummy.py <model> <model_dir> <iteration>")
    model, model_dir_text, iteration_text = sys.argv[1:]
    model_dir = Path(model_dir_text)
    iteration = int(iteration_text)
    spec = load_json(model_dir / "simspec.json")
    if spec["model"] != model:
        raise ValueError(f"model argument {model!r} does not match simspec model {spec['model']!r}")
    coupling = load_json(model_dir.parent / "coupling_config.json")
    if spec["role"] == "master":
        run_master(spec, model_dir, coupling, iteration)
    elif spec["role"] == "slave":
        run_slave(spec, model_dir, coupling, iteration)
    else:
        raise ValueError(f"unsupported model role: {spec['role']!r}")


if __name__ == "__main__":
    main()
