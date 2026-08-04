from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRIVER_PATH = ROOT / "ert" / "bin" / "scripts" / "run_coupled.py"
SPEC = importlib.util.spec_from_file_location("run_coupled", DRIVER_PATH)
assert SPEC is not None and SPEC.loader is not None
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


class RunpathSafetyTest(unittest.TestCase):
    def test_repository_source_trees_are_rejected(self) -> None:
        for path in (ROOT, ROOT / "input", ROOT / "input" / "nested", ROOT / "ert"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "runpath"):
                    DRIVER.validate_runpath(path.resolve())

    def test_temporary_and_repository_output_paths_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            DRIVER.validate_runpath(Path(temp_dir).resolve())
        DRIVER.validate_runpath((ROOT / "output" / "case" / "realization-0").resolve())


if __name__ == "__main__":
    unittest.main()
