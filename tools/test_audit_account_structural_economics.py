#!/usr/bin/env python3

import argparse
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import audit_account_structural_economics as audit


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "account_structural_economics_audit.json"


def upstream_payload() -> dict:
    return {
        "schema_version": "cross_venue_funding_differential_experiment_v1",
        "status": "COMPLETE",
        "fully_verifiable": True,
        "research_domain": "historical_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "research_decision": "STOP_CROSS_VENUE_FUNDING_DIFFERENTIAL_FAMILY",
        "reason_codes": ["historical_cross_venue_funding_upper_bound_failed"],
        "execution_contract": {
            "execution": {
                "bybit_taker_fee_bps_per_fill": 5.5,
                "binance_taker_fee_bps_per_fill": 5.5,
                "bybit_slippage_bps_per_fill": 1.0,
                "binance_slippage_bps_per_fill": 1.0,
                "intervenue_leg_risk_bps_per_round_trip": 2.0,
                "stress_execution_cost_multiplier": 1.25,
            },
            "historical_price_is_executable_bbo": False,
            "historical_proxy_can_authorize_demo": False,
        },
        "hindsight_oracle": {
            "maximum_candidate": {
                "direction": "long_binance_short_bybit",
                "horizon_hours": 24,
                "gross_bps": 9.144200387088157,
                "base_bps": -21.501441983665078,
                "stress_bps": -29.8477840832027,
                "basis_bps": 5.468253376110704,
                "funding_bps": 3.675947010977453,
                "execution_cost_bps": 27.905916343355972,
            }
        },
    }


class AccountStructuralEconomicsAuditTest(unittest.TestCase):
    def write_upstream(self, root: pathlib.Path, payload: dict | None = None) -> pathlib.Path:
        path = root / "upstream.json"
        path.write_text(json.dumps(payload or upstream_payload()), encoding="utf-8")
        return path

    def args(self, root: pathlib.Path, *, private_mode: str = "skip") -> argparse.Namespace:
        return argparse.Namespace(
            upstream_report=str(root / "upstream.json"),
            config=str(POLICY),
            output=str(root / "report.json"),
            timeout_sec=1.0,
            private_mode=private_mode,
        )

    def test_policy_is_frozen_read_only_and_has_no_activation_authority(self):
        policy = audit.validate_policy(POLICY)
        self.assertEqual(
            audit.canonical_sha256(policy), audit.FROZEN_POLICY_IDENTITY_SHA256
        )
        private = policy["private_account_contract"]
        self.assertTrue(private["read_only_requests_only"])
        self.assertFalse(private["record_api_key"])
        self.assertFalse(private["record_account_uid"])
        self.assertFalse(private["record_exact_balance"])
        self.assertEqual(
            policy["authorities"],
            {
                "promotion_authority": False,
                "demo_activation_authorized": False,
                "live_activation_authorized": False,
            },
        )

    def test_zero_fee_stress_bound_stops_fee_tier_rescue_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.write_upstream(root)
            report = audit.run(self.args(root))
        bound = report["zero_fee_upper_bound"]
        self.assertEqual(report["status"], "COMPLETE")
        self.assertTrue(report["fully_verifiable_zero_fee_upper_bound"])
        self.assertEqual(report["account_cost_verification_status"], "UNAVAILABLE")
        self.assertEqual(
            report["structural_decision"],
            "STOP_ACCOUNT_FEE_TIER_RESCUE_FOR_CROSS_VENUE_FUNDING",
        )
        self.assertAlmostEqual(bound["zero_fee_non_fee_execution_cost_bps"], 5.9855, places=3)
        self.assertAlmostEqual(bound["zero_fee_stress_net_bps"], -2.4473, places=3)
        self.assertFalse(bound["passes"])
        self.assertFalse(report["promotion_authority"])
        self.assertFalse(report["demo_activation_authorized"])
        self.assertFalse(report["live_activation_authorized"])

    def test_bybit_private_query_records_rates_and_boolean_capital_only(self):
        policy = audit.validate_policy(POLICY)
        responses = [
            (
                {
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "SOLUSDT",
                                "makerFeeRate": "0.0001",
                                "takerFeeRate": "0.00055",
                            }
                        ]
                    },
                },
                None,
            ),
            (
                {
                    "retCode": 0,
                    "result": {"list": [{"totalAvailableBalance": "999.25"}]},
                },
                None,
            ),
        ]
        clean_env = {
            "AI_TRADE_BYBIT_DEMO_API_KEY": "private-key-marker",
            "AI_TRADE_BYBIT_DEMO_API_SECRET": "private-secret-marker",
            "AI_TRADE_API_KEY": "",
            "AI_TRADE_API_SECRET": "",
        }
        with mock.patch.dict(os.environ, clean_env, clear=False), mock.patch.object(
            audit, "_request_json", side_effect=responses
        ):
            result = audit.query_bybit_account(policy, timeout_sec=1.0)
        encoded = json.dumps(result, sort_keys=True)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertAlmostEqual(result["taker_fee_bps"], 5.5)
        self.assertTrue(result["capital_sufficient_for_frozen_reference"])
        self.assertFalse(result["exact_balance_recorded"])
        self.assertNotIn("999.25", encoded)
        self.assertNotIn("private-key-marker", encoded)
        self.assertNotIn("private-secret-marker", encoded)

    def test_binance_private_query_records_rates_and_boolean_capital_only(self):
        policy = audit.validate_policy(POLICY)
        responses = [
            ({"serverTime": 1_777_000_000_000}, None),
            (
                {
                    "symbol": "SOLUSDT",
                    "makerCommissionRate": "0.0002",
                    "takerCommissionRate": "0.0005",
                    "rpiCommissionRate": "0.0001",
                },
                None,
            ),
            ({"availableBalance": "800.125"}, None),
        ]
        clean_env = {
            "AI_TRADE_BINANCE_DEMO_API_KEY": "private-binance-key",
            "AI_TRADE_BINANCE_DEMO_API_SECRET": "private-binance-secret",
        }
        with mock.patch.dict(os.environ, clean_env, clear=False), mock.patch.object(
            audit, "_request_json", side_effect=responses
        ):
            result = audit.query_binance_account(policy, timeout_sec=1.0)
        encoded = json.dumps(result, sort_keys=True)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertAlmostEqual(result["taker_fee_bps"], 5.0)
        self.assertTrue(result["capital_sufficient_for_frozen_reference"])
        self.assertFalse(result["exact_balance_recorded"])
        self.assertNotIn("800.125", encoded)
        self.assertNotIn("private-binance-key", encoded)
        self.assertNotIn("private-binance-secret", encoded)

    def test_upstream_cost_drift_is_rejected_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            changed = upstream_payload()
            changed["execution_contract"]["execution"][
                "bybit_taker_fee_bps_per_fill"
            ] = 4.0
            path = self.write_upstream(root, changed)
            policy = audit.validate_policy(POLICY)
            with self.assertRaisesRegex(ValueError, "execution contract drift"):
                audit.validate_upstream(path, policy)


if __name__ == "__main__":
    unittest.main()
