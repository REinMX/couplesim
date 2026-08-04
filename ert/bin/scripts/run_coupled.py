#!/usr/bin/env python3
"""Coupled multi-model reservoir forward model -- ERT job + standalone demo.

One ERT realization runs THREE reservoir models "at the same time":

  master_network -- dummy reservoir with the subsea network model
  model_n        -- coupled slave (GSATPROD profiles from master_network)
  model_hdn      -- coupled slave (GSATPROD profiles from master_network;
                    a static mode is available per model via coupling.json)

Coupling loop (fixed-point iteration on well rates):

    for iteration in 1..max_iterations:
        1. master_network reads the slave wells' demanded rates (from the
           slaves' simulation results) and solves the network -> writes
           GSATPROD tables for each coupled slave
        2. each slave simulates with its GSATPROD table -> reports actual
           rates back to coupling/
        3. convergence check: max relative change of coupled-slave rates
           vs previous iteration <= tolerance -> stop

How this script is launched:
  * ERT : installed as the RUN_COUPLED forward-model step (job file
          ert/bin/jobs/RUN_COUPLED). ERT runs it with cwd == realization
          runpath, which is where GEN_KW wrote q0_mult.txt (the sampled
          parameter).
  * Demo: ./run_demo.sh --demo   -> no ERT needed, writes output/demo/
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path


def find_root() -> Path:
    """Repository root: first ancestor of this script that has coupling.json."""
    here = Path(__file__).resolve()
    for cand in [here, *here.parents]:
        if (cand / "coupling.json").exists():
            return cand
    raise SystemExit(f"cannot locate coupling.json from {here}")


ROOT = find_root()
COUPLING_JSON = ROOT / "coupling.json"


def log(msg: str) -> None:
    print(f"[run_coupled] {msg}", flush=True)


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_q0_mult(runpath: Path) -> float:
    """Read the GEN_KW result file (written by ERT into the runpath)."""
    f = runpath / "q0_mult.txt"
    if not f.exists():
        return 1.0
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            try:
                return float(line.split()[-1])
            except ValueError:
                pass
    return 1.0


def stage_model(runpath: Path, model: str) -> None:
    src = ROOT / "input" / model
    dst = runpath / model
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    log(f"staged {model} -> {dst}")


def apply_q0_mult(runpath: Path, model: str, mult: float) -> None:
    """Scale the coupled slave's productivity with the sampled parameter."""
    simspec_path = runpath / model / "simspec.json"
    spec = load_json(simspec_path)
    for w in spec.get("wells", []):
        w["q0_sm3d"] = round(w["q0_sm3d"] * mult, 2)
    simspec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")


def run_dummy(model: str, runpath: Path, iteration: int) -> None:
    cmd = [
        sys.executable,
        str(ROOT / "bin" / "eclipse_dummy.py"),
        model,
        str(runpath / model),
        str(iteration),
    ]
    subprocess.run(cmd, check=True, cwd=str(runpath))


def read_slave_rates(coupling_dir: Path, model: str) -> dict:
    path = coupling_dir / f"slave_rates_{model}.csv"
    if not path.exists():
        return {}
    out: dict = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.setdefault(row["well"], {})[int(row["year"])] = float(row["q_liq_sm3d"])
    return out


def max_rel_diff(a: dict, b: dict) -> float:
    worst = 0.0
    for well, years in b.items():
        for y, q in years.items():
            q_prev = a.get(well, {}).get(y)
            if q_prev is None:
                continue
            worst = max(worst, abs(q - q_prev) / max(abs(q_prev), 1e-9))
    return worst


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true", help="standalone demo run under output/demo/ (no ERT)")
    ap.add_argument("--runpath", help="explicit runpath (default: cwd under ERT, output/demo/realization-0 in demo)")
    args = ap.parse_args()

    coupling = load_json(COUPLING_JSON)
    master_model = coupling["master"]["model"]
    slaves = coupling["slaves"]

    if args.runpath:
        runpath = Path(args.runpath)
    elif args.demo or not (Path.cwd() / "q0_mult.txt").exists():
        runpath = ROOT / "output" / "demo" / "realization-0"
    else:
        runpath = Path.cwd()
    runpath.mkdir(parents=True, exist_ok=True)
    # fresh coupling dir: repeated demo runs must not pick up stale CSVs
    shutil.rmtree(runpath / "coupling", ignore_errors=True)
    (runpath / "coupling").mkdir(parents=True, exist_ok=True)

    q0_mult = parse_q0_mult(runpath)
    if args.demo or args.runpath:
        (runpath / "q0_mult.txt").write_text(f"Q0_MULT  {q0_mult:.6f}\n", encoding="utf-8")
    log(f"runpath: {runpath}   q0_mult: {q0_mult}")

    # stage the three models into the runpath and prepare the coupling files
    stage_model(runpath, master_model)
    prev_rates: dict = {}
    for name, cfg in slaves.items():
        stage_model(runpath, name)
        if cfg["mode"] == "coupled":
            apply_q0_mult(runpath, name, q0_mult)
        else:
            static = ROOT / cfg["static_profile"]
            shutil.copyfile(static, runpath / "coupling" / f"gsatprod_{name}.inc")
            log(f"static profile for {name}: {static.name}")

    max_iter = int(coupling["coupling"]["max_iterations"])
    tol = float(coupling["coupling"]["tolerance"])
    history: list[list] = []
    converged = False

    for it in range(1, max_iter + 1):
        log(f"--- coupling iteration {it}/{max_iter} ---")
        run_dummy(master_model, runpath, it)
        for name, cfg in slaves.items():
            run_dummy(name, runpath, it)

        if it == 1:
            history.append([it, ""])
            log("iteration 1: slaves ran from the initial rate guess")
            for name, cfg in slaves.items():
                prev_rates[name] = read_slave_rates(runpath / "coupling", name)
            continue

        diff = 0.0
        for name, cfg in slaves.items():
            if cfg["mode"] != "coupled":
                continue
            rates = read_slave_rates(runpath / "coupling", name)
            diff = max(diff, max_rel_diff(prev_rates.get(name, {}), rates))
            prev_rates[name] = rates
        history.append([it, round(diff, 6)])
        log(f"iteration {it}: max relative rate change = {diff:.4%}")
        if diff <= tol:
            converged = True
            break

    # convergence history + final report
    with open(runpath / "coupling" / "convergence_history.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["iteration", "max_rel_diff"])
        w.writerows(history)

    slave_desc = ", ".join(f"{n} ({c['mode']})" for n, c in slaves.items())
    lines = [
        "COUPLED RUN REPORT",
        "===================",
        f"runpath       : {runpath}",
        f"q0_mult       : {q0_mult}",
        f"master model  : {master_model}",
        f"slave models  : {slave_desc}",
        f"iterations    : {history[-1][0]} of {max_iter} (converged={converged}, tol={tol})",
        "",
        "Final well rates (sm3/d) by year:",
    ]
    for name, cfg in slaves.items():
        rates = read_slave_rates(runpath / "coupling", name)
        for well, years in sorted(rates.items()):
            ys = ", ".join(f"{y}: {q:.1f}" for y, q in sorted(years.items()))
            lines.append(f"  {name}/{well}: {ys}")
    (runpath / "COUPLED_REPORT.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("COUPLED RUN COMPLETE" + (" (converged)" if converged else " (max iterations reached)"))


if __name__ == "__main__":
    main()
