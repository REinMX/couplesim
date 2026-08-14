#!/usr/bin/env python3
"""Eclipse backend tests: licence-free, fully mocked.

The eclipse backend (ert/bin/backends/eclipse_slave_adapter.py) drives a
proprietary simulator through the `eclrun` launcher. These tests stand in
scripted mock executables for both `eclrun` and OPM's `summary` CLI, so the
whole chain -- deck rendering, restart staging, summary extraction,
statefulness checks, exchange schema -- is exercised without a licence or a
simulator, plus a full coupled realization through run_coupled.py with both
slaves on the mocked eclipse backend and no prescribed profiles (the fully
two-way mode).
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "ert" / "bin" / "backends" / "eclipse_slave_adapter.py"
DRIVER = ROOT / "ert" / "bin" / "scripts" / "run_coupled.py"
TWOWAY_CONFIG = ROOT / "configs" / "coupling.twoway.json"

MOCK_ECLRUN = """\
#!/usr/bin/env python3
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("Mock Eclipse 2026.1")
    raise SystemExit(0)
deck = Path(sys.argv[1])
stem = deck.name[:-5]
for suffix in (".SMSPEC", ".UNSMRY", ".UNRST", ".EGRID"):
    (Path.cwd() / f"{stem}{suffix}").write_bytes(b"mock-eclipse-output")
"""

MOCK_SUMMARY = """\
#!/usr/bin/env python3
import sys
from pathlib import Path

argv = sys.argv[1:]
marker = argv.index("-r")
smspec = Path(argv[marker + 1])
vectors = argv[marker + 2 :]
year = int(smspec.name[:-7].rsplit("_", 1)[1])
print(" ".join(vectors))
row = []
for vector in vectors:
    if vector == "FOPT":
        value = 1000.0 + (year - 2024) * 100.0
    elif vector == "FWPT":
        value = 500.0 + (year - 2024) * 50.0
    elif vector == "FPR":
        value = 300.0
    elif vector.startswith("WOPR"):
        value = 500.0
    elif vector.startswith("WWPR"):
        value = 100.0
    elif vector.startswith("WBHP"):
        value = 290.0
    else:
        value = 0.0
    row.append(f"{value:.6f}")
