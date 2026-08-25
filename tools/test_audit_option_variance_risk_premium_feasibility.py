#!/usr/bin/env python3

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import audit_option_variance_risk_premium_feasibility as audit


class OptionVrpFeasibilityAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = pathlib.Path(__file__).resolve().parents[1] / "config" / "option_variance_risk_premium_feasibility.json"
        cls.policy = json.loads(cls.config_path.read_text(encoding="utf-8"))

    def live(self):
        return {
            "active_contract_count": 800, "two_sided_contract_count": 700,
            "scoped_two_sided_contract_count": 180, "scoped_volume_contract_count": 120,
            "scoped_spread_ratio_p90": 0.08, "recent_trade_count": 1000,
            "atm_mark_iv_median": 0.45, "historical_volatility_30d": 0.37,
        }

    def capture(self, *, ready=False):
        gate = self.policy["forward_capture_gate"]
        return {
            "checksum_bound_seconds": gate["minimum_checksum_bound_seconds"] if ready else 0,
            "completed_expiries_with_delivery": gate["minimum_completed_expiries_with_delivery"] if ready else 0,
            "successful_poll_count": gate["minimum_successful_polls"] if ready else 0,
            "invalid_segment_count": 0,
        }

    def test_waits_without_forward_payoff_evidence(self):
        report = audit.build_report(policy=self.policy, policy_path=self.config_path, live=self.live(), capture_audit=self.capture())
        self.assertEqual(report["decision"], "WAIT_FOR_OPTION_VRP_FORWARD_CAPTURE")
        self.assertFalse(report["economics"]["profitability_verified"])
        self.assertFalse(report["demo_activation_authorized"])

    def test_ready_only_after_all_capture_gates(self):
        report = audit.build_report(policy=self.policy, policy_path=self.config_path, live=self.live(), capture_audit=self.capture(ready=True))
        self.assertEqual(report["decision"], "READY_FOR_FROZEN_OPTION_PAYOFF_AUDIT")
        self.assertFalse(report["promotion_eligible"])

    def test_stops_when_live_market_gate_fails(self):
        live = self.live()
        live["scoped_two_sided_contract_count"] = 0
        report = audit.build_report(policy=self.policy, policy_path=self.config_path, live=live, capture_audit=self.capture(ready=True))
        self.assertEqual(report["decision"], "STOP_OPTION_VRP_MARKET_FEASIBILITY")


if __name__ == "__main__":
    unittest.main()
