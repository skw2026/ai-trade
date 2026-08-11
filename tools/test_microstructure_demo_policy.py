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
    row = {
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
    for symbol, scale in (("BTCUSDT", 10.0), ("ETHUSDT", 5.0)):
        prefix = policy.collector.context_prefix(symbol)
        context_mid = mid * scale
        row.update(
            {
                f"{prefix}_mid": context_mid,
                f"{prefix}_spread_bps": 1.0,
                f"{prefix}_microprice": context_mid + 0.001,
                f"{prefix}_book_imbalance_l1": 0.08,
                f"{prefix}_book_imbalance_l5": 0.04,
                f"{prefix}_book_imbalance_l20": 0.01,
                f"{prefix}_depth_slope": 1.2,
                f"{prefix}_book_update_count": 4,
                f"{prefix}_trade_count": 2,
                f"{prefix}_buy_quote_volume": 90.0,
                f"{prefix}_sell_quote_volume": 70.0,
                f"{prefix}_trade_imbalance": 0.125,
            }
        )
    return row


def stream_messages():
    previous = {}
    output = []
    for index, timestamp in enumerate((1000, 2000)):
        for symbol, scale in (("SOLUSDT", 1.0), ("BTCUSDT", 10.0), ("ETHUSDT", 5.0)):
            bid = 99.0 * scale + index
            ask = 101.0 * scale + index
            if index == 0:
                book = {
                    "topic": f"orderbook.50.{symbol}",
                    "type": "snapshot",
                    "cts": timestamp,
                    "data": {"u": 1, "seq": 1, "b": [[bid, 1]], "a": [[ask, 1]]},
                }
            else:
                old_bid, old_ask = previous[symbol]
                book = {
                    "topic": f"orderbook.50.{symbol}",
                    "type": "delta",
                    "cts": timestamp,
                    "data": {
                        "u": 2,
                        "seq": 2,
                        "b": [[old_bid, 0], [bid, 1]],
                        "a": [[old_ask, 0], [ask, 1]],
                    },
                }
            trade = {
                "topic": f"publicTrade.{symbol}",
                "data": [
                    {
                        "T": timestamp,
                        "S": "Buy",
                        "v": "1",
                        "p": str((bid + ask) / 2.0),
                        "i": f"{symbol}-{index}",
                    }
                ],
            }
            output.extend((book, trade))
            previous[symbol] = (bid, ask)
    return output


class DemoPolicyTest(unittest.TestCase):
    def candidate(self, prediction) -> policy.CandidateBundle:
        rows = [feature_row(index * 1000, index * 0.001) for index in range(61)]
        _, names = development.build_causal_features(policy.series_from_rows(rows))
        actions = [
            {"direction": "long", "horizon_seconds": 15},
            {"direction": "short", "horizon_seconds": 15},
        ]
        target_transform = {
            "method": "fit_only_winsorized_action_net_return_v3",
            "training_objective": "independent_executable_base_net_return_bps",
            "actions": actions,
            "model_action_indices": [0, 1],
            "model_output_count": 2,
            "target_normalization": "per_action_fit_only_winsorized_zero_mean_unit_variance",
            "inference_reconstruction": "inverse_fit_location_scale_clipped_to_fit_winsor_bounds_bps",
            "validation_or_test_statistics_used": False,
            "stress_incremental_cost_bps": 1.0,
            "winsor_lower_quantile": 0.01,
            "winsor_upper_quantile": 0.99,
            "action_statistics": [
                {
                    "action_index": index,
                    "row_count": 300,
                    "lower_clip_bps": -10.0,
                    "upper_clip_bps": 10.0,
                    "winsorized_location_bps": 0.0,
                    "winsorized_scale_bps": 1.0,
                    "raw_mean_base_net_bps": 0.0,
                    "stress_profitable_count": 100,
                    "stress_profitable_rate": 1.0 / 3.0,
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
            policy_action_index=0,
            threshold_bps=2.0,
            execution_latency_seconds=1,
            report={"frozen_candidate": {"target_transform": target_transform}},
            model=FakeModel(prediction),
        )

    def test_policy_uses_frozen_threshold_and_holds_non_overlapping_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "signal.json"
            engine = policy.DemoPolicyEngine(signal_output=output)
            engine.set_candidate(self.candidate([3.5, 1.0]))
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
            engine.candidate.model = FakeModel([-0.35, 0.9])
            released = engine.on_row(feature_row(78_000, 0.078))
            self.assertEqual(released["status"], "FLAT")
            self.assertIsNone(released["action"])
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
        for aggregator in stream.aggregator.aggregators.values():
            for timestamp in (1000, 2000):
                aggregator.buckets[timestamp] = policy.collector.FeatureBucket(
                    timestamp_ms=timestamp,
                    metrics={
                        "best_bid": 99.0,
                        "best_ask": 101.0,
                        "mid": 100.0,
                        "spread_bps": 200.0,
                        "microprice": 100.0,
                        "book_imbalance_l1": 0.0,
                        "book_imbalance_l5": 0.0,
                        "book_imbalance_l20": 0.0,
                        "depth_slope": 0.0,
                    },
                )
            aggregator.latest_book_bucket = 2000
            aggregator.latest_trade_bucket = 2000
        with mock.patch.object(stream.aggregator, "process", return_value=True):
            rows = stream.process({})
        self.assertEqual([row["timestamp"] for row in rows], [1000])

    def test_streaming_row_matches_offline_cross_asset_replay_semantics(self):
        messages = stream_messages()
        offline = policy.collector.CrossAssetMicrostructureAggregator()
        for message in messages:
            offline.process(message)
        stream = policy.StreamingFeatureRows()
        emitted = []
        for message in messages:
            emitted.extend(stream.process(message))
        self.assertEqual(emitted, offline.rows()[:1])
        self.assertEqual(tuple(emitted[0]), policy.collector.OUTPUT_FIELDS)

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
