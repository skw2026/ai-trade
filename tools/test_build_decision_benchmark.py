#!/usr/bin/env python3

import csv
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest


TOOLS_DIR = pathlib.Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_decision_benchmark as BUILDER  # noqa: E402


class BuildDecisionBenchmarkTest(unittest.TestCase):
    def write_json(self, path: pathlib.Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def write_feature(self, path: pathlib.Path, offset: float = 0.0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "ema_diff",
                    "zscore_48",
                    "mom_12",
                    "mom_48",
                    "ret_1",
                    "range_pct",
                    "vol_12",
                ]
            )
            for timestamp in range(1000, 7000, 1000):
                price = 100.0 + offset + timestamp / 1000.0
                writer.writerow(
                    [
                        timestamp,
                        price,
                        price + 1.0,
                        price - 1.0,
                        price + 0.25,
                        10.0,
                        0.01,
                        0.0,
                        0.01,
                        0.02,
                        0.001,
                        0.01,
                        0.01,
                    ]
                )

    def candidate_report(self) -> dict:
        objective = "aggregate_model_net_bps_per_unit_turnover_after_cost"
        return {
            "model_version": "integrator-v1",
            "feature_schema_version": "integrator-feature-v1",
            "factor_set_version": "factor-v1",
            "feature_names": ["ema_diff"],
            "feature_transform": {
                "feature_clipping_enabled": False,
                "feature_normalization_enabled": False,
            },
            "data": {
                "csv_path": "training.csv",
                "training_symbol": "BTCUSDT",
                "bar_interval_ms": 1000,
                "online_bar_source": "closed_ohlcv",
                "source_venue": "bybit",
                "source_category": "linear",
                "price_type": "trade_price",
                "volume_unit": "base_asset",
            },
            "metrics_oos": {
                "primary_objective": objective,
                "mean_model_net_edge_bps_per_round_trip": 1.0,
                "split_trained_count": 2,
                "split_count": 2,
            },
            "governance": {"pass": True, "primary_objective": objective},
            "model_artifact_status": "published",
        }

    def build_inputs(self, base: pathlib.Path, multi_symbol: bool = False) -> dict:
        inputs = base / "inputs"
        inputs.mkdir(parents=True)
        model = inputs / "integrator.cbm"
        model.write_bytes(b"integrator-model")
        candidate_report = inputs / "integrator.json"
        self.write_json(candidate_report, self.candidate_report())
        runtime_config = inputs / "runtime.yaml"
        runtime_config.write_text("system:\n  mode: live\n", encoding="utf-8")
        replay_config = inputs / "replay.yaml"
        replay_config.write_text("system:\n  mode: replay\n", encoding="utf-8")
        validation_config = inputs / "decision-policy.json"
        validation_config.write_text(
            (TOOLS_DIR.parent / "config" / "decision_evidence_validation.json")
            .read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        trade_bot = inputs / "trade_bot"
        trade_bot.write_bytes(b"trade-bot")

        symbols = ["BTCUSDT", "ETHUSDT"] if multi_symbol else ["BTCUSDT"]
        feature_by_symbol = {}
        corpus_by_symbol = {}
        for index, symbol in enumerate(symbols):
            feature = inputs / f"{symbol}.csv"
            self.write_feature(feature, offset=float(index) * 100.0)
            corpus = inputs / f"{symbol}-corpus.json"
            self.write_json(
                corpus,
                {
                    "schema_version": "replay_selection_manifest_v3",
                    "candidate_set_frozen": True,
                    "symbol": symbol,
                    "target_bucket": "trend",
                    "base_interval_ms": 1000,
                    "source_feature_csv": "selection.csv",
                    "source_feature_sha256": "a" * 64,
                },
            )
            feature_by_symbol[symbol] = feature
            corpus_by_symbol[symbol] = corpus

        model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
        report_sha = hashlib.sha256(candidate_report.read_bytes()).hexdigest()
        runs = []
        intervals = [(1000, 2000)] if multi_symbol else [(1000, 2000), (4000, 5000)]
        for start, end in intervals:
            for symbol in symbols:
                runs.append(
                    {
                        "symbol": symbol,
                        "segment": {
                            "start_timestamp": start,
                            "end_timestamp": end,
                            "target_bucket": "trend",
                        },
                    }
                )
        replay_report = inputs / "replay-report.json"
        self.write_json(
            replay_report,
            {
                "status": "pass",
                "target_bucket": "trend",
                "base_interval_ms": 1000,
                "base_interval_ms_by_symbol": {
                    symbol: 1000 for symbol in symbols
                },
                "candidate_identity": {
                    "model_version": "integrator-v1",
                    "model_sha256": model_sha,
                    "integrator_report_sha256": report_sha,
                },
                "runs": runs,
            },
        )
        output_dir = base / "output"
        return {
            "replay_report": replay_report,
            "feature_csv": feature_by_symbol["BTCUSDT"],
            "corpus_manifest": corpus_by_symbol["BTCUSDT"],
            "runtime_config": runtime_config,
            "replay_config": replay_config,
            "candidate_model": model,
            "candidate_report": candidate_report,
            "validation_config": validation_config,
            "trade_bot": trade_bot,
            "output_dir": output_dir,
            "manifest_path": output_dir / "decision-benchmark.json",
            "build_report_path": output_dir / "build-report.json",
            "feature_csv_by_symbol": feature_by_symbol if multi_symbol else {},
            "corpus_manifest_by_symbol": corpus_by_symbol if multi_symbol else {},
        }

    def test_builds_verified_policy_bound_benchmark_with_warmup_zero(self):
        with tempfile.TemporaryDirectory() as td:
            kwargs = self.build_inputs(pathlib.Path(td))
            report = BUILDER.build_decision_benchmark(**kwargs)

            self.assertEqual(report["status"], "VERIFIED", report)
            self.assertEqual(report["validation"]["identity_status"], "VERIFIED")
            manifest = json.loads(
                kwargs["manifest_path"].read_text(encoding="utf-8")
            )
            run_config_files = manifest["components"]["run_config"]["files"]
            self.assertEqual(
                [item["logical_name"] for item in run_config_files],
                ["decision_evidence_validation", "runtime_config"],
            )
            block = manifest["evaluation_universe"]["blocks"][0]
            event_path = pathlib.Path(block["executions"][0]["path"])
            with event_path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([int(row["timestamp"]) for row in rows], [1000, 2000])
            self.assertEqual({row["execution_enabled"] for row in rows}, {"1"})
            self.assertEqual(
                hashlib.sha256(event_path.read_bytes()).hexdigest(),
                block["executions"][0]["event_sha256"],
            )
            self.assertEqual(
                report["paired_inputs"]["feature_csv"],
                str(kwargs["feature_csv"].resolve()),
            )
            self.assertTrue(
                pathlib.Path(report["paired_inputs"]["corpus_manifest"]).is_file()
            )

    def test_benchmark_identity_is_stable_when_all_inputs_move(self):
        with tempfile.TemporaryDirectory() as td:
            base = pathlib.Path(td)
            first_inputs = self.build_inputs(base / "first")
            second_inputs = self.build_inputs(base / "second")

            first = BUILDER.build_decision_benchmark(**first_inputs)
            second = BUILDER.build_decision_benchmark(**second_inputs)

            self.assertEqual(first["status"], "VERIFIED", first)
            self.assertEqual(second["status"], "VERIFIED", second)
            self.assertEqual(
                first["validation"]["benchmark_id"],
                second["validation"]["benchmark_id"],
            )

    def test_missing_replay_producer_fails_closed_with_build_report(self):
        with tempfile.TemporaryDirectory() as td:
            kwargs = self.build_inputs(pathlib.Path(td))
            kwargs["replay_report"].unlink()

            report = BUILDER.build_decision_benchmark(**kwargs)

            self.assertEqual(report["status"], "UNVERIFIABLE")
            self.assertIn("input.replay_report_missing", report["errors"])
            self.assertTrue(kwargs["build_report_path"].is_file())
            self.assertFalse(kwargs["manifest_path"].exists())

    def test_multi_symbol_interval_builds_canonical_isolated_executions(self):
        with tempfile.TemporaryDirectory() as td:
            kwargs = self.build_inputs(pathlib.Path(td), multi_symbol=True)

            report = BUILDER.build_decision_benchmark(**kwargs)

            self.assertEqual(report["status"], "VERIFIED", report)
            manifest = json.loads(
                kwargs["manifest_path"].read_text(encoding="utf-8")
            )
            blocks = manifest["evaluation_universe"]["blocks"]
            self.assertEqual(len(blocks), 1)
            self.assertEqual(
                [item["symbol"] for item in blocks[0]["executions"]],
                ["BTCUSDT", "ETHUSDT"],
            )
            self.assertEqual(
                [item["execution_id"] for item in blocks[0]["executions"]],
                [
                    f"{blocks[0]['block_id']}:BTCUSDT",
                    f"{blocks[0]['block_id']}:ETHUSDT",
                ],
            )
            self.assertEqual(
                set(report["paired_inputs"]["feature_csv_by_symbol"]),
                {"BTCUSDT", "ETHUSDT"},
            )


if __name__ == "__main__":
    unittest.main()
