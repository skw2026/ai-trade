#!/usr/bin/env python3

import argparse
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import microstructure_demo_policy as policy
import run_microstructure_alpha_development as development


class FakeModel:
    def __init__(self, prediction):
        self.prediction = np.asarray(prediction, dtype=np.float64)

    def predict(self, features):
        return np.tile(self.prediction, (len(features), 1))


def feature_row(timestamp: int, offset: float = 0.0):
    mid = 100.0 + offset
    return {
        "timestamp": timestamp,
        "best_bid": mid - 0.01,
        "best_ask": mid + 0.01,
        "mid": mid,
        "spread_bps": 2.0,
        "microprice": mid + 0.001,
        "book_imbalance_l1": 0.1,
        "book_imbalance_l5": 0.05,
        "book_imbalance_l20": 0.02,
        "depth_slope": 1.0,
        "book_update_count": 5,
        "trade_count": 2,
        "buy_quote_volume": 100.0,
        "sell_quote_volume": 80.0,
        "trade_imbalance": 1.0 / 9.0,
    }


class DemoPolicyTest(unittest.TestCase):
    def candidate(self, prediction) -> policy.CandidateBundle:
        rows = [feature_row(index * 1000, index * 0.001) for index in range(61)]
        _, names = development.build_causal_features(policy.series_from_rows(rows))
        actions = [
            {"direction": "long", "horizon_seconds": 15},
            {"direction": "short", "horizon_seconds": 15},
        ]
        target_transform = {
            "method": "fit_only_standardized_stress_profitability_v1",
            "profitability_hurdle": "base_net_return_bps_gt_stress_incremental_cost_bps",
            "inference_reconstruction": "clipped_probability_times_fit_class_conditional_base_net_means",
            "validation_or_test_statistics_used": False,
            "stress_incremental_cost_bps": 1.0,
            "action_statistics": [
                {
                    "action_index": index,
                    "row_count": 100,
                    "positive_count": 50,
                    "nonpositive_count": 50,
                    "positive_rate": 0.5,
                    "standardization_scale": 0.5,
                    "positive_mean_base_net_bps": 10.0,
                    "nonpositive_mean_base_net_bps": -10.0,
                    "learnable": True,
                }
                for index in range(len(actions))
            ],
        }
        return policy.CandidateBundle(
            candidate_id="a" * 64,
            state_sha256="b" * 64,
            model_sha256="c" * 64,
            development_report_sha256="d" * 64,
            feature_names=names,
            actions=actions,
            threshold_bps=2.0,
            execution_latency_seconds=1,
            report={"frozen_candidate": {"target_transform": target_transform}},
            model=FakeModel(prediction),
        )

    def test_policy_uses_frozen_threshold_and_holds_non_overlapping_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "signal.json"
            engine = policy.DemoPolicyEngine(signal_output=output)
            engine.set_candidate(self.candidate([0.35, 0.1]))
            payload = None
            for index in range(62):
                payload = engine.on_row(feature_row(index * 1000, index * 0.001))
            self.assertEqual(payload["status"], "ACTIVE")
            self.assertEqual(payload["action"]["direction"], 1)
            first_until = payload["active_until_exchange_ms"]
            first_started = payload["action"]["started_exchange_ms"]

            engine.candidate.model = FakeModel([0.0, 0.9])
            held = engine.on_row(feature_row(62_000, 0.062))
            self.assertEqual(held["reason"], "frozen_action_holding_window")
            self.assertEqual(held["action"]["direction"], 1)
            self.assertEqual(held["action"]["started_exchange_ms"], first_started)
            self.assertEqual(held["active_until_exchange_ms"], first_until)
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(persisted["live_promotion_eligible"])

    def test_policy_is_flat_when_threshold_not_met_and_fail_closed_on_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "signal.json"
            engine = policy.DemoPolicyEngine(signal_output=output)
            engine.set_candidate(self.candidate([-0.35, -0.4]))
            payload = None
            for index in range(62):
                payload = engine.on_row(feature_row(index * 1000, index * 0.001))
            self.assertEqual(payload["status"], "FLAT")
            self.assertIsNone(payload["action"])
            engine.publish_fail_closed("test_integrity_failure", 61_000)
            failed = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "FAIL_CLOSED")
            self.assertEqual(failed["active_until_exchange_ms"], 0)

    def test_feature_stream_finalizes_only_behind_watermark(self):
        stream = policy.StreamingFeatureRows()
        stream.aggregator.buckets[1000] = policy.collector.FeatureBucket(
            timestamp_ms=1000, metrics={
                "best_bid": 99.0, "best_ask": 101.0, "mid": 100.0,
                "spread_bps": 200.0, "microprice": 100.0,
                "book_imbalance_l1": 0.0, "book_imbalance_l5": 0.0,
                "book_imbalance_l20": 0.0, "depth_slope": 0.0,
            }
        )
        stream.aggregator.buckets[2000] = policy.collector.FeatureBucket(
            timestamp_ms=2000, metrics={
                "best_bid": 99.0, "best_ask": 101.0, "mid": 100.0,
                "spread_bps": 200.0, "microprice": 100.0,
                "book_imbalance_l1": 0.0, "book_imbalance_l5": 0.0,
                "book_imbalance_l20": 0.0, "depth_slope": 0.0,
            }
        )
        with mock.patch.object(stream.aggregator, "process", return_value=True):
            rows = stream.process({})
        self.assertEqual([row["timestamp"] for row in rows], [1000])

    def test_healthcheck_accepts_waiting_candidate_but_rejects_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            health = pathlib.Path(temp_dir) / "health.json"
            health.write_text(
                json.dumps({
                    "state": "waiting_candidate",
                    "last_heartbeat_epoch_ms": int(time.time() * 1000),
                }),
                encoding="utf-8",
            )
            args = argparse.Namespace(health_output=str(health), max_stale_ms=10_000)
            self.assertEqual(policy.healthcheck(args), 0)
            payload = json.loads(health.read_text(encoding="utf-8"))
            payload["last_heartbeat_epoch_ms"] = 1
            health.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(policy.healthcheck(args), 1)


if __name__ == "__main__":
    unittest.main()
