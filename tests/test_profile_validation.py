from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DUMMY_PATH = ROOT / "bin" / "eclipse_dummy.py"
SPEC = importlib.util.spec_from_file_location("eclipse_dummy", DUMMY_PATH)
assert SPEC is not None and SPEC.loader is not None
DUMMY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DUMMY)


class PrescribedProfileValidationTest(unittest.TestCase):
    def parse(self, text: str) -> list[dict]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.inc"
            path.write_text(text, encoding="utf-8")
            return DUMMY.parse_gsatprod_inc(path)

    def test_duplicate_external_well_year_is_rejected(self) -> None:
        rows = self.parse(
            """GSATPROD
'EXT-P1' 2024 100 1000 40 280 0.3 /
'EXT-P1' 2024 110 1100 40 280 0.3 /
'EXT-P1' 2025 90 900 40 280 0.3 /
/
"""
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            DUMMY.validate_prescribed_profile_rows(rows, {2024, 2025}, "external_satellite")

    def test_each_external_well_must_cover_every_schedule_year(self) -> None:
        rows = self.parse(
            """GSATPROD
'EXT-P1' 2024 100 1000 40 280 0.3 /
'EXT-P1' 2025 90 900 40 280 0.3 /
'EXT-P2' 2024 80 800 40 275 0.3 /
/
"""
        )
        with self.assertRaisesRegex(ValueError, "incomplete coverage"):
            DUMMY.validate_prescribed_profile_rows(rows, {2024, 2025}, "external_satellite")

    def test_missing_gsatprod_header_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "GSATPROD header"):
            self.parse("'EXT-P1' 2024 100 1000 40 280 0.3 /\n/\n")

    def test_non_finite_external_rate_is_rejected(self) -> None:
        rows = self.parse(
            """GSATPROD
'EXT-P1' 2024 NaN 1000 40 280 0.3 /
'EXT-P1' 2025 90 900 40 280 0.3 /
/
"""
        )
        with self.assertRaisesRegex(ValueError, "finite"):
            DUMMY.validate_prescribed_profile_rows(rows, {2024, 2025}, "external_satellite")


if __name__ == "__main__":
    unittest.main()
