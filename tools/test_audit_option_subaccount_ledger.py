#!/usr/bin/env python3
"""Hand-calculated, explicitly synthetic cases; no exchange account input."""
import copy
from decimal import Decimal
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import audit_option_subaccount_ledger as ledger


ROOT = pathlib.Path(__file__).resolve().parents[1]
CALL = "BTC-2SEP26-80000-C-USDT"
PUT = "BTC-2SEP26-80000-P-USDT"
PERP = "BTCUSDT"
START = 1788335990000  # 2026-09-02 07:59:50 UTC; synthetic sequence only.
EXPIRY = START + 10000


def bbo(now, bid, ask, mark=None, size="1"):
    result = {"ts_ms": now, "bid": str(bid), "ask": str(ask),
              "bid_size": size, "ask_size": size}
    if mark is not None:
        result["mark"] = str(mark)
    return result


def checkpoint(seq, kind, offset, cash, nav, positions, valuation, **event):
    now = START + offset
    return {"seq": seq, "id": f"fixture-{seq}", "scope_id": "offline_subaccount",
            "type": kind, "ts_ms": now, "valuation": valuation,
            "margin": {"scope_id": "offline_subaccount", "margin_mode": "REGULAR_MARGIN",
                       "ts_ms": now, "positions_btc": positions, "wallet_usdt": str(cash),
                       "usdt_usd_index": "1", "total_equity_usd": str(nav),
                       "total_initial_margin_usd": "100", "total_maintenance_margin_usd": "50",
                       "account_im_rate": "0.1", "account_mm_rate": "0.05",
                       "total_available_balance_usd": "800", "liabilities_usdt": "0"},
            **event}


def fixture():
    """1000 -> 1003.3: premiums 20, delivery -10, hedge -5, funding -.8, fees .9.

    The option liability exactly offsets premium at opening, before fees. Hedge
    0.01 BTC open 80000, close 79500. At funding, hedge mark 80000 and rate .001.
    Values are manually specified, never obtained by calling the code under test.
    """
    option_marks = lambda now: {CALL: bbo(now, 990, 1010, 1000)}
    pair_marks = lambda now: {**option_marks(now), PUT: bbo(now, 990, 1010, 1000)}
    hedge_marks = lambda now: {**pair_marks(now), PERP: bbo(now, 79990, 80000, 80000)}
    events = [checkpoint(0, "MARK", 0, "1000", "1000", {}, {})]
    events.append(checkpoint(1, "FILL", 1000, "1009.8", "999.8", {CALL: "-0.01"},
                             option_marks(START + 1000), symbol=CALL, signed_qty_btc="-0.01",
                             price_usdt_per_btc="1000", fee_usdt="0.2",
                             execution_quote=bbo(START + 1000, 1000, 1010)))
    events.append(checkpoint(2, "FILL", 2000, "1019.6", "999.6", {CALL: "-0.01", PUT: "-0.01"},
                             pair_marks(START + 2000), symbol=PUT, signed_qty_btc="-0.01",
                             price_usdt_per_btc="1000", fee_usdt="0.2",
                             execution_quote=bbo(START + 2000, 1000, 1010)))
    events.append(checkpoint(3, "FILL", 3000, "1019.4", "999.4", {CALL: "-0.01", PUT: "-0.01", PERP: "0.01"},
                             hedge_marks(START + 3000), symbol=PERP, signed_qty_btc="0.01",
                             price_usdt_per_btc="80000", fee_usdt="0.2",
                             execution_quote=bbo(START + 3000, 79990, 80000)))
    events.append(checkpoint(4, "FUNDING", 4000, "1018.6", "998.6", {CALL: "-0.01", PUT: "-0.01", PERP: "0.01"},
                             hedge_marks(START + 4000), symbol=PERP, settlement_id="funding-1",
                             rate_kind="settled_rate", position_qty_btc="0.01", rate="0.001",
                             mark_usdt_per_btc="80000", cash_delta_usdt="-0.8"))
    events.append(checkpoint(5, "FILL", 5000, "1013.4", "993.4", {CALL: "-0.01", PUT: "-0.01"},
                             pair_marks(START + 5000), symbol=PERP, signed_qty_btc="-0.01",
                             price_usdt_per_btc="79500", fee_usdt="0.2",
                             execution_quote=bbo(START + 5000, 79500, 79510)))
    events.append(checkpoint(6, "DELIVERY", 10000, "1003.3", "1003.3", {PUT: "-0.01"},
                             {PUT: bbo(EXPIRY, 0, 1, 0)}, symbol=CALL,
                             price_kind="official_delivery", delivery_price="81000",
                             cash_delta_before_fee_usdt="-10", fee_usdt="0.1"))
    events.append(checkpoint(7, "DELIVERY", 10000, "1003.3", "1003.3", {}, {}, symbol=PUT,
                             price_kind="official_delivery", delivery_price="81000",
                             cash_delta_before_fee_usdt="0", fee_usdt="0"))
    return {"schema_version": "option_subaccount_ledger_input_v1", "evidence_kind": "synthetic_fixture",
            "scope_id": "offline_subaccount", "simulation_capital_usdt": "1000",
            "start_ts_ms": START, "end_ts_ms": EXPIRY,
            "illustrative_limits": {"im_rate_max": "0.5", "mm_rate_reduce": "0.6", "mm_rate_exit": "0.8",
                                    "drawdown_exit": "0.2", "quote_max_age_ms": 1000, "checkpoint_max_gap_ms": 6000},
            "instruments": {
                CALL: {"kind": "call", "strike": "80000", "expiry_ts_ms": EXPIRY,
                       "quantity_unit": "BTC", "settle_coin": "USDT", "qty_step": "0.01"},
                PUT: {"kind": "put", "strike": "80000", "expiry_ts_ms": EXPIRY,
                      "quantity_unit": "BTC", "settle_coin": "USDT", "qty_step": "0.01"},
                PERP: {"kind": "linear_perpetual", "quantity_unit": "BTC", "settle_coin": "USDT", "qty_step": "0.001"}},
            "funding_schedule": [{"settlement_id": "funding-1", "ts_ms": START + 4000, "symbol": PERP}],
            "events": events}


