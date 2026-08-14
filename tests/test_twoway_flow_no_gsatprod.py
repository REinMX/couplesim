from __future__ import annotations

import csv
import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "ert" / "bin" / "scripts" / "run_coupled.py"
MASTER_ADAPTER = ROOT / "spikes" / "004-opm-flow-master" / "opm_flow_master_adapter.py"
SLAVE_ADAPTER = (
    ROOT / "spikes" / "003-opm-model-n-restart" / "opm_model_n_restart_adapter.py"
)
TWOWAY_TEMPLATE = ROOT / "spikes" / "004-opm-flow-master" / "MASTER_FLOW_TWOWAY.DATA.tmpl"
ERT_CONFIG = ROOT / "ert" / "model" / "02_ensemble_coupled.ert"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TwoWayFlowContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.driver = load_module("run_coupled_twoway_contract", DRIVER)
        cls.adapter = load_module("flow_master_twoway_contract", MASTER_ADAPTER)

    def test_primary_config_is_all_real_without_prescribed_profiles(self) -> None:
        config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
        self.assertEqual(config["prescribed_network_profiles"], {})
        self.assertEqual(config["master"]["backend"], "flow")
        self.assertEqual(config["slaves"]["model_a"]["backend"], "flow")
        self.assertEqual(config["slaves"]["model_b"]["backend"], "flow")
        self.assertAlmostEqual(config["coupling"]["relaxation"], 0.4)
        self.assertGreaterEqual(config["coupling"]["max_iterations"], 20)

    def test_flow_topology_accepts_no_external_profile(self) -> None:
        config = json.loads(
            (ROOT / "configs" / "coupling.twoway.json").read_text(encoding="utf-8")
        )
        config["master"]["backend"] = "flow"
        config["slaves"]["model_a"]["backend"] = "flow"
        config["slaves"]["model_b"]["backend"] = "flow"
        self.driver.validate_topology(config)

    def test_twoway_master_template_has_no_gsatprod(self) -> None:
        text = TWOWAY_TEMPLATE.read_text(encoding="utf-8")
        self.assertNotIn("GSATPROD", text)
        self.assertNotIn("SAT", text)
        for well in ("A-P1", "A-P2", "B-P1", "B-P2"):
            self.assertIn(well, text)

    def test_master_adapter_profile_is_optional(self) -> None:
        profile = inspect.signature(self.adapter.execute).parameters["profile"]
        self.assertIsNone(profile.default)
        self.assertIn("None", str(profile.annotation))

    def test_ert_declares_100_realizations_and_one_coupled_step(self) -> None:
        text = ERT_CONFIG.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^NUM_REALIZATIONS\s+100\s*$")
        self.assertEqual(text.count("FORWARD_MODEL RUN_COUPLED"), 1)
        self.assertNotIn("GSATPROD", text)
        self.assertNotIn("NETWORK_CHOKE", text)

    def test_two_model_ensemble_uses_fixed_nominal_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runpath = Path(temp_dir)
            (runpath / "q0_mult_model_a.txt").write_text(
                "Q0_MULT_MODEL_A 0.9\n", encoding="utf-8"
            )
            (runpath / "q0_mult_model_b.txt").write_text(
                "Q0_MULT_MODEL_B 1.1\n", encoding="utf-8"
            )
            self.assertEqual(
                self.driver.parse_network_choke(runpath, allow_default=False),
                1.0,
            )


@unittest.skipUnless(
    shutil.which("flow") is not None and shutil.which("summary") is not None,
    "requires installed OPM Flow and summary CLI",
)
class TwoWayFlowMasterIntegrationTest(unittest.TestCase):
    def test_real_master_runs_without_external_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            rate_files: dict[str, Path] = {}
            for model, wells, base in (
                ("model_a", ("A-P1", "A-P2"), 1200.0),
                ("model_b", ("B-P1", "B-P2"), 700.0),
            ):
                path = temp / f"{model}.csv"
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["well", "year", "q_liq_sm3d"])
                    for index, well in enumerate(wells):
                        for year, decline in ((2024, 1.0), (2025, 0.85), (2026, 0.70)):
                            writer.writerow([well, year, base * (1.0 - 0.1 * index) * decline])
                rate_files[model] = path

            output = temp / "master"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(MASTER_ADAPTER),
                    "--rates",
                    "model_a",
                    str(rate_files["model_a"]),
                    "--rates",
                    "model_b",
                    str(rate_files["model_b"]),
                    "--output-dir",
                    str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            rendered = (output / "MASTER_FLOW.DATA").read_text(encoding="utf-8")
            self.assertNotIn("GSATPROD", rendered)
            report = json.loads((output / "master_report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["checks"]["external_profile_used"])
            self.assertEqual(report["prescribed_profile"], {})
            prt = (output / "flow-run" / "MASTER_FLOW.PRT").read_text(
                encoding="utf-8"
            )
            self.assertIn("Error summary:", prt)
            self.assertRegex(prt, r"Warnings\s+0")
            self.assertRegex(prt, r"Errors\s+0")
            self.assertRegex(prt, r"Problems\s+0")
            for model in ("model_a", "model_b"):
                with (output / f"network_constraints_{model}.csv").open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(len(rows), 6)
                self.assertTrue(all(float(row["prescribed_q_liq_sm3d"]) == 0.0 for row in rows))
                self.assertTrue(
                    all(
                        float(row["total_network_q_liq_sm3d"])
                        == float(row["simulated_q_liq_sm3d"])
                        for row in rows
                    )
                )


@unittest.skipUnless(
    shutil.which("flow") is not None and shutil.which("summary") is not None,
    "requires installed OPM Flow and summary CLI",
)
class TwoWayFlowSlaveEnsembleIntegrationTest(unittest.TestCase):
    def test_productivity_multiplier_changes_real_flow_deck_and_rates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            constraints = temp / "constraints.csv"
            with constraints.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["well", "year", "p_bhp_bar"])
                for year in (2024, 2025, 2026):
                    writer.writerow(["A-P1", year, 300.0])
                    writer.writerow(["A-P2", year, 310.0])

            rates_by_multiplier: dict[float, float] = {}
            for multiplier in (0.8, 1.2):
                output = temp / f"m-{multiplier}"
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SLAVE_ADAPTER),
                        "--constraints",
                        str(constraints),
                        "--output-dir",
                        str(output),
                        "--model",
                        "model_a",
                        "--productivity-multiplier",
                        str(multiplier),
                    ],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                deck = (output / "year-2024" / "MODEL_A_2024.DATA").read_text(
                    encoding="utf-8"
                )
                self.assertIn(f"5*{100.0 * multiplier:.6f}", deck)
                prt = (
                    output
                    / "year-2026"
                    / "flow-run"
                    / "MODEL_A_2026.PRT"
                ).read_text(encoding="utf-8")
                self.assertIn("Error summary:", prt)
                self.assertRegex(prt, r"Warnings\s+0")
                self.assertRegex(prt, r"Errors\s+0")
                self.assertRegex(prt, r"Problems\s+0")
                with (output / "slave_rates_model_a.csv").open(
                    newline="", encoding="utf-8"
                ) as handle:
                    rows = list(csv.DictReader(handle))
                rates_by_multiplier[multiplier] = float(rows[0]["q_liq_sm3d"])

            self.assertGreater(rates_by_multiplier[1.2], rates_by_multiplier[0.8])


if __name__ == "__main__":
    unittest.main()
