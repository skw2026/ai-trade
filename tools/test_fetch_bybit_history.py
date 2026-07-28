#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import tempfile
import unittest


def load_module():
    path = pathlib.Path(__file__).with_name("fetch_bybit_history.py")
    spec = importlib.util.spec_from_file_location("fetch_bybit_history", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HISTORY = load_module()


class FetchBybitHistoryTest(unittest.TestCase):
    def test_future_requested_end_is_clamped_to_server_closed_boundary(self):
        end_ms, boundary_ms = HISTORY.resolve_closed_end_ms(
            requested_end_ms=1_800_000_000_000,
            server_time_ms=1_700_000_650_000,
            bar_ms=300_000,
        )
        self.assertEqual(boundary_ms, 1_700_000_400_000)
        self.assertEqual(end_ms, boundary_ms)

    def test_past_requested_end_remains_the_upper_bound(self):
        end_ms, boundary_ms = HISTORY.resolve_closed_end_ms(
            requested_end_ms=1_600_000_000_000,
            server_time_ms=1_700_000_650_000,
            bar_ms=300_000,
        )
        self.assertEqual(boundary_ms, 1_700_000_400_000)
        self.assertEqual(end_ms, 1_600_000_000_000)

    def test_collect_history_paginates_backwards_and_deduplicates(self):
        calls = []

        def request(start_ms, end_ms, limit):
            calls.append((start_ms, end_ms, limit))
            if len(calls) == 1:
                timestamps = [500, 400, 300]
            else:
                timestamps = [300, 200, 100]
            return [
                HISTORY.Candle(ts, 10.0, 11.0, 9.0, 10.5, 2.0)
                for ts in timestamps
            ]

        candles, pages = HISTORY.collect_history(
            start_ms=100,
            end_ms_exclusive=600,
            bar_ms=100,
            page_limit=3,
            request=request,
        )
        self.assertEqual(pages, 2)
        self.assertEqual(
            [candle.timestamp_ms for candle in candles],
            [100, 200, 300, 400, 500],
        )
        self.assertLess(calls[1][1], calls[0][1])

    def test_write_csv_uses_complete_ohlcv_schema(self):
        with tempfile.TemporaryDirectory() as td:
            output = pathlib.Path(td) / "ohlcv.csv"
            HISTORY.write_csv(
                output,
                [HISTORY.Candle(100, 10.0, 11.0, 9.0, 10.5, 2.0)],
            )
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                lines[0], "timestamp,open,high,low,close,volume"
            )
            self.assertEqual(len(lines), 2)


if __name__ == "__main__":
    unittest.main()
