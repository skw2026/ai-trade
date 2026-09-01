#!/usr/bin/env python3

import json
import lzma
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import audit_option_vrp_sequential_payoff as audit
import capture_bybit_option_vrp_v2 as capture


ROOT = pathlib.Path(__file__).resolve().parents[1]
LEGACY_POLICY_PATH = ROOT / "config" / "option_variance_risk_premium_sequential_payoff.json"
LEGACY_MANIFEST_PATH = ROOT / "config" / "option_variance_risk_premium_sequential_payoff_manifest.json"
POLICY_PATH = ROOT / "config" / "option_variance_risk_premium_sequential_payoff_v2.json"
MANIFEST_PATH = ROOT / "config" / "option_variance_risk_premium_sequential_payoff_manifest_v2.json"


class OptionVrpSequentialPayoffTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy, cls.manifest = audit.load_frozen_contract(POLICY_PATH, MANIFEST_PATH)

    def option_rows(self, expiry, index=100000.0, strike=100000.0):
        rows = []
        for side, delta in (("Call", 0.55), ("Put", -0.45)):
            rows.append({
                "symbol": f"BTC-30SEP26-{int(strike)}-{side[0]}-USDT",
                "deliveryTime": expiry, "strike": strike, "optionsType": side,
                "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
                "minOrderQty": "0.01", "qtyStep": "0.01", "deliveryFeeRate": "0.00015",
                "tickSize": "5", "bid1Price": "950", "ask1Price": "1000",
                "bid1Size": "2", "ask1Size": "2", "indexPrice": str(index), "delta": str(delta),
            })
        return rows

    def snapshot(self, timestamp, expiry, *, include_options=True, delivery=False):
        options = self.option_rows(expiry) if include_options else []
        delivery_rows = []
        if delivery:
            delivery_rows = [{
                "symbol": row["symbol"], "deliveryPrice": "101000", "deliveryTime": expiry,
                "baseCoin": "BTC", "quoteCoin": "USDT", "settleCoin": "USDT",
                "scopeIdentitySha256": capture.SCOPE_IDENTITY_SHA256, "deliveryPriceNumeric": 101000.0,
            } for row in self.option_rows(expiry)]
        return {
            "schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
            "timestamp_epoch_ms": timestamp,
            "scope_contract": capture.SCOPE_CONTRACT,
            "scope_identity_sha256": capture.SCOPE_IDENTITY_SHA256,
            "delivery_query_status": "PASS",
            "selection_contract": {
                "minimum_dte_days": 0.5, "maximum_dte_days": 10.0,
                "maximum_absolute_moneyness": 0.1,
                "scope_identity_sha256": capture.SCOPE_IDENTITY_SHA256, "settle_coin": "USDT",
            },
            "scoped_options": options, "delivery_prices": delivery_rows,
            "hedge_ticker": {"bid1Price": "99990", "ask1Price": "100010"},
            "hedge_orderbook_l1": {"b": [["99990", "1"]], "a": [["100010", "1"]], "ts": timestamp},
        }

    def write_segment(self, root, snapshots, name="segment"):
        raw = root / "raw" / "BTC" / f"{name}.jsonl.xz"
        features = root / "features" / "BTC" / f"{name}.csv"
        report = root / "reports" / "BTC" / f"{name}.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        features.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        with lzma.open(raw, "wt", encoding="utf-8", preset=1) as handle:
            for snapshot in snapshots:
                handle.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
        features.write_text("timestamp_epoch_ms\n" + "\n".join(str(row["timestamp_epoch_ms"]) for row in snapshots) + "\n", encoding="utf-8")
        payload = {
            "schema_version": capture.SCHEMA_VERSION, "snapshot_schema_version": capture.SNAPSHOT_SCHEMA_VERSION,
            "scope_identity_sha256": capture.SCOPE_IDENTITY_SHA256,
            "capture_root_name": capture.CAPTURE_ROOT_NAME, "status": "PASS", "settle_coin": "USDT",
            "raw_codec": capture.RAW_CODEC,
            "coverage": {
                "capture_started_epoch_ms": snapshots[0]["timestamp_epoch_ms"],
                "capture_completed_epoch_ms": snapshots[-1]["timestamp_epoch_ms"],
                "successful_poll_count": len(snapshots),
            },
            "raw": {"path": raw.relative_to(root).as_posix(), "sha256": audit.sha256_file(raw), "snapshot_count": len(snapshots)},
            "features": {"path": features.relative_to(root).as_posix(), "sha256": audit.sha256_file(features), "row_count": len(snapshots)},
            "quality": {"delivery_query_status": "PASS"},
        }
        report.write_text(json.dumps(payload), encoding="utf-8")
        return raw, report

    def test_frozen_contract_identity_and_permissions(self):
        self.assertEqual(capture.canonical_sha256(self.policy), audit.FROZEN_POLICY_IDENTITY_SHA256_V2)
        self.assertEqual(capture.canonical_sha256(self.manifest), audit.FROZEN_MANIFEST_IDENTITY_SHA256_V2)
        self.assertFalse(any(self.policy["authorities"].values()))
        legacy_policy, legacy_manifest = audit.load_frozen_contract(
            LEGACY_POLICY_PATH, LEGACY_MANIFEST_PATH
        )
        self.assertEqual(
            capture.canonical_sha256(legacy_policy), audit.FROZEN_POLICY_IDENTITY_SHA256
        )
        self.assertEqual(
            capture.canonical_sha256(legacy_manifest), audit.FROZEN_MANIFEST_IDENTITY_SHA256
        )
        mutated = json.loads(json.dumps(self.policy))
        mutated["actions"][1]["quantity_btc_per_leg"] = 0.02
        self.assertNotEqual(capture.canonical_sha256(mutated), audit.FROZEN_POLICY_IDENTITY_SHA256_V2)

    def test_active_entry_calendar_can_reach_first_review(self):
        action = self.policy["actions"][1]
        entry = self.policy["entry_contract"]
        first_review = self.policy["sequential_reviews"][0]
        self.assertEqual(action["target_entry_dte_days"], 1.0)
        self.assertEqual(entry["boundary_entry_dte_days"], [0.75, 1.25])
        self.assertLessEqual(
            max(entry["boundary_entry_dte_days"])
            + first_review["minimum_completed_expiries"]
            * entry["expected_expiry_cluster_cadence_days"],
            first_review["day"],
        )
        self.assertEqual(
            self.policy["cost_contract"]["daily_option_delivery_fee_treatment"],
            "conservative_standard_rate_not_exchange_exemption",
        )

    def test_option_payoff_fee_cap_and_delivery_fee(self):
        call = audit.option_leg_economics(
            position_sign=-1, quantity=0.01, bid=950, ask=1000, index_price=100000,
            strike=100000, delivery_price=101000, option_type="call", tick_size=5,
            option_fee_rate=0.0003, fee_cap_fraction=0.125, delivery_fee_rate=0.00015,
            delivery_fee_cap_fraction=0.125,
            stress_slippage_ticks=1,
        )
        self.assertAlmostEqual(call["intrinsic_per_btc"], 1000)
        self.assertAlmostEqual(call["option_fee"], 0.3)
        self.assertAlmostEqual(call["delivery_fee"], 0.1515)
        put = audit.option_leg_economics(
            position_sign=1, quantity=0.01, bid=1, ask=2, index_price=100000,
            strike=100000, delivery_price=101000, option_type="put", tick_size=1,
            option_fee_rate=0.0003, fee_cap_fraction=0.125, delivery_fee_rate=0.00015,
            delivery_fee_cap_fraction=0.125,
            stress_slippage_ticks=1,
        )
        self.assertAlmostEqual(put["option_fee"], 0.0025)
        self.assertEqual(put["delivery_fee"], 0.0)
        capped = audit.option_leg_economics(
            position_sign=1, quantity=0.01, bid=1, ask=2, index_price=100000,
            strike=100000, delivery_price=100001, option_type="call", tick_size=1,
            option_fee_rate=0.0003, fee_cap_fraction=0.125, delivery_fee_rate=0.00015,
            delivery_fee_cap_fraction=0.125, stress_slippage_ticks=1,
        )
        self.assertAlmostEqual(capped["delivery_fee"], 0.00125)

    def test_hedge_delta_flip_closes_residual_and_preserves_identity(self):
        result = audit.hedge_ledger(
            targets=[
                {"timestamp_epoch_ms": 1, "target": 0.004, "bid": 99990, "ask": 100010},
                {"timestamp_epoch_ms": 2, "target": -0.003, "bid": 100090, "ask": 100110},
            ],
            final_quote={"timestamp_epoch_ms": 3, "bid": 100190, "ask": 100210},
            fee_rate=0.00055, quantity_step=0.001, minimum_trade_quantity=0.001,
            stress_slippage_bps=1.0,
        )
        self.assertEqual(len(result["ledger"]), 3)
        self.assertAlmostEqual(result["residual_quantity_btc"], 0.0)
        self.assertAlmostEqual(result["base_net"], result["gross_pnl"] - result["spread_cost"] - result["hedge_fee"])

    def test_checksum_replay_rejects_tamper_and_conflicting_duplicate(self):
        start = int(self.manifest["observation_start_epoch_ms"]) + 1000
        expiry = start + 8 * 86400000
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / capture.CAPTURE_ROOT_NAME
            raw, _ = self.write_segment(root, [self.snapshot(start, expiry), self.snapshot(start + 60000, expiry)])
            replay = audit.replay_capture_root(root, policy=self.policy, manifest=self.manifest)
            self.assertEqual(replay["eligible_snapshot_count"], 2)
            with raw.open("ab") as handle:
                handle.write(b"x")
            invalid = audit.replay_capture_root(root, policy=self.policy, manifest=self.manifest)
            self.assertEqual(invalid["invalid_segment_count"], 1)

    def test_episode_uses_causal_crossing_delivery_and_final_hedge(self):
        start = int(self.manifest["observation_start_epoch_ms"]) + 1000
        expiry = start + 2 * 86400000
        entry = expiry - 86400000
        snapshots = [
            self.snapshot(entry - 60000, expiry),
            self.snapshot(entry, expiry),
            self.snapshot(expiry - 60000, expiry, include_options=False, delivery=True),
        ]
        delivery = {
            f"{row['symbol']}|{expiry}|USDT": 101000.0 for row in self.option_rows(expiry)
        }
        episode = audit.build_episode(
            snapshots=snapshots, delivery_evidence=delivery, policy=self.policy,
            action=self.policy["actions"][1], delivery_time=expiry,
        )
        self.assertEqual(episode["state"], "complete")
        self.assertEqual(episode["strike"], 100000)
        self.assertAlmostEqual(episode["identity_residual_usdt"], 0.0)
        self.assertAlmostEqual(episode["residual_hedge_quantity_btc"], 0.0)
        pending = audit.build_episode(
            snapshots=snapshots[:-1], delivery_evidence={}, policy=self.policy,
            action=self.policy["actions"][1], delivery_time=expiry,
        )
        self.assertEqual(pending["state"], "pending_delivery")

    def test_episode_distinguishes_future_window_from_missed_crossing(self):
        start = int(self.manifest["observation_start_epoch_ms"]) + 1000
        expiry = start + 2 * 86400000
        awaiting = audit.build_episode(
            snapshots=[self.snapshot(start, expiry)], delivery_evidence={}, policy=self.policy,
            action=self.policy["actions"][1], delivery_time=expiry,
        )
        self.assertEqual(awaiting["state"], "awaiting_entry_window")
        crossing = expiry - 86400000
        missed = audit.build_episode(
            snapshots=[
                self.snapshot(crossing - 300000, expiry),
                self.snapshot(crossing, expiry),
            ],
            delivery_evidence={}, policy=self.policy,
            action=self.policy["actions"][1], delivery_time=expiry,
        )
        self.assertEqual(missed["state"], "missed_entry")

    def test_sequential_gate_never_passes_early_and_can_stop_early(self):
        base_capture = {"invalid_segment_count": 0, "checksum_bound_seconds": 691200, "successful_poll_count": 1000}
        positive = {
            "completed_expiry_count": 6, "stress_ucb_bps": 10, "stress_lcb_bps": 1,
            "gross_mean_bps": 20, "positive_expiry_ratio": 1.0, "worst_expiry_bps": 1,
        }
        day8 = int(self.manifest["observation_start_epoch_ms"]) + 8 * 86400000
        decision = audit.sequential_decision(
            now_epoch_ms=day8, manifest=self.manifest, capture_summary=base_capture,
            primary_summary=positive, boundary_summaries={"a": positive}, policy=self.policy,
        )
        self.assertEqual(decision["decision"], self.policy["decision_contract"]["continue_decision"])
        negative = dict(positive, stress_ucb_bps=0.0)
        stopped = audit.sequential_decision(
            now_epoch_ms=day8, manifest=self.manifest, capture_summary=base_capture,
            primary_summary=negative, boundary_summaries={"a": negative}, policy=self.policy,
        )
        self.assertEqual(stopped["reason_code"], "GROSS_EDGE_ABSENT")

    def test_day35_pass_requires_expiry_tail_and_boundaries(self):
        capture_summary = {"invalid_segment_count": 0, "checksum_bound_seconds": 3024000, "successful_poll_count": 4500}
        passed = {
            "completed_expiry_count": 22, "stress_ucb_bps": 12, "stress_lcb_bps": 2,
            "gross_mean_bps": 20, "positive_expiry_ratio": 0.7, "worst_expiry_bps": -100,
        }
        now = int(self.manifest["observation_start_epoch_ms"]) + 35 * 86400000
        result = audit.sequential_decision(
            now_epoch_ms=now, manifest=self.manifest, capture_summary=capture_summary,
            primary_summary=passed, boundary_summaries={"0.75": passed, "1.25": passed}, policy=self.policy,
        )
        self.assertEqual(result["decision"], self.policy["decision_contract"]["final_pass_decision"])
        unstable = dict(passed, worst_expiry_bps=-151)
        result = audit.sequential_decision(
            now_epoch_ms=now, manifest=self.manifest, capture_summary=capture_summary,
            primary_summary=unstable, boundary_summaries={"0.75": passed, "1.25": passed}, policy=self.policy,
        )
        self.assertEqual(result["reason_code"], "TAIL_UNSTABLE")


if __name__ == "__main__":
    unittest.main()
