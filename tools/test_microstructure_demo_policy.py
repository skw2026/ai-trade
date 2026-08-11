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

    def predict_proba(self, features):
        positive = np.tile(self.prediction, len(features))
        return np.column_stack((1.0 - positive, positive))


def feature_row(timestamp: int, offset: float = 0.0):
    mid = 100.0 + offset
    row = {
        "timestamp": timestamp,
        "best_bid": mid - 0.01,
        "best_ask": mid + 0.01,
        "best_bid_size": 10.0,
        "best_ask_size": 8.0,
        "bid_depth_l5": 40.0,
        "ask_depth_l5": 35.0,
        "bid_depth_l20": 120.0,
        "ask_depth_l20": 110.0,
        "mid": mid,
        "spread_bps": 2.0,
        "microprice": mid + 0.001,
        "book_imbalance_l1": 0.1,
        "book_imbalance_l5": 0.05,
        "book_imbalance_l20": 0.02,
        "depth_slope": 1.0,
        "book_update_count": 5,
        "book_flow_imbalance": 0.1,
        "book_flow_quote_volume": 1000.0,
        "book_ofi": 0.05,
        "book_mid_range_bps": 0.5,
        "trade_count": 2,
        "buy_quote_volume": 100.0,
        "sell_quote_volume": 80.0,
        "buy_base_volume": 1.0,
        "sell_base_volume": 0.8,
        "trade_imbalance": 1.0 / 9.0,
        "trade_vwap_dislocation_bps": 0.1,
    }
    for symbol, scale in (("BTCUSDT", 10.0), ("ETHUSDT", 5.0)):
        prefix = policy.collector.context_prefix(symbol)
        context_mid = mid * scale
        row.update(
            {
                f"{prefix}_mid": context_mid,
                f"{prefix}_spread_bps": 1.0,
                f"{prefix}_microprice": context_mid + 0.001,
                f"{prefix}_best_bid_size": 12.0,
                f"{prefix}_best_ask_size": 11.0,
                f"{prefix}_bid_depth_l5": 50.0,
                f"{prefix}_ask_depth_l5": 45.0,
                f"{prefix}_bid_depth_l20": 150.0,
                f"{prefix}_ask_depth_l20": 140.0,
                f"{prefix}_book_imbalance_l1": 0.08,
                f"{prefix}_book_imbalance_l5": 0.04,
                f"{prefix}_book_imbalance_l20": 0.01,
                f"{prefix}_depth_slope": 1.2,
                f"{prefix}_book_update_count": 4,
                f"{prefix}_book_flow_imbalance": 0.08,
                f"{prefix}_book_flow_quote_volume": 2000.0,
                f"{prefix}_book_ofi": 0.04,
                f"{prefix}_book_mid_range_bps": 0.4,
                f"{prefix}_trade_count": 2,
                f"{prefix}_buy_quote_volume": 90.0,
                f"{prefix}_sell_quote_volume": 70.0,
                f"{prefix}_buy_base_volume": 0.9,
                f"{prefix}_sell_base_volume": 0.7,
                f"{prefix}_trade_imbalance": 0.125,
                f"{prefix}_trade_vwap_dislocation_bps": 0.05,
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
        rows = [
            feature_row(index * 1000, index * 0.001)
            for index in range(development.MIN_CAUSAL_FEATURE_HISTORY_ROWS)
        ]
        _, names = development.build_causal_features(policy.series_from_rows(rows))
        actions = [
            {"direction": "long", "horizon_seconds": 15},
            {"direction": "short", "horizon_seconds": 15},
        ]
        target_transform = {
            "method": "fit_only_stress_profitability_event_v5",
            "training_objective": "independent_stress_cost_profitable_event",
            "actions": actions,
            "available_action_indices": [0, 1],
            "model_action_indices": [0],
            "model_output_count": 1,
            "event_definition": "executable_base_net_return_bps_gt_stress_incremental_cost_bps",
            "minimum_profitable_events_per_action": 16,
            "minimum_unprofitable_events_per_action": 16,
            "target_encoding": "binary_zero_one",
            "inference_reconstruction": "fit_only_event_conditional_expected_base_net_bps",
            "validation_or_test_statistics_used": False,
            "stress_incremental_cost_bps": 1.0,
            "action_statistics": [
                {
                    "action_index": index,
                    "row_count": 300,
                    "raw_mean_base_net_bps": 0.0,
                    "raw_minimum_base_net_bps": -10.0,
                    "raw_maximum_base_net_bps": 10.0,
                    "stress_profitable_count": 100,
                    "stress_unprofitable_count": 200,
                    "stress_profitable_rate": 1.0 / 3.0,
                    "stress_profitable_mean_base_net_bps": 5.0,
                    "stress_unprofitable_mean_base_net_bps": -2.5,
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
            engine.set_candidate(self.candidate([0.8]))
            payload = None
            for index in range(development.MIN_CAUSAL_FEATURE_HISTORY_ROWS):
                payload = engine.on_row(feature_row(index * 1000, index * 0.001))
            self.assertEqual(payload["status"], "ACTIVE")
            self.assertEqual(payload["action"]["direction"], 1)
            first_until = payload["active_until_exchange_ms"]
            first_started = payload["action"]["started_exchange_ms"]

            engine.candidate.model = FakeModel([0.2])
            held = engine.on_row(feature_row(301_000, 0.301))
            self.assertEqual(held["reason"], "frozen_action_holding_window")
            self.assertEqual(held["action"]["direction"], 1)
            self.assertEqual(held["action"]["started_exchange_ms"], first_started)
            self.assertEqual(held["active_until_exchange_ms"], first_until)
            engine.candidate.model = FakeModel([0.2])
            released = engine.on_row(feature_row(317_000, 0.317))
            self.assertEqual(released["status"], "FLAT")
            self.assertIsNone(released["action"])
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(persisted["live_promotion_eligible"])

    def test_policy_is_flat_when_threshold_not_met_and_fail_closed_on_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "signal.json"
            engine = policy.DemoPolicyEngine(signal_output=output)
            engine.set_candidate(self.candidate([0.35]))
            payload = None
            for index in range(development.MIN_CAUSAL_FEATURE_HISTORY_ROWS):
                payload = engine.on_row(feature_row(index * 1000, index * 0.001))
            self.assertEqual(payload["status"], "FLAT")
            self.assertIsNone(payload["action"])
            engine.publish_fail_closed("test_integrity_failure", 300_000)
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
                        "best_bid_size": 1.0,
                        "best_ask_size": 1.0,
                        "bid_depth_l5": 1.0,
                        "ask_depth_l5": 1.0,
                        "bid_depth_l20": 1.0,
                        "ask_depth_l20": 1.0,
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
