#!/usr/bin/env python3

import csv
import hashlib
import io
import pathlib
import tempfile
import unittest
import zipfile

import fetch_market_alpha_history as history


def archive_blob(rows):
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SOLUSDT-5m-2026-05.csv",
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,"
            "taker_buy_volume,taker_buy_quote_volume,ignore\n" + "\n".join(rows) + "\n",
        )
    return body.getvalue()


class MarketAlphaHistoryTest(unittest.TestCase):
    def test_parse_monthly_archive_preserves_flow_fields(self):
        blob = archive_blob(
            ["1777593600000,83,84,82,83.5,10,1777593899999,835,7,6,501,0"]
        )
        bars = history.parse_archive_zip(blob)
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].trade_count, 7)
        self.assertEqual(bars[0].taker_buy_quote_volume, 501.0)

    def test_checksum_filename_is_bound(self):
        digest = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(history.parse_checksum(f"{digest}  file.zip", "file.zip"), digest)
        with self.assertRaisesRegex(ValueError, "filename"):
            history.parse_checksum(f"{digest}  wrong.zip", "file.zip")

    def test_month_iteration_crosses_year(self):
        start = 1767225600000  # 2026-01-01 UTC
        end = 1772323200000  # 2026-03-01 UTC
        self.assertEqual(list(history.iter_months(start, end)), ["2026-01", "2026-02", "2026-03"])

    def test_alignment_is_exact_and_fails_closed(self):
        timestamps = [1000, 301000]
        bars = {
            symbol: {
                timestamp: history.AggregateBar(timestamp, timestamp + 299999, 10.0, 20.0, 3, 11.0)
                for timestamp in timestamps
            }
            for symbol in history.DEFAULT_SYMBOLS
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = pathlib.Path(temp_dir) / "alpha.csv"
            quality = history.validate_and_write_aligned(
                output_path=output,
                timestamps=timestamps,
                bars_by_symbol=bars,
                symbols=history.DEFAULT_SYMBOLS,
                interval_ms=300000,
            )
            self.assertEqual(quality["row_count"], 2)
            with output.open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["binance_sol_trade_count"], "3")
            del bars["BTCUSDT"][301000]
            with self.assertRaisesRegex(ValueError, "coverage failed closed"):
                history.validate_and_write_aligned(
                    output_path=output,
                    timestamps=timestamps,
                    bars_by_symbol=bars,
                    symbols=history.DEFAULT_SYMBOLS,
                    interval_ms=300000,
                )


if __name__ == "__main__":
    unittest.main()
