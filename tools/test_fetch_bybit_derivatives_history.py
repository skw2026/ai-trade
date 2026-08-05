#!/usr/bin/env python3

import csv
import pathlib
import tempfile
import unittest

import fetch_bybit_derivatives_history as history


class FetchBybitDerivativesHistoryTest(unittest.TestCase):
    def test_backward_pagination_advances_without_duplicates(self):
        pages = {
            10: [history.Point(10, (1.0,)), history.Point(9, (2.0,))],
            8: [history.Point(8, (3.0,)), history.Point(7, (4.0,))],
        }
        calls = []

        def request(_start, end, _limit):
            calls.append(end)
            return pages.get(end, [])

        points, page_count = history.collect_backward(
            start_ms=7,
            end_ms=10,
            page_limit=2,
            request_page=request,
            expected_step_ms=1,
            sleep_sec=0.0,
        )
        self.assertEqual([point.timestamp_ms for point in points], [7, 8, 9, 10])
        self.assertEqual(page_count, 2)
        self.assertEqual(calls, [10, 8])

    def test_alignment_delays_slow_series_and_never_looks_forward(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "aligned.csv"
            missing = history.write_aligned_csv(
                path=output,
                bar_timestamps=[100, 200, 300],
                premium=[history.Point(100, (0.1,)), history.Point(300, (0.3,))],
                open_interest=[history.Point(100, (10.0,))],
                account_ratio=[history.Point(100, (0.6, 0.4))],
                funding=[history.Point(200, (0.0001,))],
                slow_publication_delay_ms=100,
            )
            with output.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["premium_index_close"], "0.1")
            self.assertEqual(rows[0]["open_interest"], "")
            self.assertEqual(rows[1]["open_interest"], "10")
            self.assertEqual(rows[1]["funding_rate"], "0.0001")
            self.assertEqual(rows[2]["premium_index_close"], "0.3")
            self.assertEqual(missing["open_interest"], 1)
            self.assertEqual(missing["account_ratio"], 1)
            self.assertEqual(missing["funding"], 1)

    def test_parsers_drop_malformed_rows(self):
        points = history.parse_dict_points(
            [
                {"timestamp": "100", "value": "2.5"},
                {"timestamp": "bad", "value": "2.5"},
                {"timestamp": "101"},
            ],
            timestamp_key="timestamp",
            value_keys=("value",),
        )
        self.assertEqual(points, [history.Point(100, (2.5,))])
        premium = history.parse_premium_points([["100", "0", "0", "0", "0.2"], []])
        self.assertEqual(premium, [history.Point(100, (0.2,))])


if __name__ == "__main__":
    unittest.main()
