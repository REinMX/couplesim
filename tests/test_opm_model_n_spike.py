from __future__ import annotations

import csv
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes" / "001-opm-model-n-roundtrip"
ADAPTER = SPIKE / "opm_model_n_adapter.py"
FLOW_AVAILABLE = shutil.which("flow") is not None and shutil.which("summary") is not None


class OpmModelNRoundtripTest(unittest.TestCase):
    def write_constraints(self, path: Path, rows: list[tuple[str, int, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["well", "year", "p_bhp_bar"])
            writer.writerows(rows)

    def run_adapter(
        self,
        constraints: Path,
        output_dir: Path,
        *,
        flow: str | None = None,
        summary: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(ADAPTER),
            "--constraints",
            str(constraints),
            "--year",
            "2024",
            "--output-dir",
            str(output_dir),
        ]
        if flow is not None:
            command.extend(["--flow", flow])
        if summary is not None:
            command.extend(["--summary", summary])
        return subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_rendered_deck_uses_selected_constraint_year(self) -> None:
        spec = importlib.util.spec_from_file_location("opm_model_n_adapter", ADAPTER)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temp_dir:
            deck = Path(temp_dir) / "MODEL_N_FLOW.DATA"
            module.render_deck(deck, {"N-P1": 300.0, "N-P2": 320.0}, 2025)
            rendered = deck.read_text(encoding="utf-8")
            self.assertIn("START\n  1 JAN 2025 /", rendered)
            self.assertNotIn("__START_YEAR__", rendered)

    @unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
    def test_relative_executable_overrides_survive_output_cwd_change(self) -> None:
        flow = shutil.which("flow")
        summary = shutil.which("summary")
        self.assertIsNotNone(flow)
        self.assertIsNotNone(summary)
        output_root = ROOT / "output"
        output_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=output_root) as temp_dir:
            temp = Path(temp_dir)
            tools = temp / "tools"
            tools.mkdir()
            flow_link = tools / "flow"
            summary_link = tools / "summary"
            flow_link.symlink_to(Path(flow).resolve())
            summary_link.symlink_to(Path(summary).resolve())
            relative_flow = os.path.relpath(flow_link, ROOT)
            relative_summary = os.path.relpath(summary_link, ROOT)
            constraints = temp / "constraints.csv"
            self.write_constraints(
                constraints,
                [("N-P1", 2024, 300.0), ("N-P2", 2024, 320.0)],
            )
            completed = self.run_adapter(
                constraints,
                temp / "run",
                flow=relative_flow,
                summary=relative_summary,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((temp / "run" / "slave_rates_model_n.csv").is_file())

    @unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
    def test_real_flow_rates_decrease_when_bhp_constraints_increase(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            low_constraints = temp / "low.csv"
            high_constraints = temp / "high.csv"
            self.write_constraints(
                low_constraints,
                [("N-P1", 2024, 300.0), ("N-P2", 2024, 320.0)],
            )
            self.write_constraints(
                high_constraints,
                [("N-P1", 2024, 320.0), ("N-P2", 2024, 330.0)],
            )

            low_completed = self.run_adapter(low_constraints, temp / "low-run")
            high_completed = self.run_adapter(high_constraints, temp / "high-run")
            self.assertEqual(low_completed.returncode, 0, low_completed.stderr)
            self.assertEqual(high_completed.returncode, 0, high_completed.stderr)

            def read_rates(run_dir: Path) -> dict[str, dict[str, str]]:
                with (run_dir / "slave_rates_model_n.csv").open(
                    newline="", encoding="utf-8"
                ) as handle:
                    return {row["well"]: row for row in csv.DictReader(handle)}

            low = read_rates(temp / "low-run")
            high = read_rates(temp / "high-run")
            self.assertEqual(set(low), {"N-P1", "N-P2"})
            self.assertEqual(set(high), {"N-P1", "N-P2"})
            for well in ("N-P1", "N-P2"):
                low_rate = float(low[well]["q_liq_sm3d"])
                high_rate = float(high[well]["q_liq_sm3d"])
                self.assertTrue(math.isfinite(low_rate) and low_rate > 0.0)
                self.assertTrue(math.isfinite(high_rate) and high_rate > 0.0)
                self.assertLess(high_rate, low_rate)
                self.assertEqual(float(low[well]["q_gas_sm3d"]), 0.0)
                self.assertEqual(float(low[well]["q_ipr_sm3d"]), low_rate)
                self.assertEqual(low[well]["origin"], "opm_flow")

            low_report = json.loads(
                (temp / "low-run" / "roundtrip_report.json").read_text(encoding="utf-8")
            )
            self.assertTrue(low_report["simulator"].startswith("flow "))
            self.assertEqual(low_report["year"], 2024)
            self.assertTrue((temp / "low-run" / "flow-run" / "MODEL_N_FLOW.SMSPEC").is_file())
            rendered_deck = (temp / "low-run" / "MODEL_N_FLOW.DATA").read_text(
                encoding="utf-8"
            )
            self.assertIn("START\n  1 JAN 2024 /", rendered_deck)
            self.assertIn("'N-P1' 'OPEN' 'BHP' 5* 300.000000 /", rendered_deck)
            self.assertIn("'N-P2' 'OPEN' 'BHP' 5* 320.000000 /", rendered_deck)

    def test_duplicate_constraint_is_rejected_before_flow_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            constraints = temp / "duplicate.csv"
            self.write_constraints(
                constraints,
                [
                    ("N-P1", 2024, 300.0),
                    ("N-P1", 2024, 301.0),
                    ("N-P2", 2024, 320.0),
                ],
            )
            output_dir = temp / "run"
            completed = self.run_adapter(constraints, output_dir)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("duplicate constraint", completed.stderr)
            self.assertFalse((output_dir / "flow-run").exists())

    def test_missing_or_non_finite_constraint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for name, rows, expected in (
                ("missing", [("N-P1", 2024, 300.0)], "missing constraints"),
                (
                    "non-finite",
                    [("N-P1", 2024, "NaN"), ("N-P2", 2024, 320.0)],
                    "finite positive",
                ),
            ):
                with self.subTest(name=name):
                    constraints = temp / f"{name}.csv"
                    self.write_constraints(constraints, rows)
                    output_dir = temp / f"{name}-run"
                    completed = self.run_adapter(constraints, output_dir)
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn(expected, completed.stderr)
                    self.assertFalse((output_dir / "flow-run").exists())


if __name__ == "__main__":
    unittest.main()
