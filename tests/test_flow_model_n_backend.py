from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "ert" / "bin" / "scripts" / "run_coupled.py"
FLOW_AVAILABLE = shutil.which("flow") is not None and shutil.which("summary") is not None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def run_driver(
    *args: str,
    runpath: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(DRIVER), "--demo"]
    if runpath is not None:
        command.extend(["--runpath", str(runpath)])
    command.extend(args)
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


class FlowBackendValidationTest(unittest.TestCase):
    def write_config(self, path: Path, slaves: dict) -> None:
        config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
        config["slaves"] = slaves
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    def test_invalid_backend_value_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = temp / "coupling.json"
            slaves = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))["slaves"]
            slaves["model_n"]["backend"] = "eclipse"
            self.write_config(config, slaves)
            completed = run_driver("--config", str(config), runpath=temp / "run")
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unsupported backend", completed.stderr)
            self.assertFalse((temp / "run" / "master_network").exists())

    def test_flow_backend_on_model_hdn_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = temp / "coupling.json"
            slaves = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))["slaves"]
            slaves["model_hdn"]["backend"] = "flow"
            self.write_config(config, slaves)
            stripped = {"PATH": ""}
            completed = run_driver(
                "--config", str(config),
                runpath=temp / "run",
                env=stripped,
            )
            # Validation accepts the hdn flow backend; the stripped PATH makes
            # the executable preflight fail before any staging.
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("flow backend requires", completed.stderr)
            self.assertFalse((temp / "run" / "master_network").exists())

    def test_flow_backend_requires_executables_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            stripped = {"PATH": ""}
            completed = run_driver(
                "--backend-model-n", "flow",
                runpath=temp / "run",
                env=stripped,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("flow backend requires", completed.stderr)
            self.assertFalse((temp / "run" / "master_network").exists())

    def test_cli_backend_override_lands_in_runpath_config_copy(self) -> None:
        if not FLOW_AVAILABLE:
            self.skipTest("requires installed OPM Flow and summary CLI")
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            completed = run_driver("--backend-model-n", "flow", runpath=temp / "run")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            copied = json.loads(
                (temp / "run" / "coupling_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(copied["slaves"]["model_n"]["backend"], "flow")


@unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
class FlowBackendHybridIntegrationTest(unittest.TestCase):
    """One full hybrid realization: real Flow model_n + dummy model_hdn +
    dummy master_network with prescribed GSATPROD sources."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.temp = Path(cls.temp_dir.name)
        # Pin model_hdn to dummy explicitly: the repo default is now hybrid
        # (both slaves on flow), while this class tests the model_n-only
        # hybrid topology.
        config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
        config["slaves"]["model_n"]["backend"] = "flow"
        config["slaves"]["model_hdn"]["backend"] = "dummy"
        cls.config_path = cls.temp / "coupling-hybrid.json"
        cls.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        cls.runpath = cls.temp / "hybrid-run"
        cls.completed = run_driver(
            "--config", str(cls.config_path), runpath=cls.runpath
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_hybrid_realization_converges(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        history = read_csv(self.runpath / "coupling" / "convergence_history.csv")
        self.assertGreaterEqual(len(history), 2)
        self.assertLessEqual(float(history[-1]["max_fixed_point_residual"]), 0.005)

    def test_report_names_the_backends(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        report = (self.runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
        self.assertIn("slave backends     : model_n=flow, model_hdn=dummy", report)
        self.assertIn("prescribed profiles : external_satellite (GSATPROD)", report)

    def test_flow_rates_are_real_flow_outputs(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        rows = read_csv(self.runpath / "coupling" / "slave_rates_model_n.csv")
        self.assertEqual(len(rows), 6)
        self.assertEqual({int(row["year"]) for row in rows}, {2024, 2025, 2026})
        self.assertEqual({row["well"] for row in rows}, {"N-P1", "N-P2"})
        for row in rows:
            self.assertTrue(row["origin"].startswith("opm_flow_restart"))
            self.assertGreater(float(row["q_liq_sm3d"]), 0.0)
            self.assertGreater(float(row["q_ipr_sm3d"]), 0.0)
            self.assertLess(float(row["p_bhp_bar"]), 350.0)
        flow_iters = sorted(
            (self.runpath / "coupling" / "flow_model_n").glob("iteration-*")
        )
        self.assertGreaterEqual(len(flow_iters), 2)
        self.assertTrue(
            (flow_iters[-1] / "restart_report.json").is_file(),
            "adapter report must exist for the final Flow run",
        )
        result = json.loads(
            (
                self.runpath
                / "coupling"
                / "exchange"
                / "slave_result_model_n_iteration_001.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(result["backend"], "opm_flow_restart")
        self.assertIn("relaxation", result)
        self.assertIn("raw_rates", result)

    def test_master_still_sees_all_three_source_categories(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        requests = sorted(
            (self.runpath / "coupling" / "exchange").glob("network_request_iteration_*.json")
        )
        request = json.loads(requests[-1].read_text(encoding="utf-8"))
        self.assertEqual(set(request["sources"]["simulated_slaves"]), {"model_n", "model_hdn"})
        self.assertEqual(
            set(request["sources"]["prescribed_profiles"]), {"external_satellite"}
        )
        for total in request["totals_by_year"]:
            self.assertAlmostEqual(
                total["network_q_liq_sm3d"],
                total["prescribed_q_liq_sm3d"] + total["simulated_q_liq_sm3d"],
                places=6,
            )

    def test_dummy_slave_unchanged_by_flow_backend(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        rows = read_csv(self.runpath / "coupling" / "slave_rates_model_hdn.csv")
        self.assertTrue(rows)
        self.assertTrue(all(row["origin"] == "simulation_output" for row in rows))


@unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
class FlowBackendBothFlowIntegrationTest(unittest.TestCase):
    """Both slaves on real Flow: model_n and model_hdn restart chains in the
    same coupled realization (the repo's intended hybrid topology)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.temp = Path(cls.temp_dir.name)
        config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
        config["slaves"]["model_n"]["backend"] = "flow"
        config["slaves"]["model_hdn"]["backend"] = "flow"
        cls.config_path = cls.temp / "coupling-both.json"
        cls.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        cls.runpath = cls.temp / "both-run"
        cls.completed = run_driver("--config", str(cls.config_path), runpath=cls.runpath)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def test_both_flow_realization_converges(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        history = read_csv(self.runpath / "coupling" / "convergence_history.csv")
        self.assertGreaterEqual(len(history), 2)
        self.assertLessEqual(float(history[-1]["max_fixed_point_residual"]), 0.005)
        report = (self.runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
        self.assertIn("slave backends     : model_n=flow, model_hdn=flow", report)

    def test_both_slaves_emit_real_flow_rates(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        for model, wells in (("model_n", {"N-P1", "N-P2"}), ("model_hdn", {"H-P1", "H-P2"})):
            rows = read_csv(self.runpath / "coupling" / f"slave_rates_{model}.csv")
            self.assertEqual(len(rows), 6)
            self.assertEqual({row["well"] for row in rows}, wells)
            self.assertEqual({int(row["year"]) for row in rows}, {2024, 2025, 2026})
            for row in rows:
                self.assertTrue(row["origin"].startswith("opm_flow_restart"))
                self.assertGreater(float(row["q_liq_sm3d"]), 0.0)
            flow_iters = sorted((self.runpath / "coupling" / f"flow_{model}").glob("iteration-*"))
            self.assertGreaterEqual(len(flow_iters), 2)
            self.assertTrue(
                (flow_iters[-1] / "restart_report.json").is_file(),
                f"adapter report must exist for the final {model} Flow run",
            )
            result = json.loads(
                (
                    self.runpath
                    / "coupling"
                    / "exchange"
                    / f"slave_result_{model}_iteration_001.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(result["model"], model)
            self.assertEqual(result["backend"], "opm_flow_restart")

    def test_master_combines_three_source_categories(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)
        requests = sorted(
            (self.runpath / "coupling" / "exchange").glob("network_request_iteration_*.json")
        )
        request = json.loads(requests[-1].read_text(encoding="utf-8"))
        self.assertEqual(set(request["sources"]["simulated_slaves"]), {"model_n", "model_hdn"})
        self.assertEqual(
            set(request["sources"]["prescribed_profiles"]), {"external_satellite"}
        )
        for total in request["totals_by_year"]:
            self.assertAlmostEqual(
                total["network_q_liq_sm3d"],
                total["prescribed_q_liq_sm3d"] + total["simulated_q_liq_sm3d"],
                places=6,
            )


if __name__ == "__main__":
    unittest.main()