print(" ".join(row))
"""


def write_mock_executables(directory: Path) -> tuple[Path, Path]:
    eclrun = directory / "mock-eclrun"
    summary = directory / "mock-summary"
    eclrun.write_text(MOCK_ECLRUN, encoding="utf-8")
    summary.write_text(MOCK_SUMMARY, encoding="utf-8")
    eclrun.chmod(0o755)
    summary.chmod(0o755)
    return eclrun, summary


def write_constraints(path: Path, rows: list[tuple[str, int, float]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
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
        )
        for well, year, bhp in rows:
            writer.writerow([well, year, 500.0, 0.0, 500.0, 500.0, 40.0, 41.0, bhp])


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class EclipseAdapterMockTest(unittest.TestCase):
    def run_adapter(
        self,
        constraints: Path,
        output_dir: Path,
        eclrun: Path,
        summary: Path,
        model: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(ADAPTER),
                "--constraints",
                str(constraints),
                "--output-dir",
                str(output_dir),
                "--model",
                model,
                "--eclrun",
                str(eclrun),
                "--summary",
                str(summary),
            ],
            cwd=constraints.parent,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_model_a_chain_stages_restart_and_emits_exchange_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            eclrun, summary = write_mock_executables(temp)
            constraints = temp / "constraints.csv"
            write_constraints(
                constraints,
                [
                    ("A-P1", 2024, 300.0),
                    ("A-P2", 2024, 320.0),
                    ("A-P1", 2025, 295.0),
                    ("A-P2", 2025, 315.0),
                    ("A-P1", 2026, 290.0),
                    ("A-P2", 2026, 310.0),
                ],
            )
            out = temp / "out"
            completed = self.run_adapter(constraints, out, eclrun, summary, "model_a")
            self.assertEqual(completed.returncode, 0, completed.stderr)

            rates = read_csv_rows(out / "slave_rates_model_a.csv")
            self.assertEqual(len(rates), 6)
            self.assertEqual({row["well"] for row in rates}, {"A-P1", "A-P2"})
            self.assertEqual({int(row["year"]) for row in rates}, {2024, 2025, 2026})
            self.assertTrue(all(row["origin"] == "eclipse_restart" for row in rates))
            self.assertTrue(all(float(row["q_liq_sm3d"]) == 600.0 for row in rates))

            report = json.loads((out / "restart_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["model"], "model_a")
            self.assertEqual(report["simulator"], "Mock Eclipse 2026.1")
            self.assertEqual(report["checks"]["fopt_strictly_increasing"], True)
            self.assertEqual(report["checks"]["rates_positive"], True)
            # The continuation decks must see the previous year's restart
            # artifacts staged beside them.
            self.assertTrue((out / "year-2025" / "MODEL_A_2024.UNRST").is_file())
            self.assertTrue((out / "year-2025" / "MODEL_A_2024.EGRID").is_file())
            self.assertTrue((out / "year-2026" / "MODEL_A_2025.UNRST").is_file())
            # Every year's deck renders and the markers resolve.
            for year in (2024, 2025, 2026):
                deck = out / f"year-{year}" / f"MODEL_A_{year}.DATA"
                rendered = deck.read_text(encoding="utf-8")
                self.assertNotIn("__", rendered)
                self.assertIn(f"START\n  1 JAN {year} /", rendered)

    def test_model_b_chain_uses_b_wells_and_315_bar_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            eclrun, summary = write_mock_executables(temp)
            constraints = temp / "constraints.csv"
            write_constraints(
                constraints,
                [
                    ("B-P1", 2024, 280.0),
                    ("B-P2", 2024, 295.0),
                    ("B-P1", 2025, 275.0),
                    ("B-P2", 2025, 290.0),
                    ("B-P1", 2026, 270.0),
                    ("B-P2", 2026, 285.0),
                ],
            )
            out = temp / "out"
            completed = self.run_adapter(constraints, out, eclrun, summary, "model_b")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            rates = read_csv_rows(out / "slave_rates_model_b.csv")
            self.assertEqual({row["well"] for row in rates}, {"B-P1", "B-P2"})
            self.assertEqual(len(rates), 6)

    def test_chain_fails_fast_when_eclrun_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            _, summary = write_mock_executables(temp)
            constraints = temp / "constraints.csv"
            write_constraints(
                constraints,
                [("A-P1", year, 300.0) for year in (2024, 2025, 2026)]
                + [("A-P2", year, 320.0) for year in (2024, 2025, 2026)],
            )
            out = temp / "out"
            completed = self.run_adapter(
                constraints, out, temp / "no-such-eclrun", summary, "model_a"
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("required executable", completed.stderr)
            self.assertFalse((out / "restart_report.json").exists())


class EclipseBackendDriverTest(unittest.TestCase):
    """One full two-way realization: dummy master, both slaves on the mocked
    eclipse backend, and NO prescribed profiles (model_b volumes come only
    from its own simulation)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp = Path(self.temp_dir.name)
        self.eclrun, self.summary = write_mock_executables(self.temp)
        config = json.loads(TWOWAY_CONFIG.read_text(encoding="utf-8"))
        for slave in config["slaves"].values():
            slave["backend"] = "eclipse"
            slave["eclrun"] = str(self.eclrun)
            slave["summary"] = str(self.summary)
        config["coupling"]["tolerance"] = 0.05  # deterministic convergence with fixed mock rates
        self.config = self.temp / "coupling-eclipse.json"
        self.config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        self.runpath = self.temp / "run"
        self.completed = subprocess.run(
            [
                sys.executable,
                str(DRIVER),
                "--demo",
                "--config",
                str(self.config),
                "--runpath",
                str(self.runpath),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_two_way_realization_converges(self) -> None:
        self.assertEqual(self.completed.returncode, 0, self.completed.stderr)

    def test_exchange_shows_both_simulated_slaves_and_no_prescribed_profiles(self) -> None:
        reports = sorted(
            (self.runpath / "coupling" / "flow_master").glob(
                "iteration-*/master_report.json"
            )
        )
        self.assertTrue(reports)
        report = json.loads(reports[-1].read_text(encoding="utf-8"))
        self.assertEqual(set(report["requested_rates"]), {"model_a", "model_b"})
        self.assertEqual(report["prescribed_profile"], {})

    def test_convergence_history_and_report(self) -> None:
        history = read_csv_rows(self.runpath / "coupling" / "convergence_history.csv")
        self.assertGreater(len(history), 1)
        report = (self.runpath / "COUPLED_REPORT.txt").read_text(encoding="utf-8")
        self.assertIn("slave backends     : model_a=eclipse, model_b=eclipse", report)
        self.assertIn("prescribed profiles : ", report)

    def test_slave_rates_carry_relaxed_eclipse_origin(self) -> None:
        for model in ("model_a", "model_b"):
            rows = read_csv_rows(self.runpath / "coupling" / f"slave_rates_{model}.csv")
            self.assertTrue(rows)
            self.assertTrue(all(row["origin"] == "eclipse_restart_relaxed" for row in rows))
            exchange = json.loads(
                (
                    self.runpath
                    / "coupling"
                    / "exchange"
                    / f"slave_result_{model}_iteration_001.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(exchange["backend"], "eclipse_restart")

    def test_invalid_configured_eclrun_fails_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config = json.loads(TWOWAY_CONFIG.read_text(encoding="utf-8"))
            for slave in config["slaves"].values():
                slave["backend"] = "eclipse"
                slave["eclrun"] = str(temp / "missing-eclrun")
                slave["summary"] = "summary"
            config_path = temp / "bad.json"
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--demo",
                    "--config",
                    str(config_path),
                    "--runpath",
                    str(temp / "run"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("configured eclrun path does not exist", completed.stderr)
            self.assertFalse((temp / "run" / "coupling").exists())


if __name__ == "__main__":
    unittest.main()
