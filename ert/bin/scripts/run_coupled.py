#!/usr/bin/env python3
"""Run one ERT realization of the master-network/two-slave example.

For each coupling iteration the dummy workflow performs this data exchange:

1. ``master_network`` reads immutable prescribed GSATPROD profiles plus the
   latest simulated rates from both ``model_n`` and ``model_hdn``.
2. The master solves its illustrative shared network and writes pressure
   constraints for each slave.
3. Both reservoir slaves simulate against those constraints and return new
   rates to the network.
4. Iteration continues until all slave well/year rates converge.

The executable is installed as one ERT forward-model job. It can also be run
standalone with ``--demo`` without ERT or an Eclipse licence.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


def find_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "coupling.json").exists():
            return candidate
    raise SystemExit(f"cannot locate coupling.json from {here}")


ROOT = find_root()
DEFAULT_CONFIG = ROOT / "coupling.json"
FLOW_ADAPTER = (
    ROOT / "spikes" / "003-opm-model-n-restart" / "opm_model_n_restart_adapter.py"
)
ALLOWED_BACKENDS = ("dummy", "flow")


def log(message: str) -> None:
    print(f"[run_coupled] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def schedule_years(coupling: dict[str, Any]) -> list[int]:
    schedule = coupling["schedule"]
    if int(schedule.get("steps_per_year", 1)) != 1:
        raise ValueError("dummy example currently supports exactly one coupling step per year")
    first_year = date.fromisoformat(schedule["start"]).year
    count = int(schedule["years"])
    if count < 1:
        raise ValueError("schedule.years must be positive")
    return list(range(first_year, first_year + count))


def parse_q0_mult(runpath: Path, *, allow_default: bool) -> float:
    """Read ERT's GEN_KW result; explicit demo mode may default to 1.0."""
    path = runpath / "q0_mult.txt"
    if not path.exists():
        if allow_default:
            return 1.0
        raise FileNotFoundError(f"required ERT parameter file is missing: {path}")
    values: list[float] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        try:
            values.append(float(line.split()[-1]))
        except ValueError as exc:
            raise ValueError(f"invalid Q0_MULT result row in {path}: {raw}") from exc
    if len(values) != 1 or not math.isfinite(values[0]) or values[0] <= 0.0:
        raise ValueError(
            f"expected exactly one finite positive Q0_MULT value in {path}, found {values}"
        )
    return values[0]


def stage_model(runpath: Path, model: str) -> None:
    source = ROOT / "input" / model
    if not source.is_dir():
        raise FileNotFoundError(f"model input directory does not exist: {source}")
    destination = runpath / model
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    log(f"staged {model} -> {destination}")


def apply_q0_mult(runpath: Path, model: str, multiplier: float) -> None:
    path = runpath / model / "simspec.json"
    spec = load_json(path)
    if spec.get("role") != "slave":
        raise ValueError(f"Q0_MULT can only be applied to slave models: {model}")
    wells = spec.get("wells", [])
    if not wells:
        raise ValueError(f"slave model has no wells: {model}")
    for well in wells:
        base_q0 = float(well["q0_sm3d"])
        if not math.isfinite(base_q0) or base_q0 < 0.0:
            raise ValueError(f"q0_sm3d must be finite and non-negative for {model}/{well['name']}")
        scaled_q0 = base_q0 * multiplier
        if not math.isfinite(scaled_q0):
            raise ValueError(f"scaled q0_sm3d is non-finite for {model}/{well['name']}")
        well["q0_sm3d"] = round(scaled_q0, 6)
    path.write_text(json.dumps(spec, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def run_dummy(model: str, runpath: Path, iteration: int) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "bin" / "eclipse_dummy.py"),
            model,
            str(runpath / model),
            str(iteration),
        ],
        check=True,
        cwd=runpath,
    )


