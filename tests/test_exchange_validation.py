from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONFIG = ROOT / "configs" / "coupling.legacy-gsatprod.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DUMMY = load_module("exchange_dummy", ROOT / "bin" / "eclipse_dummy.py")
DRIVER = load_module("exchange_driver", ROOT / "ert" / "bin" / "scripts" / "run_coupled.py")


class ExchangeValidationTest(unittest.TestCase):
    def test_slave_rejects_every_non_finite_numeric_constraint_field(self) -> None:
        numeric_fields = (
            "network_input_q_liq_sm3d",
            "prescribed_q_liq_sm3d",
            "simulated_q_liq_sm3d",
            "total_network_q_liq_sm3d",
            "p_manifold_bar",
            "p_wh_bar",
            "p_bhp_bar",
        )
        header = ["well", "year", *numeric_fields]
        defaults: dict[str, object] = {
            "network_input_q_liq_sm3d": 100.0,
            "prescribed_q_liq_sm3d": 220.0,
            "simulated_q_liq_sm3d": 800.0,
            "total_network_q_liq_sm3d": 1020.0,
            "p_manifold_bar": 40.0,
            "p_wh_bar": 45.0,
            "p_bhp_bar": 200.0,
        }

        for invalid_field in numeric_fields:
            with self.subTest(field=invalid_field), tempfile.TemporaryDirectory() as temp_dir:
                runpath = Path(temp_dir)
                model_dir = runpath / "model_a"
                model_dir.mkdir()
                coupling_dir = runpath / "coupling"
                (coupling_dir / "exchange").mkdir(parents=True)

                spec = json.loads(
                    (ROOT / "input" / "model_a" / "simspec.json").read_text(encoding="utf-8")
                )
                coupling = json.loads((LEGACY_CONFIG).read_text(encoding="utf-8"))
                rows: list[list[object]] = []
                for well in spec["wells"]:
                    for year in (2024, 2025, 2026):
                        values = defaults.copy()
                        if not rows:
                            values[invalid_field] = "NaN"
                        rows.append([well["name"], year, *(values[field] for field in numeric_fields)])
                with (coupling_dir / "network_constraints_model_a.csv").open(
                    "w", newline="", encoding="utf-8"
                ) as handle:
                    writer = csv.writer(handle)
                    writer.writerow(header)
                    writer.writerows(rows)

                with self.assertRaisesRegex(ValueError, invalid_field):
                    DUMMY.run_slave(spec, model_dir, coupling, iteration=1)

    def test_initial_rate_generation_rejects_gas_rate_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runpath = Path(temp_dir)
            model_dir = runpath / "model_a"
            model_dir.mkdir()
            (runpath / "coupling").mkdir()

            spec = json.loads(
                (ROOT / "input" / "model_a" / "simspec.json").read_text(encoding="utf-8")
            )
            for well in spec["wells"]:
                well["gor_sm3_sm3"] = 1.0e308
            (model_dir / "simspec.json").write_text(json.dumps(spec), encoding="utf-8")

            coupling = json.loads((LEGACY_CONFIG).read_text(encoding="utf-8"))
            for well in spec["wells"]:
                coupling["initial_slave_rates_sm3d"][well["name"]] = 1.0e308

            with self.assertRaisesRegex(ValueError, "initial gas rate"):
                DRIVER.initial_rate_rows(runpath, coupling, "model_a", [2024])


if __name__ == "__main__":
    unittest.main()
