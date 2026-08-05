#!/usr/bin/env python3

import csv
import datetime as dt
import gzip
import io
import pathlib
import tempfile
import unittest

import fetch_bybit_trade_history as history


def trade_blob(lines):
    body = io.BytesIO()
    with gzip.GzipFile(fileobj=body, mode="wb") as compressed:
        compressed.write(
            (
                "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,"
                "homeNotional,foreignNotional,RPI\n" + "\n".join(lines) + "\n"
            ).encode()
        )
    return body.getvalue()


class BybitTradeHistoryTest(unittest.TestCase):
    def test_trade_flow_is_bucketed_causally(self):
        blob = trade_blob(
            [
                "1777593600.1,SOLUSDT,Buy,2,100,x,a,0,0,0,0",
                "1777593601.2,SOLUSDT,Sell,1,101,x,b,0,0,0,0",
            ]
        )
        bars = history.parse_trade_archive(
            blob, interval_ms=300000, large_trade_quote_threshold=150.0
        )
        bar = bars[1777593600000]
        self.assertEqual(bar.trade_count, 2)
        self.assertEqual(bar.buy_quote_volume, 200.0)
        self.assertEqual(bar.sell_quote_volume, 101.0)
        self.assertEqual(bar.large_trade_quote_volume, 200.0)

    def test_archive_url_is_official_daily_shape(self):
        url = history.build_archive_url(
            base_url=history.DEFAULT_BASE_URL,
            symbol="solusdt",
            day=dt.date(2026, 5, 1),
        )
        self.assertTrue(url.endswith("/SOLUSDT/SOLUSDT2026-05-01.csv.gz"))

    def test_write_rejects_missing_anchor_bar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "flow.csv"
            with self.assertRaisesRegex(ValueError, "coverage failed closed"):
                history.write_flow_csv(
                    path,
                    timestamps=[1000, 301000],
                    bars={1000: history.TradeFlowBar(timestamp_ms=1000, trade_count=1)},
                )


if __name__ == "__main__":
    unittest.main()