def run_flow_model_n(runpath: Path, iteration: int, coupling: dict[str, Any]) -> None:
    """Run the real OPM Flow restart backend for model_n (Spike 003 adapter).

    The master has already written network_constraints_model_n.csv; the
    adapter simulates the full 2024-2026 chain against those BHPs and writes
    its own raw slave_rates_model_n.csv. The driver then applies the same
    coupling relaxation the dummy slave applies internally: the raw Flow
    rates are reported as q_ipr_sm3d (unrelaxed fixed-point target) and the
    rates forwarded to the master are q_liq_sm3d = q_prev + relaxation *
    (q_raw - q_prev), keeping the unrelaxed-residual convergence criterion
    honest while stabilising the real-simulator response.
    """
    constraints = runpath / "coupling" / "network_constraints_model_n.csv"
    if not constraints.is_file():
        raise FileNotFoundError(
            f"flow backend requires master constraints before model_n runs: {constraints}"
        )
    relaxation = float(coupling["coupling"].get("relaxation", 1.0))
    if not 0.0 < relaxation <= 1.0:
        raise ValueError("coupling.relaxation must be in (0, 1]")
    run_dir = runpath / "coupling" / "flow_model_n" / f"iteration-{iteration:03d}"
    if run_dir.exists():
        # Defensive: main() wipes coupling_dir before the loop, but keep the
        # adapter's "output dir must be absent or empty" contract guaranteed.
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
        ],
        check=True,
        cwd=runpath,
    )
    raw_rates = run_dir / "slave_rates_model_n.csv"
    if not raw_rates.is_file():
        raise FileNotFoundError(f"flow backend did not produce slave rates: {raw_rates}")

    previous_by_well_year: dict[tuple[str, int], float] = {}
    with constraints.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            previous_by_well_year[(row["well"], int(row["year"]))] = float(
                row["network_input_q_liq_sm3d"]
            )
    relaxed_rows: list[dict[str, Any]] = []
    with raw_rates.open(newline="", encoding="utf-8") as handle:
        raw_rows = list(csv.DictReader(handle))
    if not raw_rows:
        raise ValueError(f"flow backend returned an empty rates file: {raw_rates}")
    required_columns = {
        "well",
        "year",
        "q_liq_sm3d",
        "q_gas_sm3d",
        "p_bhp_bar",
        "p_res_bar",
        "q_ipr_sm3d",
        "backpressure_limited",
    }
    if not required_columns.issubset(raw_rows[0].keys()):
        raise ValueError(
            f"flow backend rates file has unexpected columns: "
            f"{sorted(raw_rows[0].keys())}"
        )
    for row in raw_rows:
        key = (row["well"], int(row["year"]))
        if key not in previous_by_well_year:
            raise ValueError(f"flow backend returned unexpected well/year row: {key}")
        raw_rate = float(row["q_liq_sm3d"])
        previous_rate = previous_by_well_year[key]
        relaxed_rate = previous_rate + relaxation * (raw_rate - previous_rate)
        relaxed_rows.append(
            {
                "well": row["well"],
                "year": row["year"],
                "q_liq_sm3d": round(relaxed_rate, 6),
                "q_gas_sm3d": row["q_gas_sm3d"],
                "p_bhp_bar": row["p_bhp_bar"],
                "p_res_bar": row["p_res_bar"],
                "q_ipr_sm3d": row["q_ipr_sm3d"],
                "backpressure_limited": row["backpressure_limited"],
                "origin": "opm_flow_restart_relaxed",
            }
        )
    rates_path = runpath / "coupling" / "slave_rates_model_n.csv"
    with rates_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(relaxed_rows[0]))
        writer.writeheader()
        writer.writerows(relaxed_rows)
    exchange_dir = runpath / "coupling" / "exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    (exchange_dir / f"slave_result_model_n_iteration_{iteration:03d}.json").write_text(
        json.dumps(
            {
                "iteration": iteration,
                "model": "model_n",
                "backend": "opm_flow_restart",
                "relaxation": relaxation,
                "raw_rates": str(raw_rates),
                "relaxed_rates": str(rates_path),
                "adapter_report": str(run_dir / "restart_report.json"),
            },
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    log(f"model_n (flow backend, relaxation={relaxation}): {rates_path}")


def read_slave_rates(
    coupling_dir: Path, model: str, *, field: str = "q_liq_sm3d"
) -> dict[str, dict[int, float]]:
    path = coupling_dir / f"slave_rates_{model}.csv"
    if not path.exists():
        raise FileNotFoundError(f"slave simulation did not produce required rates: {path}")
    rates: dict[str, dict[int, float]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"slave rates file is empty: {path}")
    for row in rows:
        well = row["well"]
        year = int(row["year"])
        if year in rates.setdefault(well, {}):
            raise ValueError(f"duplicate slave rate row for {model}/{well}/{year}")
        if field not in row or row[field] in (None, ""):
            raise ValueError(f"slave rates file is missing {field} for {model}/{well}/{year}")
        rate = float(row[field])
        if not math.isfinite(rate) or rate < 0.0:
            raise ValueError(
                f"{field} must be finite and non-negative for "
                f"{model}/{well}/{year}: {rate}"
            )
        rates[well][year] = rate
    return rates


def flatten_rates(rates: dict[str, dict[int, float]]) -> dict[tuple[str, int], float]:
    return {(well, year): value for well, years in rates.items() for year, value in years.items()}


def max_rel_diff(previous: dict[str, dict[int, float]], current: dict[str, dict[int, float]]) -> float:
    previous_flat = flatten_rates(previous)
    current_flat = flatten_rates(current)
    if previous_flat.keys() != current_flat.keys():
        missing = sorted(previous_flat.keys() - current_flat.keys())
        extra = sorted(current_flat.keys() - previous_flat.keys())
        raise ValueError(f"rate coverage changed between iterations; missing={missing}, extra={extra}")
    if not current_flat:
        raise ValueError("cannot calculate convergence from an empty rate set")
    return max(
        abs(current_flat[key] - previous_flat[key]) / max(abs(previous_flat[key]), 1e-9)
        for key in current_flat
    )


def validate_topology(coupling: dict[str, Any]) -> None:
    years = schedule_years(coupling)
    del years  # validation side effect: schedule must parse before any staging
    settings = coupling["coupling"]
    max_iterations = settings.get("max_iterations")
    if (
        isinstance(max_iterations, bool)
        or not isinstance(max_iterations, int)
        or max_iterations < 1
    ):
        raise ValueError("coupling.max_iterations must be a positive integer")
    tolerance = float(settings.get("tolerance"))
    if not math.isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError("coupling.tolerance must be a finite positive number")
    relaxation = float(settings.get("relaxation"))
    if not math.isfinite(relaxation) or not 0.0 < relaxation <= 1.0:
        raise ValueError("coupling.relaxation must be in (0, 1]")

    master = coupling["master"]["model"]
    if master != "master_network":
        raise ValueError("master model must be 'master_network'")
    if master in coupling["slaves"]:
        raise ValueError("master model cannot also be configured as a slave")
    if set(coupling["slaves"]) != {"model_n", "model_hdn"}:
        raise ValueError("this example requires both model_n and model_hdn as coupled slaves")
    for name, cfg in coupling["slaves"].items():
        backend = str(cfg.get("backend", "dummy"))
        if backend not in ALLOWED_BACKENDS:
            raise ValueError(
                f"slave {name} has unsupported backend {backend!r}; "
                f"allowed values are {ALLOWED_BACKENDS}"
            )
        if backend == "flow":
            if name != "model_n":
                raise ValueError("the flow backend is currently supported only for model_n")
            if not FLOW_ADAPTER.is_file():
                raise FileNotFoundError(
                    f"flow backend requires the Spike 003 adapter at {FLOW_ADAPTER}"
                )
            for executable in ("flow", "summary"):
                if shutil.which(executable) is None:
                    raise FileNotFoundError(
                        f"flow backend requires the {executable!r} executable on PATH"
                    )
    if not coupling.get("prescribed_network_profiles"):
        raise ValueError("at least one prescribed network profile is required")
    profile_prefix = Path("input") / master
    for name, profile in coupling["prescribed_network_profiles"].items():
        if profile.get("keyword") != "GSATPROD":
            raise ValueError(
                f"dummy supports only GSATPROD prescribed profiles; "
                f"{name} uses {profile.get('keyword')!r}"
            )
        configured_path = Path(profile["path"])
        if configured_path.is_absolute() or ".." in configured_path.parts:
            raise ValueError(f"prescribed profile path must be repository-relative: {configured_path}")
        try:
            configured_path.relative_to(profile_prefix)
        except ValueError as exc:
            raise ValueError(
                f"prescribed profile {name} must live below {profile_prefix}: {configured_path}"
            ) from exc
        if not (ROOT / configured_path).is_file():
            raise FileNotFoundError(f"prescribed network profile does not exist: {ROOT / configured_path}")


def validate_runpath(runpath: Path) -> None:
    protected_exact = {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()}
    if runpath in protected_exact:
        raise ValueError(f"refusing unsafe runpath: {runpath}")
    protected_trees = [ROOT / "input", ROOT / "bin", ROOT / "ert", ROOT / "tests"]
    for protected in protected_trees:
        protected = protected.resolve()
        if runpath == protected or runpath.is_relative_to(protected):
            raise ValueError(f"runpath overlaps protected repository sources: {runpath}")


def initial_rate_rows(
    runpath: Path, coupling: dict[str, Any], model: str, years: list[int]
) -> dict[str, dict[int, float]]:
    spec = load_json(runpath / model / "simspec.json")
    initial = coupling["initial_slave_rates_sm3d"]
    rows: list[list[Any]] = []
    rates: dict[str, dict[int, float]] = {}
    for well in spec["wells"]:
        name = well["name"]
        if name not in initial:
            raise ValueError(f"missing initial rate for {model}/{name}")
        rate = float(initial[name])
        if not math.isfinite(rate) or rate < 0.0:
            raise ValueError(f"initial rate must be finite and non-negative for {model}/{name}: {rate}")
        gor = float(well["gor_sm3_sm3"])
        if not math.isfinite(gor) or gor < 0.0:
            raise ValueError(f"GOR must be finite and non-negative for {model}/{name}: {gor}")
        initial_gas_rate = rate * gor
        if not math.isfinite(initial_gas_rate):
            raise ValueError(f"initial gas rate is non-finite for {model}/{name}")
        rates[name] = {}
        for year in years:
            rates[name][year] = rate
            rows.append([name, year, rate, initial_gas_rate, "initial_guess"])
    path = runpath / "coupling" / f"slave_rates_{model}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["well", "year", "q_liq_sm3d", "q_gas_sm3d", "origin"])
        writer.writerows(rows)
    return rates


def write_report(
    runpath: Path,
    coupling: dict[str, Any],
    q0_mult: float,
    history: list[list[Any]],
    converged: bool,
) -> str:
    master_model = coupling["master"]["model"]
    slaves = coupling["slaves"]
    profile_description = ", ".join(
        f"{name} ({profile['keyword']})"
        for name, profile in coupling["prescribed_network_profiles"].items()
    )
    slave_description = ", ".join(
        f"{name} ({str(cfg['role']).replace('_', ' ')})" for name, cfg in slaves.items()
    )
    slave_backends = ", ".join(
        f"{name}={coupling['slaves'][name].get('backend', 'dummy')}" for name in slaves
    )
    lines = [
        "COUPLED RUN REPORT",
        "===================",
        f"runpath            : {runpath}",
        f"q0_mult            : {q0_mult}",
        f"master model       : {master_model}",
        f"prescribed profiles : {profile_description}",
        f"slave models       : {slave_description}",
        f"slave backends     : {slave_backends}",
        f"iterations         : {history[-1][0]} of {coupling['coupling']['max_iterations']} "
        f"(converged={converged}, tol={coupling['coupling']['tolerance']})",
        "",
        "Network input contract:",
        "  prescribed production profiles       -> master_network",
        "  model_n simulation results            -> master_network",
        "  model_hdn simulation results          -> master_network",
        "  master network pressure constraints   -> both slaves",
        "",
        "Final simulated slave rates (sm3/d):",
    ]
    for name in slaves:
        rates = read_slave_rates(runpath / "coupling", name)
        for well, year_rates in sorted(rates.items()):
            rendered = ", ".join(f"{year}: {rate:.1f}" for year, rate in sorted(year_rates.items()))
            lines.append(f"  {name}/{well}: {rendered}")
    report = "\n".join(lines) + "\n"
    (runpath / "COUPLED_REPORT.txt").write_text(report, encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run outside ERT")
    parser.add_argument("--runpath", help="explicit output runpath")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="coupling configuration JSON")
    parser.add_argument(
        "--backend-model-n",
        choices=list(ALLOWED_BACKENDS),
        help="override the model_n slave backend (default: coupling.json)",
    )
    args = parser.parse_args()

    coupling = load_json(Path(args.config))
    if args.backend_model_n is not None:
        if "model_n" not in coupling.get("slaves", {}):
            raise ValueError(
                "--backend-model-n requires a model_n slave in the coupling configuration"
            )
        coupling["slaves"]["model_n"]["backend"] = args.backend_model_n
    validate_topology(coupling)
    master_model = coupling["master"]["model"]
    slaves = coupling["slaves"]
    years = schedule_years(coupling)

    if args.runpath:
        runpath = Path(args.runpath).resolve()
    elif args.demo:
        runpath = ROOT / "output" / "demo" / "realization-0"
    else:
        runpath = Path.cwd()
    validate_runpath(runpath)
    q0_mult = parse_q0_mult(runpath, allow_default=args.demo)
    runpath.mkdir(parents=True, exist_ok=True)

    coupling_dir = runpath / "coupling"
    if coupling_dir.exists():
        shutil.rmtree(coupling_dir)
    coupling_dir.mkdir(parents=True)

    if args.demo and not (runpath / "q0_mult.txt").exists():
        (runpath / "q0_mult.txt").write_text(f"Q0_MULT  {q0_mult:.6f}\n", encoding="utf-8")
    (runpath / "coupling_config.json").write_text(
        json.dumps(coupling, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    log(f"runpath: {runpath}   q0_mult: {q0_mult}")

    stage_model(runpath, master_model)
    previous_rates: dict[str, dict[str, dict[int, float]]] = {}
    for name in slaves:
        stage_model(runpath, name)
        apply_q0_mult(runpath, name, q0_mult)
        previous_rates[name] = initial_rate_rows(runpath, coupling, name, years)

    max_iterations = int(coupling["coupling"]["max_iterations"])
    tolerance = float(coupling["coupling"]["tolerance"])
    history: list[list[Any]] = []
    converged = False

    for iteration in range(1, max_iterations + 1):
        log(f"--- coupling iteration {iteration}/{max_iterations} ---")
        run_dummy(master_model, runpath, iteration)
        for name in slaves:
            if coupling["slaves"][name].get("backend", "dummy") == "flow":
                run_flow_model_n(runpath, iteration, coupling)
            else:
                run_dummy(name, runpath, iteration)

        current_rates: dict[str, dict[str, dict[int, float]]] = {}
        difference = 0.0
        for name in slaves:
            current_rates[name] = read_slave_rates(coupling_dir, name)
            fixed_point_rates = read_slave_rates(
                coupling_dir, name, field="q_ipr_sm3d"
            )
            difference = max(
                difference,
                max_rel_diff(previous_rates[name], fixed_point_rates),
            )
        history.append([iteration, round(difference, 9)])
        log(f"iteration {iteration}: max relative fixed-point residual = {difference:.4%}")
        if difference <= tolerance:
            converged = True
            break
        previous_rates = current_rates

    with (coupling_dir / "convergence_history.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["iteration", "max_fixed_point_residual"])
        writer.writerows(history)

    report = write_report(runpath, coupling, q0_mult, history, converged)
    print(report, end="")
    if not converged:
        print("COUPLED RUN FAILED: maximum iterations reached without convergence", file=sys.stderr)
        return 2
    print("COUPLED RUN COMPLETE (converged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
