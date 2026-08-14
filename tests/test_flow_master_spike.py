from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONFIG = ROOT / "configs" / "coupling.legacy-gsatprod.json"
MASTER_ADAPTER = ROOT / "spikes" / "004-opm-flow-master" / "opm_flow_master_adapter.py"
DRIVER = ROOT / "ert" / "bin" / "scripts" / "run_coupled.py"
PROFILE = ROOT / "input" / "master_network" / "profiles" / "gsatprod_external.inc"
FLOW_AVAILABLE = shutil.which("flow") is not None and shutil.which("summary") is not None

A_CAP = 350.0
B_CAP = 315.0


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_rates(path: Path, base: float, wells: tuple[str, ...]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["well", "year", "q_liq_sm3d", "q_gas_sm3d", "p_bhp_bar", "p_res_bar",
             "q_ipr_sm3d", "backpressure_limited", "origin"]
        )
        for index, well in enumerate(wells):
            multiplier = 1.0 - 0.1 * index
            for year, decline in ((2024, 1.0), (2025, 0.85), (2026, 0.7)):
                writer.writerow(
                    [well, year, round(base * multiplier * decline, 3), 0.0, 300.0, 330.0,
                     round(base * multiplier * decline, 3), 1, "test"]
                )


def run_master(
    rates_n: Path,
    rates_hdn: Path,
    output_dir: Path,
    profile: Path = PROFILE,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(MASTER_ADAPTER),
            "--rates",
            "model_a",
            str(rates_n),
            "--rates",
            "model_b",
            str(rates_hdn),
            "--profile",
            str(profile),
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def run_driver(
    config: Path,
    runpath: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIVER), "--demo", "--config", str(config), "--runpath", str(runpath)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


class FlowMasterValidationTest(unittest.TestCase):
    def make_config(self, path: Path, master_backend: str) -> Path:
        config = json.loads((LEGACY_CONFIG).read_text(encoding="utf-8"))
        config["master"]["backend"] = master_backend
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return path

    def test_invalid_master_backend_is_rejected_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = self.make_config(temp / "coupling.json", "eclipse")
            completed = run_driver(config, temp / "run")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsupported backend", completed.stderr)
            self.assertFalse((temp / "run" / "master_network").exists())

    def test_flow_master_requires_executables_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = self.make_config(temp / "coupling.json", "flow")
            completed = run_driver(config, temp / "run", env={"PATH": ""})
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("flow master backend requires", completed.stderr)
            self.assertFalse((temp / "run" / "master_network").exists())

    def test_missing_master_simspec_is_rejected(self) -> None:
        if not FLOW_AVAILABLE:
            self.skipTest("requires installed OPM Flow and summary CLI")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            rates_n = temp / "n.csv"
            rates_hdn = temp / "hdn.csv"
            write_rates(rates_n, 500.0, ("A-P1", "A-P2"))
            write_rates(rates_hdn, 400.0, ("B-P1", "B-P2"))
            completed = run_master(rates_n, rates_hdn, temp / "run")
            # Default simspec exists, so this run should succeed; a missing
            # simspec path is exercised via execute() directly below.
            self.assertEqual(completed.returncode, 0, completed.stderr)
            import importlib.util
            spec = importlib.util.spec_from_file_location("master_adapter", MASTER_ADAPTER)
            self.assertIsNotNone(spec)
            module = importlib.util.module_from_spec(spec)  # type: ignore[union-attr]
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            with self.assertRaises(FileNotFoundError):
                module.execute(
                    {"model_a": rates_n, "model_b": rates_hdn},
                    PROFILE,
                    temp / "run2",
                    "flow",
                    "summary",
                    simspec=temp / "missing.json",
                )


@unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
class FlowMasterAdapterIntegrationTest(unittest.TestCase):
    def test_adapter_writes_constraints_within_slave_caps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            rates_n = temp / "n.csv"
            rates_hdn = temp / "hdn.csv"
            write_rates(rates_n, 500.0, ("A-P1", "A-P2"))
            write_rates(rates_hdn, 400.0, ("B-P1", "B-P2"))
            completed = run_master(rates_n, rates_hdn, temp / "run")
            self.assertEqual(completed.returncode, 0, completed.stderr)

            report = json.loads((temp / "run" / "master_report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["simulator"].startswith("flow "))
            self.assertEqual(report["checks"]["satellite_registers_in_group_totals"], True)
            for model, cap in (("model_a", A_CAP), ("model_b", B_CAP)):
                rows = read_csv(temp / "run" / f"network_constraints_{model}.csv")
                self.assertEqual(len(rows), 6)
                for row in rows:
                    self.assertLess(float(row["p_bhp_bar"]), cap)
                    self.assertGreater(float(row["p_bhp_bar"]), 0.0)
                    self.assertGreater(float(row["p_manifold_bar"]), 0.0)
                    self.assertAlmostEqual(
                        float(row["total_network_q_liq_sm3d"]),
                        float(row["prescribed_q_liq_sm3d"]) + float(row["simulated_q_liq_sm3d"]),
                        places=6,
                    )

    def test_manifold_rises_with_total_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            manifolds = []
            for label, base in (("low", 150.0), ("high", 1500.0)):
                rates_n = temp / f"{label}-n.csv"
                rates_hdn = temp / f"{label}-hdn.csv"
                write_rates(rates_n, base, ("A-P1", "A-P2"))
                write_rates(rates_hdn, base * 0.8, ("B-P1", "B-P2"))
                completed = run_master(rates_n, rates_hdn, temp / label)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                report = json.loads((temp / label / "master_report.json").read_text(encoding="utf-8"))
                manifolds.append(report["checks"]["manifold_pressure_by_year"]["2024"])
            self.assertGreater(manifolds[1], manifolds[0] + 1.0)


@unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
class FlowMasterDriverIntegrationTest(unittest.TestCase):
    def make_config(self, path: Path, master: str, model_a: str, model_b: str) -> Path:
        config = json.loads((LEGACY_CONFIG).read_text(encoding="utf-8"))
        config["master"]["backend"] = master
        config["slaves"]["model_a"]["backend"] = model_a
        config["slaves"]["model_b"]["backend"] = model_b
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return path

    def test_flow_master_with_dummy_slaves_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = self.make_config(temp / "coupling.json", "flow", "dummy", "dummy")
            completed = run_driver(config, temp / "run")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            history = read_csv(temp / "run" / "coupling" / "convergence_history.csv")
            self.assertLessEqual(float(history[-1]["max_fixed_point_residual"]), 0.005)
            report = (temp / "run" / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("slave backends     : model_a=dummy, model_b=dummy", report)
            flow_master = temp / "run" / "coupling" / "flow_master"
            self.assertGreaterEqual(len(list(flow_master.glob("iteration-*"))), 2)

    def test_all_real_realization_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = self.make_config(temp / "coupling.json", "flow", "flow", "flow")
            config = json.loads(config.read_text(encoding="utf-8"))
            config["coupling"]["relaxation"] = 0.4
            config["coupling"]["max_iterations"] = 20
            (temp / "coupling-all.json").write_text(
                json.dumps(config, indent=2) + "\n", encoding="utf-8"
            )
            completed = run_driver(temp / "coupling-all.json", temp / "run")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            history = read_csv(temp / "run" / "coupling" / "convergence_history.csv")
            self.assertLessEqual(float(history[-1]["max_fixed_point_residual"]), 0.005)
            report = (temp / "run" / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("slave backends     : model_a=flow, model_b=flow", report)
            for model in ("model_a", "model_b"):
                rows = read_csv(temp / "run" / "coupling" / f"slave_rates_{model}.csv")
                self.assertTrue(rows)
                self.assertTrue(all(row["origin"].startswith("opm_flow_restart") for row in rows))
            # The master's constraints must have stayed under the slave caps.
            for model, cap in (("model_a", A_CAP), ("model_b", B_CAP)):
                rows = read_csv(temp / "run" / "coupling" / f"network_constraints_{model}.csv")
                self.assertTrue(all(float(row["p_bhp_bar"]) < cap for row in rows))
            # The final master iteration should deliver every requested rate.
            last_master = max(
                (temp / "run" / "coupling" / "flow_master").glob("iteration-*")
            )
            report = json.loads((last_master / "master_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["checks"]["cutbacks"], [])


if __name__ == "__main__":
    unittest.main()