class SubaccountLedgerTest(unittest.TestCase):
    def run_case(self, data=None, scope=None):
        return ledger.audit(copy.deepcopy(ledger.SCOPE) if scope is None else scope,
                            fixture() if data is None else data)

    def assert_invalid(self, data, reason):
        result = self.run_case(data)
        self.assertEqual(result["status"], "TECHNICALLY_INVALID", result)
        self.assertIn(reason, result["error"])
        self.assertFalse(any(result["authorities"].values()))

    def test_hand_calculated_lifecycle_and_nav(self):
        result = self.run_case()
        self.assertEqual(result["status"], "PASS_OFFLINE_ACCOUNTING_ONLY", result)
        self.assertEqual(Decimal(result["final_cash_usdt"]), Decimal("1003.3"))
        self.assertEqual(Decimal(result["pnl_on_simulated_capital_usdt"]), Decimal("3.3"))
        self.assertEqual(Decimal(result["return_on_simulated_capital"]), Decimal("0.0033"))
        self.assertEqual(Decimal(result["max_drawdown_observed_checkpoints_only"]), Decimal("0.0066"))
        self.assertEqual({key: Decimal(value) for key, value in result["totals_usdt"].items()},
                         {"option_cashflow": Decimal(10), "hedge_realized_pnl": Decimal(-5),
                          "funding": Decimal("-.8"), "fees": Decimal(".9")})
        self.assertEqual(Decimal(result["checkpoints"][1]["nav_usdt"]), Decimal("999.8"))
        self.assertEqual(Decimal(result["checkpoints"][3]["cash_usdt"]), Decimal("1019.4"))
        self.assertFalse(any(result["authorities"].values()))
        for field in ("economic_qualification", "historical_data_qualified", "actual_account_performance",
                      "exchange_margin_model_validated", "production_integrated"):
            self.assertIs(result[field], False)

    def test_repository_scope_is_offline_and_no_funding_authority(self):
        scope = json.loads((ROOT / "config/option_subaccount_research_scope_v1.json").read_text())
        self.assertEqual(self.run_case(scope=scope)["status"], "PASS_OFFLINE_ACCOUNTING_ONLY")
        for permission in ledger.PERMISSIONS:
            bad = copy.deepcopy(scope)
            bad["permissions"][permission] = not bad["permissions"][permission]
            self.assertEqual(self.run_case(scope=bad)["status"], "TECHNICALLY_INVALID")
        for field, value in (("approved_capital_usdt", "1000"), ("subaccount_uid", "123"),
                             ("automatic_top_up", True), ("assume_loss_capped_by_deposit", True),
                             ("margin_mode", "PORTFOLIO_MARGIN")):
            bad = copy.deepcopy(scope)
            bad[field] = value
            self.assertEqual(self.run_case(scope=bad)["status"], "TECHNICALLY_INVALID")

    def test_hedge_average_partial_close_flip_and_short_funding(self):
        data = fixture()
        data["instruments"] = {PERP: data["instruments"][PERP]}
        data["end_ts_ms"] = START + 6000
        data["funding_schedule"] = [{"settlement_id": "short-funding", "ts_ms": START + 5000, "symbol": PERP}]
        events = [checkpoint(0, "MARK", 0, "1000", "1000", {}, {})]
        # seq, signed fill, execution, resulting position, resulting mark, cash, NAV.
        for seq, qty, price, position, mark, cash, nav in (
            (1, "0.02", "80000", "0.02", "80000", "999.9", "999.9"),
            (2, "0.01", "83000", "0.03", "81000", "999.8", "999.8"),
            (3, "-0.01", "82000", "0.02", "82000", "1009.7", "1029.7"),
            (4, "-0.03", "79000", "-0.01", "79000", "969.6", "969.6"),
        ):
            now = START + seq * 1000
            events.append(checkpoint(seq, "FILL", seq * 1000, cash, nav, {PERP: position},
                                     {PERP: bbo(now, mark, mark, mark)}, symbol=PERP,
                                     signed_qty_btc=qty, price_usdt_per_btc=price, fee_usdt="0.1",
                                     execution_quote=bbo(now, price, price)))
        events.append(checkpoint(5, "FUNDING", 5000, "970.39", "970.39", {PERP: "-0.01"},
                                 {PERP: bbo(START + 5000, 79000, 79000, 79000)}, symbol=PERP,
                                 settlement_id="short-funding", rate_kind="settled_rate", position_qty_btc="-0.01",
                                 rate="0.001", mark_usdt_per_btc="79000", cash_delta_usdt="0.79"))
        events.append(checkpoint(6, "FILL", 6000, "980.29", "980.29", {}, {}, symbol=PERP,
                                 signed_qty_btc="0.01", price_usdt_per_btc="78000", fee_usdt="0.1",
                                 execution_quote=bbo(START + 6000, 78000, 78000)))
        data["events"] = events
        result = self.run_case(data)
        self.assertEqual(result["status"], "PASS_OFFLINE_ACCOUNTING_ONLY", result)
        self.assertEqual(Decimal(result["totals_usdt"]["hedge_realized_pnl"]), Decimal(-20))
        self.assertEqual(Decimal(result["totals_usdt"]["funding"]), Decimal(".79"))
        self.assertEqual(Decimal(result["final_cash_usdt"]), Decimal("980.29"))

    def test_funding_sign_position_prediction_and_boundary(self):
        for field, value, message in (("cash_delta_usdt", "0.8", "funding cashflow"),
                                      ("position_qty_btc", "-0.01", "funding position"),
                                      ("position_qty_btc", "0.010000001", "funding position"),
                                      ("rate_kind", "predicted", "predicted funding"),
                                      ("settlement_id", "another", "funding identity")):
            data = fixture()
            data["events"][4][field] = value
            self.assert_invalid(data, message)
        data = fixture()
        data["funding_schedule"][0]["ts_ms"] += 1
        self.assert_invalid(data, "funding identity/time")

    def test_missing_and_duplicate_funding_are_not_silent(self):
        data = fixture()
        data["funding_schedule"].append({"settlement_id": "missing", "ts_ms": START + 6000, "symbol": PERP})
        result = self.run_case(data)
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("SCHEDULED_FUNDING_MISSING", result["missing_evidence"])
        data = fixture()
        duplicate = copy.deepcopy(data["funding_schedule"][0])
        duplicate["settlement_id"] = "same-boundary-other-id"
        data["funding_schedule"].append(duplicate)
        self.assert_invalid(data, "duplicate funding settlement")

    def test_repeated_event_mixed_account_and_transfers_rejected(self):
        for field, value, message in (("id", "fixture-0", "duplicate event"),
                                      ("scope_id", "parent_account", "mixed account"),
                                      ("type", "TRANSFER_IN", "unsupported event"),
                                      ("seq", 0, "event sequence")):
            data = fixture()
            data["events"][1][field] = value
            self.assert_invalid(data, message)

    def test_execution_price_lot_depth_and_quote_time(self):
        for field, value, message in (("signed_qty_btc", "-0.001", "invalid lot"),
                                      ("price_usdt_per_btc", "1001", "taker price"),
                                      ("fee_usdt", "-1", "nonnegative")):
            data = fixture()
            data["events"][1][field] = value
            self.assert_invalid(data, message)
        for field, value, message in (("bid_size", "0.001", "taker price/quantity"),
                                      ("ts_ms", START + 1001, "future"),
                                      ("ts_ms", START - 1, "stale"),
                                      ("ask", "999", "crossed quote")):
            data = fixture()
            data["events"][1]["execution_quote"][field] = value
            self.assert_invalid(data, message)

    def test_missing_stale_or_inconsistent_margin_never_passes(self):
        data = fixture()
        data["events"][1].pop("margin")
        result = self.run_case(data)
        self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("MARGIN_EVIDENCE_MISSING", result["missing_evidence"])
        for field, value, message in (("scope_id", "parent", "margin scope"),
                                      ("ts_ms", START, "post-event"),
                                      ("positions_btc", {}, "position set"),
                                      ("wallet_usdt", "1000", "wallet"),
                                      ("total_equity_usd", "1009.8", "equity"),
                                      ("account_mm_rate", "0", "inconsistent supplied margin")):
            data = fixture()
            data["events"][1]["margin"][field] = value
            self.assert_invalid(data, message)

    def test_usdt_is_not_assumed_equal_to_usd(self):
        data = fixture()
        for event in data["events"]:
            event["margin"]["usdt_usd_index"] = "0.97"
            event["margin"]["total_equity_usd"] = str(Decimal(event["margin"]["total_equity_usd"]) * Decimal("0.97"))
        self.assertEqual(self.run_case(data)["status"], "PASS_OFFLINE_ACCOUNTING_ONLY")
        data["events"][0]["margin"]["total_equity_usd"] = "1000"
        self.assert_invalid(data, "equity")

    def test_margin_exit_and_borrowing_remain_latched_after_recovery(self):
        for field, value in (("account_mm_rate", "0.8"), ("liabilities_usdt", "1")):
            data = fixture()
            data["events"][2]["margin"][field] = value
            result = self.run_case(data)
            self.assertEqual(result["status"], "RISK_REJECTED_OFFLINE", result)
            self.assertTrue(result["exit_review_latched"])
            self.assertTrue(result["checkpoints"][-1]["exit_review_latched"])
        data = fixture()
        data["events"][2]["margin"]["account_im_rate"] = "0.5"
        self.assertEqual(self.run_case(data)["status"], "RISK_REJECTED_OFFLINE")

    def test_drawdown_exit_and_recovery(self):
        data = fixture()
        event = data["events"][2]
        event["valuation"][CALL]["mark"] = "31000"
        event["margin"]["total_equity_usd"] = "699.6"
        result = self.run_case(data)
        self.assertEqual(result["status"], "RISK_REJECTED_OFFLINE", result)
        self.assertTrue(result["exit_review_latched"])
        self.assertEqual(Decimal(result["max_drawdown_observed_checkpoints_only"]), Decimal("0.3004"))

    def test_gap_missing_mark_open_lifecycle_and_exit_depth(self):
        data = fixture()
        data["illustrative_limits"]["checkpoint_max_gap_ms"] = 4000
        self.assertIn("CHECKPOINT_GAP", self.run_case(data)["missing_evidence"])
        data = fixture()
        data["events"][2]["valuation"].pop(CALL)
        self.assert_invalid(data, "valuation")
        data = fixture()
        data["events"][2]["valuation"][CALL]["ask_size"] = "0.001"
        self.assertIn("EXIT_BBO_DEPTH_INSUFFICIENT", self.run_case(data)["missing_evidence"])
        data = fixture()
        data["events"] = data["events"][:6]
        data["end_ts_ms"] = START + 5000
        self.assertIn("OPEN_LIFECYCLE_AT_END", self.run_case(data)["missing_evidence"])

    def test_delivery_amount_prediction_and_expiry_metadata(self):
        for field, value, message in (("price_kind", "predicted", "predicted delivery"),
                                      ("cash_delta_before_fee_usdt", "10", "delivery cashflow")):
            data = fixture()
            data["events"][6][field] = value
            self.assert_invalid(data, message)
        data = fixture()
        data["instruments"][CALL]["expiry_ts_ms"] += 86400000
        self.assert_invalid(data, "expiry symbol/date")
        data = fixture()
        data["instruments"][CALL]["settle_coin"] = "USDC"
        self.assert_invalid(data, "units mismatch")
        data = fixture()
        data["events"][7]["delivery_price"] = "81001"
        self.assert_invalid(data, "inconsistent BTC delivery price")

    def test_long_put_delivery_uses_intrinsic_and_signed_position(self):
        data = fixture()
        data["instruments"] = {PUT: data["instruments"][PUT]}
        data["funding_schedule"] = []
        data["illustrative_limits"]["checkpoint_max_gap_ms"] = 10000
        data["events"] = [checkpoint(0, "MARK", 0, "1000", "1000", {}, {}),
            checkpoint(1, "FILL", 1000, "989.8", "999.8", {PUT: "0.01"},
                       {PUT: bbo(START + 1000, 990, 1000, 1000)}, symbol=PUT,
                       signed_qty_btc="0.01", price_usdt_per_btc="1000", fee_usdt="0.2",
                       execution_quote=bbo(START + 1000, 990, 1000)),
            checkpoint(2, "DELIVERY", 10000, "1009.7", "1009.7", {}, {}, symbol=PUT,
                       price_kind="official_delivery", delivery_price="78000",
                       cash_delta_before_fee_usdt="20", fee_usdt="0.1")]
        result = self.run_case(data)
        self.assertEqual(result["status"], "PASS_OFFLINE_ACCOUNTING_ONLY", result)
        self.assertEqual(Decimal(result["final_nav_usdt"]), Decimal("1009.7"))

    def test_nonfinite_boolean_and_unbounded_values_rejected(self):
        for bad in ("NaN", "Infinity", "1e1000", "1e-1000", "", True, 1000.0):
            data = fixture()
            data["simulation_capital_usdt"] = bad
            self.assertEqual(self.run_case(data)["status"], "TECHNICALLY_INVALID")

    def test_json_duplicate_keys_and_byte_budget(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON"):
            json.loads('{"a":1,"a":2}', object_pairs_hook=ledger.unique_object)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input.json"
            path.write_bytes(b" " * (ledger.MAX_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "byte budget"):
                ledger.read_input(path)

    def test_unknown_cost_or_flow_fields_cannot_be_silently_ignored(self):
        data = fixture()
        data["events"][1]["extra_fee_usdt"] = "20"
        self.assert_invalid(data, "unknown fields")
        data = fixture()
        data["unrecorded_transfer_usdt"] = "100"
        self.assert_invalid(data, "unknown fields")

    def test_offline_cli_hashes_and_scope_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "input.json"
            path.write_text(json.dumps(fixture()))
            command = [sys.executable, str(ROOT / "tools/audit_option_subaccount_ledger.py"),
                       "--scope", str(ROOT / "config/option_subaccount_research_scope_v1.json"),
                       "--input", str(path)]
            run = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            report = json.loads(run.stdout)
            self.assertEqual(len(report["input_sha256"]), 64)
            self.assertEqual(len(report["engine_sha256"]), 64)
            self.assertFalse(any(report["authorities"].values()))
            data = fixture()
            data["events"][1]["type"] = "TRANSFER_IN"
            path.write_text(json.dumps(data))
            run = subprocess.run(command, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 2)


if __name__ == "__main__":
    unittest.main()
