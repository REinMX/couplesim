from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spikes" / "002-opm-network-master"
RUNNER = SPIKE / "opm_network_master_spike.py"
FLOW_AVAILABLE = shutil.which("flow") is not None and shutil.which("summary") is not None


def flow_at_least(version: str) -> bool:
    """True when the installed flow is at least the given YYYY.MM release."""
    if not FLOW_AVAILABLE:
        return False
    completed = subprocess.run(
        [shutil.which("flow"), "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return False
    try:
        installed = completed.stdout.strip().split()[1]
    except IndexError:
        return False
    return tuple(int(part) for part in installed.split(".")) >= tuple(
        int(part) for part in version.split(".")
    )


# GSATPROD satellites only load branch VFPs on Flow >= 2026.04 (Spike 002
# re-verification); the integration test below asserts that behaviour.
GSAT_LOADS_TRUNK_AVAILABLE = flow_at_least("2026.04")


def load_module():
    spec = importlib.util.spec_from_file_location("opm_network_master_spike", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OpmNetworkMasterSpikeTest(unittest.TestCase):
    def test_templates_exist_and_have_expected_tokens(self) -> None:
        two_well = (SPIKE / "TWO_WELL_NETWORK.DATA.tmpl").read_text(encoding="utf-8")
        master = (SPIKE / "MASTER_GSATPROD.DATA.tmpl").read_text(encoding="utf-8")
        gcon = (SPIKE / "GCONPROD_GSATPROD.DATA").read_text(encoding="utf-8")
        vfp = (SPIKE / "vfp_tables.inc").read_text(encoding="utf-8")

        self.assertIn("__M_P1_ORAT__", two_well)
        self.assertIn("__M_P2_ORAT__", two_well)
        self.assertIn("BRANPROP", two_well)
        self.assertIn("NODEPROP", two_well)
        self.assertLess(
            two_well.index("BRANPROP"),
            two_well.index("NODEPROP"),
            "BRANPROP must precede NODEPROP",
        )

        self.assertIn("__SAT_OIL_SM3D__", master)
        self.assertIn("__SAT_GAS_SM3D__", master)
        self.assertIn("GSATPROD", master)
        self.assertIn("GAS", master)
        self.assertIn("DISGAS", master)

        self.assertIn("GSATPROD", gcon)
        self.assertIn("GCONPROD", gcon)

        self.assertIn("VFPPROD", vfp)
        self.assertGreaterEqual(vfp.count("VFPPROD"), 2)

    def test_analyse_classifies_partial_verdict(self) -> None:
        module = load_module()
        report = {
            "two_well_network": [
                {"results": {"GPR:MANIFOLD": 60.0, "WOPR:M-P1": 50.0, "WOPR:M-P2": 50.0, "WTHP:M-P1": 280.0}},
                {"results": {"GPR:MANIFOLD": 145.0, "WOPR:M-P1": 200.0, "WOPR:M-P2": 200.0, "WTHP:M-P1": 250.0}},
                {"results": {"GPR:MANIFOLD": 232.0, "WOPR:M-P1": 290.0, "WOPR:M-P2": 290.0, "WTHP:M-P1": 232.0}},
            ],
            "gsatprod_network": [
                {
                    "requested_gsatprod_oil_sm3d": 0.0,
                    "results": {"GOPR:SAT": 0.0, "GPR:MANIFOLD": 86.0, "WOPR:M-P1": 200.0, "WTHP:M-P1": 250.0},
                },
                {
                    "requested_gsatprod_oil_sm3d": 130.0,
                    "results": {"GOPR:SAT": 130.0, "GPR:MANIFOLD": 86.0, "WOPR:M-P1": 200.0, "WTHP:M-P1": 250.0},
                },
                {
                    "requested_gsatprod_oil_sm3d": 600.0,
                    "results": {"GOPR:SAT": 600.0, "GPR:MANIFOLD": 86.0, "WOPR:M-P1": 200.0, "WTHP:M-P1": 250.0},
                },
            ],
            "gconprod_gsatprod": {
                "results": {"WOPR:M-P1": 100.0, "GOPR:SAT": 150.0, "FOPR": 250.0},
            },
        }
        analysis = module.analyse(report)
        self.assertTrue(analysis["two_well_loads_trunk_vfp"])
        self.assertTrue(analysis["gsatprod_registers_in_group_totals"])
        self.assertFalse(analysis["gsatprod_loads_trunk_vfp"])
        self.assertTrue(analysis["gconprod_sees_gsatprod"])
        self.assertEqual(analysis["verdict"], "PARTIAL")

    def test_render_template_rejects_missing_marker(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            template = Path(temp_dir) / "t.tmpl"
            destination = Path(temp_dir) / "out.DATA"
            template.write_text("VALUE __A__\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                module.render_template(template, destination, {"__A__": "1", "__B__": "2"})

    @unittest.skipUnless(FLOW_AVAILABLE, "requires installed OPM Flow and summary CLI")
    @unittest.skipUnless(
        GSAT_LOADS_TRUNK_AVAILABLE,
        "GSATPROD loads branch VFPs only on OPM Flow >= 2026.04",
    )
    def test_full_spike_runner_produces_validated_verdict(self) -> None:
        output_root = ROOT / "output"
        output_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="network-master-", dir=output_root) as temp_dir:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--output-dir",
                    temp_dir,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
            )
            report_path = Path(temp_dir) / "network_master_report.json"
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text(encoding="utf-8"))
            analysis = report["analysis"]
            self.assertTrue(analysis["two_well_loads_trunk_vfp"])
            self.assertTrue(analysis["gsatprod_registers_in_group_totals"])
            self.assertTrue(analysis["gsatprod_loads_trunk_vfp"])
            self.assertTrue(analysis["gconprod_sees_gsatprod"])
            self.assertEqual(analysis["verdict"], "VALIDATED")

            # Quantify the trunk response and the GSATPROD trunk loading.
            two_well = report["two_well_network"]
            low_p = two_well[0]["results"]["GPR:MANIFOLD"]
            high_p = two_well[-1]["results"]["GPR:MANIFOLD"]
            self.assertGreater(high_p - low_p, 50.0)

            gsat_pressures = [
                case["results"]["GPR:MANIFOLD"] for case in report["gsatprod_network"]
            ]
            self.assertGreater(max(gsat_pressures) - min(gsat_pressures), 50.0)

            gcon = report["gconprod_gsatprod"]["results"]
            self.assertTrue(math.isclose(gcon["FOPR"], 250.0, abs_tol=1.0e-2))
            self.assertLess(gcon["WOPR:M-P1"], 150.0)


if __name__ == "__main__":
    unittest.main()
