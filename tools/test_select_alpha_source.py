#!/usr/bin/env python3

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import select_alpha_source as router


class AlphaSourceRouterTest(unittest.TestCase):
    def write(self, path: pathlib.Path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def market(self, passed: bool):
        return {
            "schema_version": "market_alpha_development_verification_v1",
            "status": "PASS" if passed else "FAIL",
            "fully_verifiable": passed,
            "economic_screen": {"development_passed": passed},
            "promotion_evidence": False,
            "promotion_eligible": False,
        }

    def micro(self, status="NOT_READY"):
        ready = status == "PASS"
        return {
            "schema_version": "microstructure_alpha_lifecycle_v1",
            "status": status,
            "fully_verifiable": ready,
            "phase": "demo_ready" if ready else "selection_collecting",
            "candidate_id": "a" * 64 if ready else None,
            "promotion_eligible": False,
            "demo_entry_eligible": ready,
            "live_promotion_eligible": False,
        }

    def test_routes_are_or_not_and(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            market, micro = root / "market.json", root / "micro.json"
            self.write(market, self.market(True))
            self.write(micro, self.micro("NOT_READY"))
            payload = router.select(market, micro)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["selected_route"], "legacy_integrator")

            self.write(market, self.market(False))
            self.write(micro, self.micro("PASS"))
            payload = router.select(market, micro)
            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["selected_route"], "microstructure_demo")

    def test_fixed_micro_precedence_does_not_compare_cross_source_returns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            market, micro = root / "market.json", root / "micro.json"
            self.write(market, self.market(True))
            self.write(micro, self.micro("PASS"))
            payload = router.select(market, micro)
            self.assertEqual(payload["selected_route"], "microstructure_demo")
            self.assertFalse(
                payload["selection_policy"]["cross_source_return_comparison_permitted"]
            )

    def test_no_ready_source_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            market, micro = root / "market.json", root / "micro.json"
            self.write(market, self.market(False))
            self.write(micro, self.micro("NOT_READY"))
            payload = router.select(market, micro)
            self.assertEqual(payload["status"], "NOT_READY")
            self.assertIsNone(payload["selected_route"])

    def test_wrong_market_schema_cannot_be_routed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            market, micro = root / "market.json", root / "micro.json"
            malformed = self.market(True)
            malformed["schema_version"] = "untrusted"
            self.write(market, malformed)
            self.write(micro, self.micro("NOT_READY"))

            payload = router.select(market, micro)

            self.assertNotEqual(payload["status"], "PASS")
            self.assertIsNone(payload["selected_route"])


if __name__ == "__main__":
    unittest.main()
