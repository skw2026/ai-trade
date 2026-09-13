#!/usr/bin/env python3
"""Synthetic records only. No account/network needed by this test suite."""
import copy
import hashlib
import hmac
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import audit_bybit_readonly_evidence as audit


START, END, NOW = 1788710400000, 1788768000000, 1789300000000


def response(group, rows, cursor=""):
    data = rows if group == "account" else {"list": rows, "nextPageCursor": cursor}
    if group not in ("account", "wallet", "transactions"):
        data["category"] = "linear"
    if group.startswith("mark_"):
        data["symbol"] = "BTCUSDT"
    return audit.encode({"retCode": 0, "time": NOW, "result": data})


def demo_fixture():
    trade = {"id": "synthetic-tx", "tradeId": "synthetic-fill", "orderId": "synthetic-order",
             "symbol": "BTCUSDT", "currency": "USDT", "category": "linear", "type": "TRADE",
             "transactionTime": str(START + 1), "side": "Buy", "cashFlow": "0", "funding": "",
             "fee": "0.04", "change": "-0.04", "qty": "0.001", "tradePrice": "80000"}
    funding = {**trade, "id": "synthetic-funding", "type": "SETTLEMENT", "tradeId": "",
               "transactionTime": str(END), "funding": "-0.002", "fee": "0", "change": "-0.002"}
    execution = {"execId": "synthetic-fill", "orderId": "synthetic-order", "execType": "Trade",
                 "symbol": "BTCUSDT", "execTime": str(START + 1), "side": "Buy", "execFee": "0.04",
                 "execQty": "0.001", "execPrice": "80000", "feeCurrency": "USDT"}
    return {"account": [{"marginMode": "ISOLATED_MARGIN"}],
            "wallet": [{"accountType": "UNIFIED", "coin": [{"coin": "USDT", "walletBalance": "99999.123"}],
                        "totalInitialMargin": "", "totalMaintenanceMargin": "", "totalEquity": ""}],
            "positions": [], "orders": [], "open_orders": [], "transactions": [trade, funding],
            "executions": [execution]}


class FakeTransport:
    def __init__(self, groups, source="demo"):
        self.groups, self.source, self.calls = groups, source, []

    def get(self, request):
        self.calls.append(copy.deepcopy(request))
        audit.validate_request(self.source, request)
        for name, item in audit.plan(self.source, START, END).items():
            if item["path"] == request["path"]:
                rows = self.groups[name]
                return response(name, rows[0] if name == "account" else rows)
        when = request["params"]["start"] + 60_000
        name = f"mark_{when}"
        return response(name, self.groups[name])


class ReadonlyEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.clock = patch.object(audit, "now_ms", return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def capture(self, fake=None, source="demo"):
        return audit.collect(self.root, source, START, END, fake or FakeTransport(demo_fixture()))

    def mutate_manifest(self, capture, change):
        path = capture / "manifest.json"
        value = audit.decode(path.read_bytes())
        change(value)
        path.write_bytes(audit.encode(value))

    def test_plan_no_write_or_unsupported_fee_endpoints(self):
        requests = audit.plan("demo", START, END)
        self.assertEqual(len(requests), 7)
        for item in requests.values():
            audit.validate_request("demo", item)
            self.assertNotIn("fee-rate", item["path"])
        self.assertEqual(requests["transactions"]["params"]["currency"], "USDT")
        self.assertNotIn("category", requests["transactions"]["params"])
        for path in ("/v5/order/create", "/v5/account/set-margin-mode", "https://evil.example"):
            with self.assertRaises(ValueError):
                audit.validate_request("demo", {"path": path, "params": {}})

    def test_window_and_extra_queries_rejected(self):
        for start, end in ((False, END), (END, START), (1, 1 + 7 * audit.DAY)):
            with self.assertRaises(ValueError):
                audit.plan("demo", start, end)
        item = audit.plan("demo", START, END)["wallet"]
        item["params"]["api_key"] = "secret"
        with self.assertRaises(ValueError):
            audit.validate_request("demo", item)

    def test_credentials_no_cross_source_mixing_or_implicit_fallback(self):
        env = {"AI_TRADE_API_KEY": "legacy-key", "AI_TRADE_API_SECRET": "legacy-secret"}
        with self.assertRaisesRegex(ValueError, "UNAVAILABLE"):
            audit.credentials(env, False)
        self.assertEqual(audit.credentials(env, True), ("legacy-key", "legacy-secret"))
        env["AI_TRADE_BYBIT_DEMO_API_KEY"] = "half-key"
        with self.assertRaises(ValueError):
            audit.credentials(env, True)

    def test_env_reader_does_not_execute_or_expand(self):
        path = self.root / "env"
        path.write_text('UNRELATED=$(exit 1)\nAI_TRADE_BYBIT_DEMO_API_KEY="key"\nAI_TRADE_BYBIT_DEMO_API_SECRET=secret\n')
        self.assertEqual(audit.credentials(audit.credential_env(path), False), ("key", "secret"))
        path.write_text('AI_TRADE_BYBIT_DEMO_API_KEY=$(touch bad)\n')
        with self.assertRaisesRegex(ValueError, "EXPANSION"):
            audit.credential_env(path)
        path.write_text('AI_TRADE_API_KEY=one\nAI_TRADE_API_KEY=two\n')
        with self.assertRaisesRegex(ValueError, "DUPLICATE"):
            audit.credential_env(path)

    def test_transport_demo_domain_signature_get_and_no_redirect(self):
        transport = audit.Transport("demo", ("test-key", "test-secret"))
        item = audit.plan("demo", START, END)["account"]
        raw = response("account", {"marginMode": "ISOLATED_MARGIN"})
        class Reply:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, count): return raw
        with patch.object(transport.opener, "open", return_value=Reply()) as call:
            self.assertEqual(transport.get(item), raw)
        req = call.call_args.args[0]
        self.assertEqual(req.full_url, "https://api-demo.bybit.com/v5/account/info")
        self.assertEqual(req.method, "GET")
        expected = hmac.new(b"test-secret", f"{NOW}test-key5000".encode(), hashlib.sha256).hexdigest()
        self.assertEqual(req.get_header("X-bapi-sign"), expected)
        with self.assertRaisesRegex(ValueError, "REDIRECT"):
            audit.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")
        with self.assertRaises(ValueError):
            audit.Transport("public", ("test-key", "test-secret"))

    def test_demo_capture_replays_and_does_not_publish_values(self):
        capture = self.capture()
        report = audit.replay(capture)
        self.assertEqual(report["status"], "READONLY_CAPTURED_CHECKS_PASS")
        self.assertEqual(report["cash_identity_checked"], 2)
        self.assertEqual(report["matched_execution_transactions"], 1)
        self.assertFalse(report["account_margin_fields_applicable"])
        self.assertFalse(report["historical_margin_reconstructed"])
        self.assertFalse(report["c2_option_accounting_qualified"])
        self.assertFalse(report["promotion_authority"])
        text = json.dumps(report)
        for private in ("99999.123", "synthetic-order", "synthetic-fill", "-0.002", "walletBalance"):
            self.assertNotIn(private, text)
        self.assertEqual(capture.stat().st_mode & 0o777, 0o700)
        for file in capture.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(report, audit.replay(capture, report["manifest_sha256"]))

    def test_decimal_identity_and_rebate_sign(self):
        groups = demo_fixture()
        groups["transactions"][0].update(fee="-0.04", change="0.04")
        groups["executions"][0]["execFee"] = "-0.04"
        self.assertEqual(audit.audit_demo(groups, START, END)["matched_execution_transactions"], 1)
        groups["transactions"][0]["change"] = "-0.04"
        with self.assertRaisesRegex(ValueError, "CASH_IDENTITY"):
            audit.audit_demo(groups, START, END)

    def test_missing_fields_and_unmatched_records_are_gaps(self):
        groups = demo_fixture()
        del groups["transactions"][0]["funding"]
        groups["executions"] = []
        gaps = audit.audit_demo(groups, START, END)["gaps"]
        self.assertIn("CASH_IDENTITY_FIELDS_MISSING", gaps)
        self.assertIn("TRANSACTION_WITHOUT_EXECUTION", gaps)
        groups = demo_fixture()
        groups["transactions"] = []
        gaps = audit.audit_demo(groups, START, END)["gaps"]
        self.assertIn("EXECUTION_WITHOUT_TRANSACTION", gaps)
        self.assertIn("NO_TRANSACTION_EVIDENCE", gaps)

    def test_wrong_currency_duplicate_ids_window_fee_mismatch(self):
        for kind in ("currency", "duplicate_tx", "duplicate_exec", "time", "fee", "nan"):
            groups = demo_fixture()
            if kind == "currency": groups["transactions"][0]["currency"] = "USDC"
            if kind == "duplicate_tx": groups["transactions"].append(copy.deepcopy(groups["transactions"][0]))
            if kind == "duplicate_exec": groups["executions"] *= 2
            if kind == "time": groups["executions"][0]["execTime"] = str(END + 1)
            if kind == "fee": groups["executions"][0]["execFee"] = "0.03"
            if kind == "nan": groups["transactions"][0]["change"] = "NaN"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                audit.audit_demo(groups, START, END)

    def test_cross_margin_not_inferred_from_isolated_zeros(self):
        groups = demo_fixture()
        groups["wallet"][0].update(totalInitialMargin="0", totalMaintenanceMargin="0", totalEquity="0")
        self.assertFalse(audit.audit_demo(groups, START, END)["account_margin_fields_present"])
        groups["account"][0]["marginMode"] = "REGULAR_MARGIN"
        self.assertTrue(audit.audit_demo(groups, START, END)["account_margin_fields_present"])

    def test_cursor_is_consumed_and_missing_terminal_page_rejected(self):
        fake = FakeTransport(demo_fixture())
        original = fake.get
        def get(request):
            if request["path"] == "/v5/account/transaction-log":
                all_rows = fake.groups["transactions"]
                return response("transactions", all_rows[1:] if request["params"].get("cursor") else all_rows[:1],
                                "" if request["params"].get("cursor") else "next&page=2")
            return original(request)
        fake.get = get
        capture = self.capture(fake)
        self.assertEqual(audit.replay(capture)["status"], "READONLY_CAPTURED_CHECKS_PASS")
        self.mutate_manifest(capture, lambda m: m["pages"].pop())
        self.assertEqual(audit.replay(capture)["status"], "READONLY_CAPTURE_INCOMPLETE")

    def test_cursor_stall_and_page_cap_are_not_success(self):
        fake = FakeTransport(demo_fixture())
        original = fake.get
        def get(request):
            if request["path"] == "/v5/account/transaction-log":
                return response("transactions", fake.groups["transactions"], "stuck")
            return original(request)
        fake.get = get
        self.assertEqual(audit.replay(self.capture(fake))["status"], "READONLY_CAPTURE_INCOMPLETE")
        with patch.object(audit, "MAX_PAGES", 2):
            capture = self.capture()
            self.assertEqual(audit.replay(capture)["status"], "READONLY_CAPTURE_INCOMPLETE")

    def test_api_error_is_retained_not_dumped(self):
        fake = FakeTransport(demo_fixture())
        fake.get = lambda request: audit.encode({"retCode": 10003, "retMsg": "private-account-detail", "time": NOW, "result": {}})
        capture = self.capture(fake)
        report = audit.replay(capture)
        self.assertEqual(report["status"], "READONLY_CAPTURE_INCOMPLETE")
        self.assertNotIn("private-account-detail", json.dumps(report))
        self.assertTrue((capture / "0000.raw").is_file())

    def test_manifest_request_domain_method_hash_and_symlink_tampering(self):
        for kind in ("domain", "method", "path", "params", "timing", "file"):
            capture = self.capture()
            def change(m):
                if kind == "domain": m["domain"] = "https://api.bybit.com"
                if kind == "method": m["method"] = "POST"
                if kind == "path": m["pages"][0]["request"]["path"] = "/v5/order/create"
                if kind == "params": m["pages"][0]["request"]["params"]["symbol"] = "wrong"
                if kind == "timing": m["pages"][0]["received_ms"] = 1
                if kind == "file": m["pages"][0]["file"] = "../secret"
            self.mutate_manifest(capture, change)
            with self.subTest(kind=kind), self.assertRaises(ValueError): audit.replay(capture)
        capture = self.capture()
        with self.assertRaisesRegex(ValueError, "MANIFEST_HASH"):
            audit.replay(capture, "0" * 64)
        (capture / "0000.raw").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "RAW_HASH"):
            audit.replay(capture)
        link = self.root / "link"
        link.symlink_to(capture)
        with self.assertRaisesRegex(ValueError, "SYMLINK"):
            audit.replay(link)

    def test_public_rates_context_and_current_rules_only(self):
        groups = {"funding": [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(END)}],
                  "instrument": [{"symbol": "BTCUSDT", "settleCoin": "USDT", "fundingInterval": 480}],
                  "risk_limit": [{"symbol": "BTCUSDT", "initialMargin": "0.01", "maintenanceMargin": "0.005"}],
                  f"mark_{END}": [[str(t), "80000", "80002", "79999", "80001"] for t in (END + 60_000, END, END - 60_000)]}
        capture = self.capture(FakeTransport(groups, "public"), "public")
        report = audit.replay(capture)
        self.assertEqual(report["status"], "READONLY_CAPTURED_CHECKS_PASS")
        self.assertEqual(report["mark_context_candles"], 3)
        self.assertFalse(report["exact_funding_marks_qualified"])
        self.assertFalse(report["historical_margin_parameters_qualified"])
        groups[f"mark_{END}"][0][2] = "70000"
        with self.assertRaisesRegex(ValueError, "OHLC"):
            audit.audit_public(groups, START, END)

    def test_public_missing_context_not_silently_qualified(self):
        groups = {"funding": [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(END)}]}
        with self.assertRaisesRegex(ValueError, "CONTEXT_INCOMPLETE"):
            audit.audit_public(groups, START, END)

    def test_cli_missing_credentials_no_traceback_or_secret(self):
        env = {key: value for key, value in os.environ.items() if key not in audit.KEY_NAMES}
        result = subprocess.run([sys.executable, audit.__file__, "collect", "--root", str(self.root)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["reason"], "DEMO_CREDENTIALS_UNAVAILABLE")
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_duplicate_json_and_unsafe_decimal_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.assertRaises(ValueError): audit.decode(raw)
        for value in (True, 1.2, "Infinity", "1e100", "", "0." + "1" * 25):
            with self.assertRaises(ValueError): audit.number(value)

    def test_real_demo_missing_account_time_and_null_empty_transaction_cursor(self):
        fake = FakeTransport(demo_fixture())
        original = fake.get
        def get(request):
            raw = original(request)
            payload = audit.decode(raw)
            if request["path"] == "/v5/account/info":
                del payload["time"]
            if request["path"] == "/v5/account/transaction-log":
                payload["result"].update(list=[], nextPageCursor=None)
            return audit.encode(payload)
        fake.get = get
        capture = self.capture(fake)
        # Old collector rejected these exact field shapes. Keep its historical
        # verdict while independently proving the retained pages with new code.
        self.mutate_manifest(capture, lambda m: m["errors"].update(account="SERVER_TIME_INVALID", transactions="CURSOR_INVALID"))
        report = audit.replay(capture)
        self.assertEqual(report["status"], "READONLY_CAPTURED_GAPS")
        self.assertEqual(report["response_time_missing_groups"], ["account"])
        self.assertEqual(report["counts"]["transactions"], 0)
        self.assertEqual(report["execution_fee_records_checked"], 1)
        self.assertIn("ACCOUNT_RESPONSE_TIME_NOT_PROVIDED", report["gaps"])
        self.assertIn("NO_TRANSACTION_EVIDENCE", report["gaps"])
        self.assertFalse(report["history_exhaustion_proves_retention"])
        self.assertEqual(report["collector_error_codes"]["account"], "SERVER_TIME_INVALID")

    def test_null_cursor_nonempty_page_and_other_missing_time_still_fail(self):
        with self.assertRaisesRegex(ValueError, "CURSOR_INVALID"):
            audit.response_rows(response("transactions", demo_fixture()["transactions"], None), "transactions")
        payload = audit.decode(response("wallet", demo_fixture()["wallet"]))
        del payload["time"]
        with self.assertRaisesRegex(ValueError, "SERVER_TIME_INVALID"):
            audit.response_rows(audit.encode(payload), "wallet")

    def test_funding_execution_is_separate_from_cash_ledger(self):
        groups = demo_fixture()
        groups["transactions"] = []
        groups["executions"].append({**groups["executions"][0], "execId": "funding", "execType": "Funding", "execFee": "0.002"})
        result = audit.audit_demo(groups, START, END)
        self.assertEqual(result["funding_execution_records"], 1)
        self.assertIn("NO_TRANSACTION_EVIDENCE", result["gaps"])
        self.assertNotIn("NO_FUNDING_SETTLEMENT_OBSERVED", result["gaps"])

    def test_capture_parent_must_be_unique(self):
        capture = self.capture()
        result = subprocess.run([sys.executable, audit.__file__, "replay", "--capture-parent", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.capture()
        result = subprocess.run([sys.executable, audit.__file__, "replay", "--capture-parent", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "CAPTURE_PARENT_NOT_UNIQUE")


if __name__ == "__main__":
    unittest.main()
