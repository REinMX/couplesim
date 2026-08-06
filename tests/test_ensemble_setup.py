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
STANDALONE = ROOT / "ert" / "bin" / "scripts" / "run_standalone.py"
COLLECT = ROOT / "ert" / "bin" / "scripts" / "collect_ensemble.py"
SCRIPTS = ROOT / "ert" / "bin" / "scripts"

sys.path.insert(0, str(SCRIPTS))
import collect_ensemble  # noqa: E402
import run_coupled  # noqa: E402


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def dummy_config() -> dict:
    config = json.loads((ROOT / "coupling.json").read_text(encoding="utf-8"))
    for slave in config["slaves"]:
        config["slaves"][slave]["backend"] = "dummy"
    return config


class ParameterParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_legacy_shared_q0_mult_still_parses(self) -> None:
        (self.dir / "q0_mult.txt").write_text("Q0_MULT  0.9\n", encoding="utf-8")
        self.assertAlmostEqual(run_coupled.parse_q0_mult(self.dir, allow_default=False), 0.9)

    def test_per_model_parameter_files_take_precedence(self) -> None:
        (self.dir / "q0_mult.txt").write_text("Q0_MULT  0.5\n", encoding="utf-8")
        (self.dir / "q0_mult_model_n.txt").write_text(
            "Q0_MULT_MODEL_N  0.85\n", encoding="utf-8"
        )
        (self.dir / "q0_mult_model_hdn.txt").write_text(
            "Q0_MULT_MODEL_HDN  1.15\n", encoding="utf-8"
        )
        mults = run_coupled.slave_q0_multipliers(self.dir, allow_default=False)
        self.assertAlmostEqual(mults["model_n"], 0.85)
        self.assertAlmostEqual(mults["model_hdn"], 1.15)

    def test_legacy_file_falls_back_for_both_slaves(self) -> None:
        (self.dir / "q0_mult.txt").write_text("Q0_MULT  0.9\n", encoding="utf-8")
        mults = run_coupled.slave_q0_multipliers(self.dir, allow_default=False)
        self.assertAlmostEqual(mults["model_n"], 0.9)
        self.assertAlmostEqual(mults["model_hdn"], 0.9)

    def test_missing_parameters_raise(self) -> None:
        with self.assertRaises(FileNotFoundError):
            run_coupled.slave_q0_multipliers(self.dir, allow_default=False)

    def test_choke_defaults_in_demo_mode(self) -> None:
        self.assertAlmostEqual(
            run_coupled.parse_network_choke(self.dir, allow_default=True), 1.0
        )
        with self.assertRaises(FileNotFoundError):
            run_coupled.parse_network_choke(self.dir, allow_default=False)

    def test_choke_defaults_for_legacy_shared_q0_config(self) -> None:
        # 01_coupled.ert parameterizes only Q0_MULT; the nominal choke applies.
        (self.dir / "q0_mult.txt").write_text("Q0_MULT  0.9\n", encoding="utf-8")
        self.assertAlmostEqual(
            run_coupled.parse_network_choke(self.dir, allow_default=False), 1.0
        )

    def test_apply_network_choke_writes_master_simspec(self) -> None:
        model_dir = self.dir / "master_network"
        model_dir.mkdir()
        (model_dir / "simspec.json").write_text(
            json.dumps({"model": "master_network", "role": "master", "network": {"outlet_pressure_bar": 40.0}}),
            encoding="utf-8",
        )
        run_coupled.apply_network_choke(self.dir, "master_network", 1.25)
        spec = json.loads((model_dir / "simspec.json").read_text(encoding="utf-8"))
        self.assertAlmostEqual(spec["network"]["choke"], 1.25)

    def test_apply_network_choke_rejects_slave_role_and_bad_values(self) -> None:
        model_dir = self.dir / "model_n"
        model_dir.mkdir()
        (model_dir / "simspec.json").write_text(
            json.dumps({"model": "model_n", "role": "slave", "wells": []}),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            run_coupled.apply_network_choke(self.dir, "model_n", 1.1)
        with self.assertRaises(ValueError):
            run_coupled.apply_network_choke(self.dir, "model_n", 0.0)
        with self.assertRaises(ValueError):
            run_coupled.apply_network_choke(self.dir, "model_n", float("nan"))


class StandaloneRunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.temp_dir = Path(cls.temp.name)
        cls.config_path = cls.temp_dir / "coupling-dummy.json"
        cls.config_path.write_text(
            json.dumps(dummy_config(), indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def run_standalone(self, model: str, runpath: Path, *, param_files: dict[str, str]) -> subprocess.CompletedProcess:
        for name, content in param_files.items():
            (runpath / name).write_text(content, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                str(STANDALONE),
                "--model",
                model,
                "--runpath",
                str(runpath),
                "--config",
                str(self.config_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_standalone_slave_dummy_reports_scaled_rates(self) -> None:
        runpath = self.temp_dir / "slave_n"
        runpath.mkdir()
        completed = self.run_standalone(
            "model_n",
            runpath,
            param_files={"q0_mult_model_n.txt": "Q0_MULT_MODEL_N  0.9\n"},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = (runpath / "STANDALONE_REPORT.txt").read_text(encoding="utf-8")
        self.assertIn("STANDALONE SLAVE REPORT", report)
        self.assertIn("q0_mult    : 0.9", report)
        rows = read_csv(runpath / "coupling" / "slave_rates_model_n.csv")
        by_well_year = {(row["well"], row["year"]): float(row["q_liq_sm3d"]) for row in rows}
        # The dummy slave relaxes from the initial guess toward the IPR target
        # (relaxation 0.6): q0 250*0.9=225 -> q_ipr 225 at p_bhp0, so the
        # standalone year-2024 output is 500 + 0.6*(225-500) = 335.
        self.assertAlmostEqual(by_well_year[("N-P1", "2024")], 335.0, places=3)
        self.assertAlmostEqual(by_well_year[("N-P2", "2024")], 402.0, places=3)

    def test_standalone_master_dummy_writes_both_constraints_and_choke_matters(self) -> None:
        base = self.temp_dir / "master_base"
        choked = self.temp_dir / "master_choked"
        base.mkdir()
        choked.mkdir()
        self.assertEqual(
            self.run_standalone(
                "master_network", base, param_files={"network_choke.txt": "NETWORK_CHOKE  1.0\n"}
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_standalone(
                "master_network", choked, param_files={"network_choke.txt": "NETWORK_CHOKE  1.2\n"}
            ).returncode,
            0,
        )
        for model in ("model_n", "model_hdn"):
            base_rows = read_csv(base / "coupling" / f"network_constraints_{model}.csv")
            choked_rows = read_csv(choked / "coupling" / f"network_constraints_{model}.csv")
            self.assertEqual(len(base_rows), 6)  # 2 wells x 3 years
            base_manifold = float(base_rows[0]["p_manifold_bar"])
            choked_manifold = float(choked_rows[0]["p_manifold_bar"])
            self.assertGreater(choked_manifold, base_manifold)
        report = (base / "STANDALONE_REPORT.txt").read_text(encoding="utf-8")
        self.assertIn("STANDALONE MASTER REPORT", report)
        self.assertIn("network_choke   : 1.0", report)


class CollectEnsembleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.case = Path(self.temp.name) / "02_coupled"
        self.case.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_realization(self, number: int, params: dict[str, float], rates: dict[str, float]) -> None:
        real = self.case / f"realization-{number}" / "iter-0"
        (real / "coupling").mkdir(parents=True)
        (real / "OK").write_text("", encoding="utf-8")
        for name, value in params.items():
            (real / f"{name}.txt").write_text(f"{name.upper()}  {value}\n", encoding="utf-8")
        with (real / "coupling" / "convergence_history.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["iteration", "max_fixed_point_residual"])
            writer.writerow(["1", "0.5"])
            writer.writerow(["7", "0.001"])
        with (real / "coupling" / "slave_rates_model_n.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["well", "year", "q_liq_sm3d", "q_gas_sm3d", "p_bhp_bar"])
            writer.writerow(["N-P1", "2024", rates["N-P1"], "0", "280"])
            writer.writerow(["N-P2", "2024", rates["N-P2"], "0", "280"])
        with (real / "coupling" / "slave_rates_model_hdn.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["well", "year", "q_liq_sm3d", "q_gas_sm3d", "p_bhp_bar"])
            writer.writerow(["H-P1", "2024", rates["H-P1"], "0", "275"])

    def test_nearest_rank_percentiles(self) -> None:
        values = list(range(1, 11))
        self.assertEqual(collect_ensemble.percentile_nearest_rank(values, 0.5), 5)
        self.assertEqual(collect_ensemble.percentile_nearest_rank(values, 0.1), 1)
        self.assertEqual(collect_ensemble.percentile_nearest_rank(values, 0.9), 9)

    def test_collect_produces_results_and_summary(self) -> None:
        self.make_realization(
            0,
            {"q0_mult_model_n": 0.9, "q0_mult_model_hdn": 1.1, "network_choke": 0.95},
            {"N-P1": 300.0, "N-P2": 350.0, "H-P1": 220.0},
        )
        self.make_realization(
            1,
            {"q0_mult_model_n": 0.95, "q0_mult_model_hdn": 1.05, "network_choke": 1.1},
            {"N-P1": 310.0, "N-P2": 360.0, "H-P1": 230.0},
        )
        self.make_realization(
            2,
            {"q0_mult_model_n": 1.0, "q0_mult_model_hdn": 1.0, "network_choke": 1.0},
            {"N-P1": 305.0, "N-P2": 355.0, "H-P1": 225.0},
        )
        completed = subprocess.run(
            [sys.executable, str(COLLECT), "--case-dir", str(self.case)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        results = read_csv(self.case / "ensemble_results.csv")
        self.assertEqual(len(results), 3)
        self.assertEqual(float(results[0]["q0_mult_model_n"]), 0.9)
        self.assertEqual(float(results[0]["network_choke"]), 0.95)
        self.assertEqual(results[0]["iterations"], "7")
        self.assertEqual(float(results[0]["N-P1_2024_q_liq_sm3d"]), 300.0)
        self.assertEqual(float(results[0]["H-P1_2024_q_liq_sm3d"]), 220.0)

        summary = read_csv(self.case / "ensemble_summary.csv")
        n_p1 = next(row for row in summary if row["well"] == "N-P1" and row["year"] == "2024")
        # sorted [300, 305, 310]; nearest-rank n=3: P10=1st, P50=2nd, P90=3rd
        self.assertEqual(float(n_p1["p10_sm3d"]), 300.0)
        self.assertEqual(float(n_p1["p50_sm3d"]), 305.0)
        self.assertEqual(float(n_p1["p90_sm3d"]), 310.0)
        self.assertEqual(float(n_p1["mean_sm3d"]), 305.0)
        self.assertEqual(n_p1["n"], "3")

    def test_realizations_without_ok_are_skipped(self) -> None:
        self.make_realization(
            0,
            {"q0_mult_model_n": 1.0, "q0_mult_model_hdn": 1.0, "network_choke": 1.0},
            {"N-P1": 300.0, "N-P2": 350.0, "H-P1": 220.0},
        )
        # realization-1 has no OK file -> must be skipped
        real = self.case / "realization-1" / "iter-0"
        (real / "coupling").mkdir(parents=True)
        completed = subprocess.run(
            [sys.executable, str(COLLECT), "--case-dir", str(self.case)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        results = read_csv(self.case / "ensemble_results.csv")
        self.assertEqual(len(results), 1)


class PerModelParamsFullRunTest(unittest.TestCase):
    """The coupled driver consumes per-model GEN_KW files end to end."""

    def test_legacy_ert_mode_run_with_only_shared_q0_still_works(self) -> None:
        """01_coupled.ert (no NETWORK_CHOKE group) must still run in ERT mode."""
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            config_path = temp_dir / "coupling-dummy.json"
            config = dummy_config()
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            runpath = temp_dir / "run"
            runpath.mkdir()
            (runpath / "q0_mult.txt").write_text("Q0_MULT  0.9\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
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
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = (runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("q0_mult model_n    : 0.9", report)
            self.assertIn("q0_mult model_hdn  : 0.9", report)
            self.assertIn("network_choke      : 1.0", report)
            master_spec = json.loads(
                (runpath / "master_network" / "simspec.json").read_text(encoding="utf-8")
            )
            self.assertAlmostEqual(master_spec["network"]["choke"], 1.0)

    def test_demo_run_with_per_model_parameters_converges(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_dir = Path(temp)
            config_path = temp_dir / "coupling-dummy.json"
            config = dummy_config()
            config["coupling"]["max_iterations"] = 12
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            runpath = temp_dir / "run"
            runpath.mkdir()
            (runpath / "q0_mult_model_n.txt").write_text(
                "Q0_MULT_MODEL_N  0.85\n", encoding="utf-8"
            )
            (runpath / "q0_mult_model_hdn.txt").write_text(
                "Q0_MULT_MODEL_HDN  1.15\n", encoding="utf-8"
            )
            (runpath / "network_choke.txt").write_text(
                "NETWORK_CHOKE  1.1\n", encoding="utf-8"
            )
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
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = (runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
            self.assertIn("q0_mult model_n    : 0.85", report)
            self.assertIn("q0_mult model_hdn  : 1.15", report)
            self.assertIn("network_choke      : 1.1", report)
            master_spec = json.loads(
                (runpath / "master_network" / "simspec.json").read_text(encoding="utf-8")
            )
            self.assertAlmostEqual(master_spec["network"]["choke"], 1.1)


if __name__ == "__main__":
    unittest.main()
