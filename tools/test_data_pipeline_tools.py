#!/usr/bin/env python3

import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
import zipfile

try:
    import numpy as np

    HAS_NUMPY = True
except ModuleNotFoundError:
    np = None
    HAS_NUMPY = False


TOOLS_DIR = pathlib.Path(__file__).resolve().parent


def load_module(name: str):
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FETCH = load_module("fetch_binance_archive")
STREAM = load_module("stream_market_ws")
GAP = load_module("gap_fill_klines")
if HAS_NUMPY:
    FEATURE = load_module("build_feature_store")
    BACKTEST = load_module("backtest_walkforward")
else:
    FEATURE = None
    BACKTEST = None


class FetchArchiveTest(unittest.TestCase):
    def test_build_daily_archive_url(self):
        day = FETCH.parse_date_ymd("2026-02-20")
        url = FETCH.build_daily_archive_url(
            base_url="https://data.binance.vision",
            market="futures_um",
            symbol="btcusdt",
            interval="5m",
            day=day,
        )
        self.assertIn("/data/futures/um/daily/klines/BTCUSDT/5m/", url)
        self.assertTrue(url.endswith("BTCUSDT-5m-2026-02-20.zip"))

    def test_parse_archive_zip(self):
        body = io.BytesIO()
        with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "BTCUSDT-5m-2026-02-20.csv",
                "open_time,open,high,low,close,volume\n"
                "1700000000000,50000,50100,49900,50050,12.3\n",
            )
        candles = FETCH.parse_archive_zip(body.getvalue())
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].timestamp_ms, 1700000000000)
        self.assertAlmostEqual(candles[0].close, 50050.0)


class StreamAndGapToolsTest(unittest.TestCase):
    def test_merge_and_gap_detection(self):
        existing = {
            1000: STREAM.Candle(1000, 1, 1, 1, 1, 1),
            2000: STREAM.Candle(2000, 1, 1, 1, 1, 1),
        }
        incoming = [
            STREAM.Candle(2000, 1, 1, 1, 1, 1),
            STREAM.Candle(3000, 1, 1, 1, 1, 1),
        ]
        added = STREAM.merge_candles(existing, incoming)
        self.assertEqual(added, 1)
        self.assertEqual(len(existing), 3)

        missing = GAP.detect_missing_timestamps([1000, 3000, 4000, 7000], 1000)
        self.assertEqual(missing, [2000, 5000, 6000])
        grouped = GAP.group_missing_ranges(missing, 1000)
        self.assertEqual(grouped, [(2000, 2000), (5000, 6000)])


@unittest.skipUnless(HAS_NUMPY, "numpy is required for feature/backtest script tests")
class FeatureAndBacktestTest(unittest.TestCase):
    def test_feature_builder_and_backtest_split(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ohlcv = root / "ohlcv.csv"
            feature_csv = root / "feature.csv"
            with ohlcv.open("w", encoding="utf-8") as fp:
                fp.write("timestamp,open,high,low,close,volume\n")
                for i in range(1, 240):
                    ts = i * 300000
                    close = 100.0 + 0.05 * i + (0.3 if i % 7 == 0 else -0.15)
                    high = close + 0.2
                    low = close - 0.2
                    volume = 10.0 + (i % 13)
                    fp.write(
                        f"{ts},{close:.6f},{high:.6f},{low:.6f},{close:.6f},{volume:.6f}\n"
                    )

            data = FEATURE.load_ohlcv(ohlcv)
            features = FEATURE.build_features(data, forward_bars=4)
            kept = FEATURE.write_feature_csv(feature_csv, features, drop_na=True)
            self.assertGreater(kept, 100)

            x_train = np.random.RandomState(7).normal(size=(120, len(BACKTEST.FEATURE_COLUMNS)))
            y_train = (0.0015 * x_train[:, 0] - 0.0007 * x_train[:, 1] + 0.0002).astype(
                np.float64
            )
            x_test = np.random.RandomState(11).normal(size=(40, len(BACKTEST.FEATURE_COLUMNS)))
            y_test = (0.0015 * x_test[:, 0] - 0.0007 * x_test[:, 1] + 0.0002).astype(
                np.float64
            )
            split = BACKTEST.run_split(
                split_index=0,
                x_train=x_train,
                y_train=y_train,
                x_test=x_test,
                y_test=y_test,
                train_start=0,
                train_end=120,
                test_start=120,
                test_end=160,
                fee_bps=6.0,
                slippage_bps=1.5,
                signal_threshold=0.0001,
                max_leverage=1.2,
                pred_scale=0.002,
                interval_minutes=5,
            )
            self.assertGreater(split.bars, 0)
            self.assertTrue(np.isfinite(split.sharpe))


if __name__ == "__main__":
    unittest.main()
