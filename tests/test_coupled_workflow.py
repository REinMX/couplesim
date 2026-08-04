from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "ert" / "bin" / "scripts" / "run_coupled.py"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class CoupledWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.runpath = Path(cls.temp_dir.name) / "run"
        cls.completed = subprocess.run(
            [sys.executable, str(DRIVER), "--demo", "--runpath", str(cls.runpath)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_network_request_contains_profiles_and_both_slave_simulations(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        requests = sorted((self.runpath / "coupling" / "exchange").glob("network_request_iteration_*.json"))
        self.assertGreaterEqual(len(requests), 2)

        request = json.loads(requests[-1].read_text(encoding="utf-8"))
        sources = request["sources"]
        self.assertEqual(set(sources["simulated_slaves"]), {"model_n", "model_hdn"})
        self.assertEqual(set(sources["prescribed_profiles"]), {"external_satellite"})
        self.assertEqual(sources["prescribed_profiles"]["external_satellite"]["keyword"], "GSATPROD")

        prescribed_wells = {
            row["well"]
            for profile in sources["prescribed_profiles"].values()
            for row in profile["rows"]
        }
        slave_wells = {
            row["well"]
            for rows in sources["simulated_slaves"].values()
            for row in rows
        }
        self.assertTrue(prescribed_wells)
        self.assertTrue(slave_wells)
        self.assertTrue(prescribed_wells.isdisjoint(slave_wells))

    def test_network_totals_include_prescribed_and_simulated_rates(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        request_path = sorted(
            (self.runpath / "coupling" / "exchange").glob("network_request_iteration_*.json")
        )[-1]
        request = json.loads(request_path.read_text(encoding="utf-8"))

        for total in request["totals_by_year"]:
            expected = total["prescribed_q_liq_sm3d"] + total["simulated_q_liq_sm3d"]
            self.assertAlmostEqual(total["network_q_liq_sm3d"], expected, places=6)
            self.assertGreater(total["prescribed_q_liq_sm3d"], 0.0)
            self.assertGreater(total["simulated_q_liq_sm3d"], 0.0)

    def test_master_returns_network_constraints_to_both_slaves(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        for model, expected_wells in {
            "model_n": {"N-P1", "N-P2"},
            "model_hdn": {"H-P1", "H-P2"},
        }.items():
            rows = read_csv(self.runpath / "coupling" / f"network_constraints_{model}.csv")
            self.assertEqual({row["well"] for row in rows}, expected_wells)
            self.assertEqual({int(row["year"]) for row in rows}, {2024, 2025, 2026})
            self.assertTrue(all(float(row["total_network_q_liq_sm3d"]) > 0.0 for row in rows))

    def test_shared_network_pressure_uses_total_rate_including_profiles(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        request_path = sorted(
            (self.runpath / "coupling" / "exchange").glob("network_request_iteration_*.json")
        )[-1]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        totals = {row["year"]: row for row in request["totals_by_year"]}
        network = json.loads(
            (ROOT / "input" / "master_network" / "simspec.json").read_text(encoding="utf-8")
        )["network"]
        constraints = read_csv(self.runpath / "coupling" / "network_constraints_model_n.csv")

        for row in constraints:
            total_rate = totals[int(row["year"])]["network_q_liq_sm3d"]
            expected = (
                network["outlet_pressure_bar"]
                + network["trunk_friction_a_bar_sm3d"] * total_rate
                + network["trunk_friction_b_bar_sm3d2"] * total_rate**2
            )
            self.assertAlmostEqual(float(row["p_manifold_bar"]), expected, places=5)

    def test_report_proves_all_three_network_input_categories(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        report = (self.runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
        self.assertIn("prescribed profiles : external_satellite (GSATPROD)", report)
        self.assertIn("model_n (coupled slave)", report)
        self.assertIn("model_hdn (coupled slave)", report)


class FailureHandlingTest(unittest.TestCase):
    def test_nonconvergence_fails_the_forward_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
            config["coupling"]["max_iterations"] = 1
            config["coupling"]["tolerance"] = 1.0e-12
            config_path = temp / "nonconvergent.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runpath = temp / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--demo",
                    "--runpath",
                    str(runpath),
                    "--config",
                    str(config_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("maximum iterations reached without convergence", completed.stderr)
            self.assertIn("converged=False", (runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8"))

    def test_tiny_relaxation_does_not_create_false_convergence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
            config["coupling"]["relaxation"] = 1.0e-6
            config["coupling"]["max_iterations"] = 2
            config_path = temp / "tiny-relaxation.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runpath = temp / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--demo",
                    "--runpath",
                    str(runpath),
                    "--config",
                    str(config_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("maximum iterations reached without convergence", completed.stderr)
            self.assertIn(
                "converged=False",
                (runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8"),
            )

    def test_unsafe_master_identifier_is_rejected_without_runpath_damage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
            config["master"]["model"] = "."
            config_path = temp / "unsafe-master.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runpath = temp / "valuable-existing-directory"
            runpath.mkdir()
            sentinel = runpath / "sentinel.txt"
            sentinel.write_text("preserve me\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--demo",
                    "--runpath",
                    str(runpath),
                    "--config",
                    str(config_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("master model must be 'master_network'", completed.stderr)
            self.assertTrue(sentinel.is_file())
            self.assertEqual(set(runpath.iterdir()), {sentinel})

    def test_invalid_zero_relaxation_is_rejected_before_runpath_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
            config["coupling"]["relaxation"] = 0.0
            config_path = temp / "invalid-relaxation.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runpath = temp / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--demo",
                    "--runpath",
                    str(runpath),
                    "--config",
                    str(config_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("relaxation must be in (0, 1]", completed.stderr)
            self.assertFalse(runpath.exists())

    def test_missing_ert_parameter_is_rejected_outside_demo_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runpath = Path(temp_dir) / "run"
            completed = subprocess.run(
                [sys.executable, str(DRIVER), "--runpath", str(runpath)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("required ERT parameter file is missing", completed.stderr)

    def test_non_finite_ert_parameter_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runpath = Path(temp_dir) / "run"
            runpath.mkdir()
            (runpath / "q0_mult.txt").write_text("Q0_MULT NaN\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(DRIVER), "--runpath", str(runpath)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("finite positive Q0_MULT", completed.stderr)

    def test_dummy_rejects_unimplemented_gsatptab_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
            config["prescribed_network_profiles"]["external_satellite"]["keyword"] = "GSATPTAB"
            config_path = temp / "gsatptab.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runpath = temp / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--demo",
                    "--runpath",
                    str(runpath),
                    "--config",
                    str(config_path),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("dummy supports only GSATPROD", completed.stderr)
            self.assertFalse(runpath.exists())

    def test_malformed_ert_parameter_is_not_silently_defaulted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runpath = Path(temp_dir) / "run"
            runpath.mkdir()
            (runpath / "q0_mult.txt").write_text("Q0_MULT not-a-number\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(DRIVER), "--runpath", str(runpath)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("invalid Q0_MULT", completed.stderr)


if __name__ == "__main__":
    unittest.main()
