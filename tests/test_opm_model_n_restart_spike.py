from __future__ import annotations

import csv
import importlib.util
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes" / "003-opm-model-n-restart"
ADAPTER = SPIKE / "opm_model_n_restart_adapter.py"
FLOW_AVAILABLE = shutil.which("flow") is not None and shutil.which("summary") is not None

REAL_CONSTRAINTS = (
    ROOT / "output" / "demo" / "realization-0" / "coupling" / "network_constraints_model_a.csv"
)


def load_adapter():
    spec = importlib.util.spec_from_file_location("opm_model_a_restart_adapter", ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpmModelNRestartValidationTest(unittest.TestCase):
    def write_constraints(self, path: Path, rows: list[tuple[str, int, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["well", "year", "p_bhp_bar"])
            writer.writerows(rows)

    def valid_rows(self) -> list[tuple[str, int, object]]:
        return [
            ("A-P1", 2024, 300.0),
            ("A-P2", 2024, 320.0),
            ("A-P1", 2025, 290.0),
            ("A-P2", 2025, 310.0),
            ("A-P1", 2026, 280.0),
            ("A-P2", 2026, 300.0),
        ]

    def hdn_rows(self) -> list[tuple[str, int, object]]:
        return [
            ("B-P1", 2024, 280.0),
            ("B-P2", 2024, 295.0),
            ("B-P1", 2025, 275.0),
            ("B-P2", 2025, 290.0),
            ("B-P1", 2026, 270.0),
            ("B-P2", 2026, 285.0),
        ]

    def test_days_in_year_leap_and_common(self) -> None:
        module = load_adapter()
        self.assertEqual(module.days_in_year(2024), 366)
        self.assertEqual(module.days_in_year(2025), 365)
        self.assertEqual(module.days_in_year(2026), 365)
        self.assertEqual(module.days_in_year(2000), 366)
        self.assertEqual(module.days_in_year(2100), 365)

    def test_constraints_chain_parses_all_years_and_wells(self) -> None:
        module = load_adapter()
        wells = module.MODEL_CONFIGS["model_a"]["wells"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "constraints.csv"
            self.write_constraints(path, self.valid_rows())
            parsed = module.read_constraints_chain(path, wells, 350.0)
            self.assertEqual(set(parsed), {2024, 2025, 2026})
            self.assertEqual(parsed[2024], {"A-P1": 300.0, "A-P2": 320.0})
            self.assertEqual(parsed[2026]["A-P2"], 300.0)

    def test_model_b_constraints_use_h_wells_and_315_bar_cap(self) -> None:
        module = load_adapter()
        wells = module.MODEL_CONFIGS["model_b"]["wells"]
        self.assertEqual(wells, ("B-P1", "B-P2"))
        self.assertEqual(module.MODEL_CONFIGS["model_b"]["initial_pressure_bar"], 315.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hdn.csv"
            self.write_constraints(path, self.hdn_rows())
            parsed = module.read_constraints_chain(path, wells, 315.0)
            self.assertEqual(set(parsed), {2024, 2025, 2026})
            self.assertEqual(parsed[2024], {"B-P1": 280.0, "B-P2": 295.0})
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "hdn-over.csv"
            self.write_constraints(path, [(w, y, 316.0) for w, y in [("B-P1", 2024), ("B-P2", 2024), ("B-P1", 2025), ("B-P2", 2025), ("B-P1", 2026), ("B-P2", 2026)]])
            with self.assertRaises(ValueError) as ctx:
                module.read_constraints_chain(path, wells, 315.0)
            self.assertIn("below 315.0", str(ctx.exception))

    def test_constraints_chain_rejects_bad_inputs(self) -> None:
        module = load_adapter()
        wells = module.MODEL_CONFIGS["model_a"]["wells"]
        cases = (
            ("missing-year", [row for row in self.valid_rows() if row[1] != 2026], "missing constraint rows for years"),
            ("missing-well", [row for row in self.valid_rows() if row != ("A-P2", 2024, 320.0)], "missing constraints for year 2024"),
            ("duplicate", self.valid_rows() + [("A-P1", 2024, 299.0)], "duplicate constraint"),
            ("non-finite", [("A-P1", 2024, 300.0), ("A-P2", 2024, "NaN"), ("A-P1", 2025, 290.0), ("A-P2", 2025, 310.0), ("A-P1", 2026, 280.0), ("A-P2", 2026, 300.0)], "finite positive"),
            ("at-initial-pressure", [("A-P1", 2024, 300.0), ("A-P2", 2024, 350.0), ("A-P1", 2025, 290.0), ("A-P2", 2025, 310.0), ("A-P1", 2026, 280.0), ("A-P2", 2026, 300.0)], "must be below 350.0"),
            ("unexpected-year", [("A-P1", 2024, 300.0), ("A-P2", 2024, 320.0), ("A-P1", 2025, 290.0), ("A-P2", 2025, 310.0), ("A-P1", 2026, 280.0), ("A-P2", 2027, 300.0)], "unexpected constraint year"),
            ("unexpected-well", [("A-P1", 2024, 300.0), ("A-P2", 2024, 320.0), ("A-P1", 2025, 290.0), ("A-P2", 2025, 310.0), ("A-P1", 2026, 280.0), ("N-P3", 2026, 300.0)], "unexpected constraint well"),
        )
        for name, rows, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / f"{name}.csv"
                self.write_constraints(path, rows)
                with self.assertRaises(ValueError) as ctx:
                    module.read_constraints_chain(path, wells, 350.0)
                self.assertIn(expected, str(ctx.exception))

    def test_rendered_base_deck_replaces_all_markers(self) -> None:
        module = load_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            deck = Path(temp_dir) / "MODEL_A_2024.DATA"
            module.render_deck(
                deck,
                module.BASE_TEMPLATE,
                {
                    "__START_YEAR__": "2024",
                    "__WELL_1__": "A-P1",
                    "__WELL_2__": "A-P2",
                    "__WELL_1_BHP_BAR__": "301.327561",
                    "__WELL_2_BHP_BAR__": "321.860831",
                    "__INITIAL_PRESSURE_BAR__": "350.000000",
                    "__YEAR_DAYS__": "366",
                },
            )
            rendered = deck.read_text(encoding="utf-8")
            self.assertIn("START\n  1 JAN 2024 /", rendered)
            self.assertIn("'A-P1' 'OPEN' 'BHP' 5* 301.327561 /", rendered)
            self.assertIn("'A-P2' 'OPEN' 'BHP' 5* 321.860831 /", rendered)
            self.assertIn("TSTEP\n  366 /", rendered)
            self.assertIn("2010 350.000000 3000.0", rendered)
            self.assertNotIn("__START_YEAR__", rendered)
            self.assertNotIn("__WELL_1__", rendered)
            self.assertNotIn("__WELL_1_BHP_BAR__", rendered)
            self.assertNotIn("__YEAR_DAYS__", rendered)
            self.assertNotIn("RESTART\n", rendered)

    def test_rendered_hdn_deck_uses_h_wells(self) -> None:
        module = load_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            deck = Path(temp_dir) / "MODEL_B_2024.DATA"
            module.render_deck(
                deck,
                module.BASE_TEMPLATE,
                {
                    "__START_YEAR__": "2024",
                    "__WELL_1__": "B-P1",
                    "__WELL_2__": "B-P2",
                    "__WELL_1_BHP_BAR__": "285.500000",
                    "__WELL_2_BHP_BAR__": "297.900000",
                    "__INITIAL_PRESSURE_BAR__": "315.000000",
                    "__YEAR_DAYS__": "366",
                },
            )
            rendered = deck.read_text(encoding="utf-8")
            self.assertIn("'B-P1' 'OPEN' 'BHP' 5* 285.500000 /", rendered)
            self.assertIn("'B-P2' 'OPEN' 'BHP' 5* 297.900000 /", rendered)
            self.assertIn("2010 315.000000 3000.0", rendered)
            self.assertNotIn("__WELL_1__", rendered)

    def test_rendered_continuation_deck_uses_restart_case_and_step(self) -> None:
        module = load_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            deck = Path(temp_dir) / "MODEL_A_2025.DATA"
            module.render_deck(
                deck,
                module.CONTINUE_TEMPLATE,
                {
                    "__START_YEAR__": "2025",
                    "__WELL_1__": "A-P1",
                    "__WELL_2__": "A-P2",
                    "__WELL_1_BHP_BAR__": "300.948929",
                    "__WELL_2_BHP_BAR__": "321.515758",
                    "__YEAR_DAYS__": "365",
                    "__RESTART_CASE__": "MODEL_A_2024",
                    "__RESTART_STEP__": "1",
                },
            )
            rendered = deck.read_text(encoding="utf-8")
            self.assertIn("START\n  1 JAN 2025 /", rendered)
            self.assertIn("'MODEL_A_2024' 1 /", rendered)
            self.assertIn("TSTEP\n  365 /", rendered)
            self.assertNotIn("__RESTART_CASE__", rendered)
            self.assertNotIn("__RESTART_STEP__", rendered)
            self.assertNotIn("EQUIL\n", rendered)

    def test_missing_marker_is_rejected(self) -> None:
        module = load_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            deck = Path(temp_dir) / "MODEL_A_2026.DATA"
            with self.assertRaises(ValueError):
                module.render_deck(
                    deck,
                    module.CONTINUE_TEMPLATE,
                    {
                        "__START_YEAR__": "2026",
                        "__WELL_1__": "A-P1",
                        "__WELL_2__": "A-P2",
                        "__WELL_1_BHP_BAR__": "300.586081",
                        "__WELL_2_BHP_BAR__": "321.185606",
                        "__YEAR_DAYS__": "365",
                        "__RESTART_CASE__": "MODEL_A_2025",
                    },
                )

    def test_non_empty_output_dir_is_rejected_before_executables(self) -> None:
        module = load_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            constraints = Path(temp_dir) / "constraints.csv"
            self.write_constraints(constraints, self.valid_rows())
            output_dir = Path(temp_dir) / "run"
            output_dir.mkdir()
            (output_dir / "leftover.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                module.run_chain(
                    constraints,
                    output_dir,
                    "flow-binary-that-does-not-exist",
                    "summary-binary-that-does-not-exist",
                )
            self.assertIn("must be absent or empty", str(ctx.exception))

    def test_unsupported_model_is_rejected(self) -> None:
        module = load_adapter()
        with tempfile.TemporaryDirectory() as temp_dir:
            constraints = Path(temp_dir) / "constraints.csv"
            self.write_constraints(constraints, self.valid_rows())
            with self.assertRaises(ValueError) as ctx:
                module.run_chain(
                    constraints,
                    Path(temp_dir) / "run",
                    "flow-binary-that-does-not-exist",
                    "summary-binary-that-does-not-exist",
                    model="model_xyz",
                )
            self.assertIn("unsupported model", str(ctx.exception))

    def test_missing_constraint_file_is_rejected(self) -> None:
        module = load_adapter()
        with self.assertRaises(FileNotFoundError):
            module.read_constraints_chain(
                Path("/nonexistent/constraints.csv"), ("A-P1", "A-P2"), 350.0
            )

    def test_chain_run_fails_fast_without_flow(self) -> None:
        module = load_adapter()
        if FLOW_AVAILABLE:
            self.skipTest("flow is installed; this test covers the no-flow failure path")
        with tempfile.TemporaryDirectory() as temp_dir:
            constraints = Path(temp_dir) / "constraints.csv"
            self.write_constraints(constraints, self.valid_rows())
            with self.assertRaises(FileNotFoundError):
                module.run_chain(constraints, Path(temp_dir) / "run", "flow", "summary")


@unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
class OpmModelNRestartIntegrationTest(unittest.TestCase):
    def test_real_three_year_chain_carries_state_and_honours_constraints(self) -> None:
        module = load_adapter()
        if not REAL_CONSTRAINTS.is_file():
            self.skipTest("demo coupling constraints not present under output/")
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "chain"
            rates_path = module.run_chain(REAL_CONSTRAINTS, output_dir, "flow", "summary")

            with rates_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 6)
            self.assertEqual({(row["well"], int(row["year"])) for row in rows}, {
                (well, year) for year in (2024, 2025, 2026) for well in ("A-P1", "A-P2")
            })
            for row in rows:
                self.assertEqual(row["origin"], "opm_flow_restart")
                self.assertEqual(float(row["q_gas_sm3d"]), 0.0)
                self.assertGreater(float(row["q_liq_sm3d"]), 0.0)
                self.assertEqual(float(row["q_ipr_sm3d"]), float(row["q_liq_sm3d"]))
                self.assertTrue(math.isfinite(float(row["p_res_bar"])))

            report = json.loads((output_dir / "restart_report.json").read_text(encoding="utf-8"))
            self.assertTrue(report["simulator"].startswith("flow "))
            self.assertEqual(report["model"], "model_a")
            self.assertEqual(report["checks"]["fopt_strictly_increasing"], True)
            self.assertEqual(report["checks"]["fwpt_non_decreasing"], True)
            self.assertEqual(report["checks"]["wbhp_matches_constraint"], True)
            self.assertEqual(report["checks"]["rates_positive"], True)
            for year in (2024, 2025, 2026):
                self.assertTrue(
                    (output_dir / f"year-{year}" / "flow-run" / f"MODEL_A_{year}.UNRST").is_file()
                )
                self.assertTrue(
                    (output_dir / f"year-{year}" / "flow-run" / f"MODEL_A_{year}.SMSPEC").is_file()
                )

            fopt = {result["year"]: result["field"]["fopt_sm3"] for result in report["year_results"]}
            # Statefulness: two carried years must far exceed one fresh year's
            # production. A silently re-initialized chain would give ~1x.
            self.assertGreater(fopt[2025], 1.3 * fopt[2024])
            self.assertGreater(fopt[2026], fopt[2025])

            # Fresh-2025 comparison: a fresh run produces only its own year;
            # the restarted 2025 run carries most of 2024's cumulative.
            fresh_dir = Path(temp_dir) / "fresh-2025"
            fresh_dir.mkdir()
            fresh_deck = fresh_dir / "MODEL_A_FRESH2025.DATA"
            module.render_deck(
                fresh_deck,
                module.BASE_TEMPLATE,
                {
                    "__START_YEAR__": "2025",
                    "__WELL_1__": "A-P1",
                    "__WELL_2__": "A-P2",
                    "__WELL_1_BHP_BAR__": f"{report['constraints']['2025']['A-P1']:.6f}",
                    "__WELL_2_BHP_BAR__": f"{report['constraints']['2025']['A-P2']:.6f}",
                    "__INITIAL_PRESSURE_BAR__": "350.000000",
                    "__YEAR_DAYS__": "365",
                },
            )
            flow = module.require_executable("flow")
            summary = module.require_executable("summary")
            module.run_checked(
                [flow, str(fresh_deck), "--output-dir=fresh-out", "--enable-terminal-output=false", "--output-mode=log"],
                cwd=fresh_dir,
            )
            fresh_rows, _ = module.extract_summary(
                summary, fresh_dir / "fresh-out" / "MODEL_A_FRESH2025.SMSPEC",
                module.summary_vectors(("A-P1", "A-P2")),
            )
            fresh_fopt = fresh_rows[-1]["FOPT"]
            self.assertGreater(fopt[2025], fresh_fopt + 0.5 * fopt[2024])


if __name__ == "__main__":
    unittest.main()
