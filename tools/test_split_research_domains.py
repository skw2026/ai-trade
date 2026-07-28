#!/usr/bin/env python3

import csv
import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("split_research_domains.py")
SPEC = importlib.util.spec_from_file_location("split_research_domains", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
split_domains = MODULE.split_domains


def write_rows(path: pathlib.Path, count: int) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for index in range(count):
            writer.writerow(
                {
                    "timestamp": 1_700_000_000_000 + index * 300_000,
                    "open": 100 + index,
                    "high": 101 + index,
                    "low": 99 + index,
                    "close": 100 + index,
                    "volume": 10,
                }
            )


def write_holdout_ledger(
    path: pathlib.Path,
    entries: list[dict[str, object]],
) -> None:
    previous_sha256 = "0" * 64
    rendered: list[str] = []
    tail_sha256 = ""
    for index, values in enumerate(entries, start=1):
        entry = {
            "schema_version": "final_holdout_consumption_v2",
            "experiment_id": f"experiment-{index}",
            "candidate_identity_sha256": f"{index:064x}",
            "dataset_path": f"/tmp/holdout-{index}.csv",
            "dataset_sha256": f"{index + 100:064x}",
            "opened_at_utc": "2026-07-27T00:00:00Z",
            "status": "opened_before_evaluation",
            "previous_entry_sha256": previous_sha256,
            **values,
        }
        tail_sha256 = hashlib.sha256(
            json.dumps(
                entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        entry["entry_sha256"] = tail_sha256
        rendered.append(
            json.dumps(
                entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        previous_sha256 = tail_sha256
    path.write_text("\n".join(rendered) + "\n", encoding="utf-8")
    path.with_suffix(path.suffix + ".checkpoint.json").write_text(
        json.dumps(
            {
                "schema_version": "final_holdout_checkpoint_v1",
                "entry_count": len(entries),
                "tail_entry_sha256": tail_sha256,
                "updated_at_utc": "2026-07-27T00:00:00Z",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


class SplitResearchDomainsTest(unittest.TestCase):
    def test_creates_non_overlapping_embargoed_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            raw = root / "raw.csv"
            feature = root / "feature.csv"
            development = root / "development.csv"
            selection = root / "selection.csv"
            holdout = root / "holdout.csv"
            report_path = root / "report.json"
            write_rows(raw, 30)
            write_rows(feature, 28)

            report = split_domains(
                raw_csv=raw,
                feature_csv=feature,
                development_csv=development,
                selection_feature_csv=selection,
                holdout_feature_csv=holdout,
                report_path=report_path,
                selection_bars=5,
                holdout_bars=5,
                embargo_bars=2,
                min_development_bars=10,
                min_selection_feature_bars=5,
                min_holdout_feature_bars=3,
                symbol="SOLUSDT",
            )

            self.assertEqual(report["schema_version"], "research_domain_split_v2")
            self.assertEqual(report["rows"]["development"], 16)
            self.assertEqual(report["rows"]["selection_embargo"], 2)
            self.assertEqual(report["rows"]["selection_raw"], 5)
            self.assertEqual(report["rows"]["selection_feature"], 5)
            self.assertEqual(report["rows"]["holdout_embargo"], 2)
            self.assertEqual(report["rows"]["holdout_raw"], 5)
            self.assertEqual(report["rows"]["holdout_feature"], 3)
            self.assertLess(
                report["boundaries"]["development_end_ts_ms"],
                report["boundaries"]["selection_start_ts_ms"],
            )
            self.assertLess(
                report["boundaries"]["selection_end_ts_ms"],
                report["boundaries"]["holdout_start_ts_ms"],
            )
            self.assertFalse(report["contract"]["domains_overlap"])
            self.assertTrue(report_path.is_file())

    def test_rejects_insufficient_source_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            raw = root / "raw.csv"
            feature = root / "feature.csv"
            write_rows(raw, 10)
            write_rows(feature, 10)
            with self.assertRaisesRegex(ValueError, "below required"):
                split_domains(
                    raw_csv=raw,
                    feature_csv=feature,
                    development_csv=root / "development.csv",
                    selection_feature_csv=root / "selection.csv",
                    holdout_feature_csv=root / "holdout.csv",
                    report_path=root / "report.json",
                    selection_bars=3,
                    holdout_bars=5,
                    embargo_bars=2,
                    min_development_bars=5,
                    min_selection_feature_bars=3,
                    min_holdout_feature_bars=3,
                    symbol="SOLUSDT",
                )

    def test_rejects_reusing_consumed_final_holdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            raw = root / "raw.csv"
            feature = root / "feature.csv"
            ledger = root / "holdout_ledger.jsonl"
            write_rows(raw, 30)
            write_rows(feature, 30)
            consumed_end = 1_700_000_000_000 + 27 * 300_000
            write_holdout_ledger(
                ledger,
                [
                    {
                        "symbol": "SOLUSDT",
                        "bar_interval_ms": 300000,
                        "holdout_start_ts_ms": (
                            1_700_000_000_000 + 23 * 300_000
                        ),
                        "holdout_end_ts_ms": consumed_end,
                    }
                ],
            )
            with self.assertRaisesRegex(
                ValueError, "selection/final evidence overlaps consumed final"
            ):
                split_domains(
                    raw_csv=raw,
                    feature_csv=feature,
                    development_csv=root / "development.csv",
                    selection_feature_csv=root / "selection.csv",
                    holdout_feature_csv=root / "holdout.csv",
                    report_path=root / "report.json",
                    selection_bars=5,
                    holdout_bars=5,
                    embargo_bars=2,
                    min_development_bars=10,
                    min_selection_feature_bars=5,
                    min_holdout_feature_bars=3,
                    symbol="SOLUSDT",
                    holdout_ledger_path=ledger,
                )

    def test_rejects_fresh_final_when_selection_reuses_consumed_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            raw = root / "raw.csv"
            feature = root / "feature.csv"
            ledger = root / "holdout_ledger.jsonl"
            write_rows(raw, 40)
            write_rows(feature, 40)
            write_holdout_ledger(
                ledger,
                [
                    {
                        "symbol": "SOLUSDT",
                        "bar_interval_ms": 300000,
                        "holdout_start_ts_ms": (
                            1_700_000_000_000 + 28 * 300_000
                        ),
                        "holdout_end_ts_ms": (
                            1_700_000_000_000 + 32 * 300_000
                        ),
                    }
                ],
            )
            with self.assertRaisesRegex(
                ValueError, "selection/final evidence overlaps consumed final"
            ):
                split_domains(
                    raw_csv=raw,
                    feature_csv=feature,
                    development_csv=root / "development.csv",
                    selection_feature_csv=root / "selection.csv",
                    holdout_feature_csv=root / "holdout.csv",
                    report_path=root / "report.json",
                    selection_bars=5,
                    holdout_bars=5,
                    embargo_bars=2,
                    min_development_bars=10,
                    min_selection_feature_bars=5,
                    min_holdout_feature_bars=3,
                    symbol="SOLUSDT",
                    holdout_ledger_path=ledger,
                )

    def test_rejects_truncated_consumption_ledger_before_domain_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            raw = root / "raw.csv"
            feature = root / "feature.csv"
            ledger = root / "holdout_ledger.jsonl"
            write_rows(raw, 50)
            write_rows(feature, 50)
            write_holdout_ledger(
                ledger,
                [
                    {
                        "symbol": "SOLUSDT",
                        "bar_interval_ms": 300000,
                        "holdout_start_ts_ms": 1_700_000_000_000,
                        "holdout_end_ts_ms": 1_700_000_300_000,
                    },
                    {
                        "symbol": "BTCUSDT",
                        "bar_interval_ms": 300000,
                        "holdout_start_ts_ms": 1_700_000_600_000,
                        "holdout_end_ts_ms": 1_700_000_900_000,
                    },
                ],
            )
            first_line = ledger.read_text(encoding="utf-8").splitlines()[0]
            ledger.write_text(first_line + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError, "checkpoint mismatch"
            ):
                split_domains(
                    raw_csv=raw,
                    feature_csv=feature,
                    development_csv=root / "development.csv",
                    selection_feature_csv=root / "selection.csv",
                    holdout_feature_csv=root / "holdout.csv",
                    report_path=root / "report.json",
                    selection_bars=5,
                    holdout_bars=5,
                    embargo_bars=2,
                    min_development_bars=10,
                    min_selection_feature_bars=5,
                    min_holdout_feature_bars=3,
                    symbol="SOLUSDT",
                    holdout_ledger_path=ledger,
                )


if __name__ == "__main__":
    unittest.main()
