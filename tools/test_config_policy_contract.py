import tempfile
import unittest
from pathlib import Path

from tools.config_policy_contract import extract_policy, policy_sha256


BASE = """
system:
  mode: paper
  data_path: ./data/live
  primary_symbol: SOLUSDT
  reconcile:
    enabled: true
exchange:
  platform: bybit
  bybit:
    demo_trading: true
    category: linear
risk:
  max_abs_notional_usd: 3000
execution:
  entry_fee_bps: 5.50
  protection:
    trailing_enabled: true
strategy:
  signal_deadband_bps: 10
integrator:
  enabled: true
  mode: canary
  shadow:
    model_path: old.cbm
    model_report_path: old.json
    candidate_validation_mode: false
    score_gain: 1.0
self_evolution:
  enabled: true
regime:
  enabled: true
universe:
  candidate_symbols: [BTCUSDT, SOLUSDT]
"""


class ConfigPolicyContractTests(unittest.TestCase):
    def test_model_identity_and_numeric_format_do_not_change_policy(self):
        changed = (
            BASE.replace("5.50", "5.5")
            .replace("old.cbm", "new.cbm")
            .replace("old.json", "new.json")
            .replace("candidate_validation_mode: false",
                     "candidate_validation_mode: true")
        )
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.yaml"
            right = Path(tmp) / "right.yaml"
            left.write_text(BASE, encoding="utf-8")
            right.write_text(changed, encoding="utf-8")
            self.assertEqual(policy_sha256(left), policy_sha256(right))

    def test_state_path_does_not_change_execution_policy(self):
        changed = BASE.replace("./data/live", "./data/replay/segment-01")
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.yaml"
            right = Path(tmp) / "right.yaml"
            left.write_text(BASE, encoding="utf-8")
            right.write_text(changed, encoding="utf-8")
            self.assertEqual(policy_sha256(left), policy_sha256(right))
            self.assertNotIn("system.data_path", extract_policy(BASE))

    def test_economic_or_exit_change_changes_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.yaml"
            right = Path(tmp) / "right.yaml"
            left.write_text(BASE, encoding="utf-8")
            right.write_text(
                BASE.replace("entry_fee_bps: 5.50", "entry_fee_bps: 2.75"),
                encoding="utf-8",
            )
            self.assertNotEqual(policy_sha256(left), policy_sha256(right))

    def test_contract_contains_nested_protection_and_integrator_policy(self):
        policy = extract_policy(BASE)
        self.assertTrue(policy["execution.protection.trailing_enabled"])
        self.assertEqual(policy["integrator.mode"], "canary")
        self.assertEqual(policy["system.primary_symbol"], "SOLUSDT")
        self.assertTrue(policy["system.reconcile.enabled"])
        self.assertTrue(policy["exchange.bybit.demo_trading"])
        self.assertNotIn("integrator.shadow.model_path", policy)

    def test_runtime_or_exchange_change_changes_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.yaml"
            right = Path(tmp) / "right.yaml"
            left.write_text(BASE, encoding="utf-8")
            right.write_text(
                BASE.replace("primary_symbol: SOLUSDT", "primary_symbol: BTCUSDT"),
                encoding="utf-8",
            )
            self.assertNotEqual(policy_sha256(left), policy_sha256(right))


if __name__ == "__main__":
    unittest.main()
