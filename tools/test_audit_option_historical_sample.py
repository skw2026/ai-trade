#!/usr/bin/env python3
import gzip
import json
import pathlib
import tempfile
import unittest

import audit_option_historical_sample as audit

SYMBOL = "BTC-2SEP26-78750-C-USDT"
DATE = "2026-09-01"


def record(*, row=None, kind="snapshot", offset=0):
    fields = {"symbol": SYMBOL, "bidPrice": "595", "askPrice": "605",
              "bidSize": "1", "askSize": "2", "indexPrice": "78581",
              "markPrice": "603", "markPriceIv": "0.35", "delta": "0.46",
              "gamma": "0.0002", "vega": "18", "theta": "-253"}
    if row is not None:
        fields = row
    message = {"topic": "tickers." + SYMBOL, "ts": 1788220800000 + offset,
               "type": kind, "data": fields}
    return (f"2026-09-01T00:00:00.{offset:03d}000Z " + json.dumps(message) + "\n").encode()


class HistoricalSampleTest(unittest.TestCase):
    def audit(self, raw):
        return audit.audit_sample(raw, date=DATE, offset=0, symbols=[SYMBOL])

    def test_real_ws_names_and_no_promotion(self):
        result = self.audit(record())
        self.assertEqual(result["status"], "PASS_SAMPLE_SCHEMA_ONLY")
        self.assertFalse(result["payoff_evidence"])
        self.assertFalse(result["continuous_history_qualified"])
        self.assertFalse(any(result["authorities"].values()))
        self.assertIn("actual_delivery_prices_not_predicted_delivery_prices", result["remaining_requirements"])

    def test_plain_and_gzip_are_supported_but_hash_binds_raw(self):
        plain, compressed = record(), gzip.compress(record())
        self.assertEqual(self.audit(plain)["status"], self.audit(compressed)["status"])
        self.assertNotEqual(self.audit(plain)["raw_sha256"], self.audit(compressed)["raw_sha256"])

    def test_delta_needs_snapshot_and_disconnect_resets_state(self):
        delta = record(row={"bidPrice": "590"}, kind="delta", offset=1)
        self.assertEqual(self.audit(delta)["status"], "REJECTED_SAMPLE")
        self.assertEqual(self.audit(record() + delta)["message_count"], 2)
        self.assertEqual(self.audit(record() + b"\n" + delta)["status"], "REJECTED_SAMPLE")

    def test_missing_rest_or_nonfinite_fields_rejected(self):
        for bad in (record().replace(b'"bidSize": "1"', b'"bidSize": "0"'),
                    record().replace(b'"delta": "0.46"', b'"delta": "nan"'),
                    record().replace(b'"bidPrice"', b'"bid1Price"'),
                    record().replace(b'"askPrice": "605"', b'"askPrice": "590"')):
            with self.subTest(bad=bad):
                self.assertEqual(self.audit(bad)["status"], "REJECTED_SAMPLE")

    def test_wrong_time_and_symbol_are_rejected(self):
        for bad in (record().replace(b"T00:00:00", b"T00:01:00"),
                    record().replace(SYMBOL.encode(), b"ETH-2SEP26-2000-C-USDT")):
            self.assertEqual(self.audit(bad)["status"], "REJECTED_SAMPLE")

    def test_free_scoped_bounded_requests_only(self):
        for date, offset, symbols in (("2026-09-02", 0, [SYMBOL]),
                                      (DATE, 1440, [SYMBOL]), (DATE, 0, []),
                                      (DATE, 0, [SYMBOL, SYMBOL]), (DATE, 0, ["OPTIONS"])):
            with self.assertRaises(ValueError):
                audit.request_contract(date, offset, symbols)

    def test_empty_and_html_not_qualified(self):
        with self.assertRaises(ValueError):
            self.audit(b"")
        self.assertEqual(self.audit(b"<html>error</html>")["status"], "REJECTED_SAMPLE")

    def test_evidence_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "sample.raw"
            audit.persist_new_or_identical(path, b"original")
            audit.persist_new_or_identical(path, b"original")
            with self.assertRaises(ValueError):
                audit.persist_new_or_identical(path, b"different")
            link = pathlib.Path(root) / "link"
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                audit.persist_new_or_identical(link, b"original")


if __name__ == "__main__":
    unittest.main()
