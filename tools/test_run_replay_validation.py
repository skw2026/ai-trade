#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_replay_module():
    module_path = pathlib.Path(__file__).with_name("run_replay_validation.py")
    spec = importlib.util.spec_from_file_location("run_replay_validation", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPLAY = load_replay_module()


class RunReplayValidationTest(unittest.TestCase):
    def test_stale_corpus_manifest_is_auto_refreshed_without_warning(self):
        rows = [
            REPLAY.FeatureRow(
                timestamp=1_700_000_000_000 + idx * 300_000,
                close=100.0 + idx,
                volume=1000.0,
                features={
                    "ema_diff": 0.01,
                    "zscore_48": 0.0,
                    "mom_12": 0.01,
                    "mom_48": 0.02,
                    "ret_1": 0.0,
                    "range_pct": 0.001,
                    "vol_12": 0.001,
                },
            )
            for idx in range(6)
        ]
        thresholds = REPLAY.RegimeThresholds(
            trend_abs_ema_diff=0.005,
            trend_abs_mom_48=0.01,
            extreme_vol_12=0.01,
            extreme_range_pct=0.01,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            feature_csv = tmp_path / "feature_store_5m.csv"
            feature_csv.write_text("timestamp,close,volume\n", encoding="utf-8")
            corpus_manifest = tmp_path / "replay_validation_trend_corpus_SOLUSDT.json"
            corpus_manifest.write_text(
                json.dumps(
                    {
                        "target_bucket": "trend",
                        "base_interval_ms": 300_000,
                        "segments": [
                            {
                                "start_timestamp": 1,
                                "end_timestamp": 2,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            selected, eligible, selection, warnings = REPLAY.select_replay_segments(
                rows,
                thresholds,
                feature_csv=feature_csv,
                target_bucket="trend",
                base_interval_ms=300_000,
                max_segments=2,
                min_segment_bars=2,
                corpus_manifest=corpus_manifest,
                refresh_corpus_manifest=False,
            )

            self.assertEqual(warnings, [])
            self.assertGreaterEqual(len(eligible), 1)
            self.assertEqual(len(selected), 1)
            self.assertTrue(selection["corpus_loaded"])
            self.assertTrue(selection["corpus_written"])
            self.assertTrue(selection["corpus_refreshed"])
            self.assertTrue(selection["corpus_auto_refreshed"])
            self.assertEqual(selection["selection_mode"], "dynamic_top_n_auto_refresh")
            self.assertTrue(selection["corpus_refresh_reasons"])
            refreshed = json.loads(corpus_manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                refreshed["segments"][0]["start_timestamp"],
                rows[0].timestamp,
            )


if __name__ == "__main__":
    unittest.main()
