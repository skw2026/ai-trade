#!/usr/bin/env python3
"""Synthetic-only mathematical, causal, capture and transport regression."""
import copy
import datetime
from decimal import Decimal as D, localcontext
from fractions import Fraction as F
import gzip
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import forward_evidence as original
import audit_two_sided_quote_screen as audit
import two_sided_quote_screen as q
import two_sided_screen_remote as remote

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / (remote.PREFIX + ".contract.json")).read_text())


def independent_payoff(b, s, bp, sp, bid, ask, maker, taker, slip):
    # Rational, cash-account implementation independent of Decimal payoff.
    b, s, bp, sp, bid, ask, maker, taker, slip = map(F, (b, s, bp, sp, bid, ask, maker, taker, slip))
    receipts, payments = s * sp, b * bp
    fees = (payments + receipts) * maker
    if b > s:
        close = (b - s) * bid * (1 - slip)
        receipts += close
        fees += close * taker
    elif s > b:
        close = (s - b) * ask * (1 + slip)
        payments += close
        fees += close * taker
    return receipts - payments - fees


def trade(at=1000, side="Sell", price="99.9", volume="1", name="synthetic"):
    return {"s": "BTCUSDT", "T": at, "S": side, "p": price, "v": volume, "i": name, "BT": False, "RPI": False}


def model():
    return {"buy_price_ratio": "0.9995", "sell_price_ratio": "1.0005",
            "exit_bid_ratio": "0.9999", "exit_ask_ratio": "1.0001",
            "buy_cap_fraction": "0.3", "sell_cap_fraction": "0.9"}


def prepared():
    start = 1800000300000000000
    decisions = [start + 60 * 10**9, start + 360 * 10**9]
    snapshots = {}
    for t in decisions:
        for off in (0, 1000, 120000, 122000):
            snapshots[t + off * q.NS] = {s: (D("99.99"), D("100.01")) for s in CONTRACT["symbols"]}
    trades = {s: [(decisions[0] + 1001 * q.NS, "Sell", D("99.9"), D("0.3"), False),
                  (decisions[0] + 120500 * q.NS, "Buy", D("100.1"), D("0.7"), False)] for s in CONTRACT["symbols"]}
    return {"index": 0, "decisions": decisions, "snapshots": snapshots, "trades": trades}


def write_fixture(root, seconds=200):
    root = Path(root)
    directory = root / "segment-000"
    directory.mkdir(mode=0o700)
    raw = directory / "receive.jsonl.gz"
    state = original.Replay()
    epoch = 1800000300000000000
    with gzip.open(raw, "xt", encoding="utf-8") as handle:
        for second in range(seconds + 1):
            for symbol in CONTRACT["symbols"]:
                for book in (True, False):
                    if book:
                        payload = {"topic": "orderbook.50." + symbol, "type": "snapshot",
                                   "cts": epoch // q.NS + second * 1000, "ts": epoch // q.NS + second * 1000,
                                   "data": {"s": symbol, "u": second + 1, "seq": second + 1,
                                            "b": [["99.99", "20"]], "a": [["100.01", "20"]]}}
                    else:
                        tr = trade(at=(epoch // q.NS + second * 1000), name=symbol + str(second))
                        tr["s"] = symbol
                        tr["S"] = "Buy" if second % 2 else "Sell"
                        tr["p"] = "100.1" if second % 2 else "99.9"
                        payload = {"topic": "publicTrade." + symbol, "data": [tr]}
                    sequence = state.count + 1
                    receive = epoch + second * 10**9 + sequence
                    row = {"sequence": sequence, "receive_utc_ns": receive,
                           "receive_monotonic_ns": receive - epoch + 10**9, "wire": json.dumps(payload)}
                    handle.write(original.canonical(row) + "\n")
                    state.accept(row)
    receipt = {"schema_version": original.SCHEMA, "status": "SEALED", "segment_index": 0,
               "raw_sha256": q.file_hash(raw), "raw_bytes": raw.stat().st_size, "observation": state.summary(),
               "previous_receipt_sha256": None, "source_sha256": q.file_hash(ROOT / "tools/forward_evidence.py"),
               "economic_evidence": False}
    original.write_json(directory / "receipt.json", receipt)
    original.write_json(root / "started.json", {"synthetic": True})
    expected = {"index": 0, "raw_sha256": receipt["raw_sha256"], "raw_bytes": receipt["raw_bytes"],
                "receipt_sha256": q.file_hash(directory / "receipt.json"), "previous_receipt_sha256": None}
    manifest = {"segments": [expected], "started_sha256": q.file_hash(root / "started.json"),
                "source_hashes": {name: q.file_hash(ROOT / "tools" / name) for name in
                                  ("forward_evidence.py", "collect_bybit_microstructure.py")}}
    return manifest


class QuoteMath(unittest.TestCase):
    def test_partial_fill_diagonal_is_not_optional(self):
        corners = {(D(0), D(0)), (D(".3"), D(0)), (D(0), D(".9")), (D(".3"), D(".9"))}
        self.assertIn((D(".3"), D(".3")), q.vertices(D(".3"), D(".9")))
        args = (D(".99"), D("1.01"), D(".8"), D("1.2"), D(0), D(0), D(0))
        self.assertGreater(q.payoff(D(".3"), D(".3"), *args), max(q.payoff(b, s, *args) for b, s in corners))

    def test_cash_ledger_matches_independent_rational(self):
        for b in (D(0), D(".1"), D(".3"), D(1)):
            for s in (D(0), D(".2"), D(".3"), D(1)):
                args = (b, s, D(".9995"), D("1.0005"), D(".9999"), D("1.0001"),
                        D(".000275"), D(".00055"), D(".0001"))
                self.assertEqual(F(q.payoff(*args)), independent_payoff(*args))

    def test_grid_is_inside_piecewise_linear_extrema(self):
        m = model()
        low, high = q.bounds(m, CONTRACT)
        args = tuple(D(m[k]) for k in ("buy_price_ratio", "sell_price_ratio", "exit_bid_ratio", "exit_ask_ratio"))
        fees = tuple(D(CONTRACT[k]) / q.BPS for k in ("maker_fee_bps", "taker_fee_bps", "liquidation_slip_bps"))
        for b in range(31):
            for s in range(91):
                value = q.payoff(D(b) / 100, D(s) / 100, *args, *fees) * q.BPS
                self.assertLessEqual(low, value)
                self.assertGreaterEqual(high, value)

    def test_empty_fill_and_stress_monotone(self):
        low, high = q.bounds(model(), CONTRACT)
        sl, sh = q.bounds(model(), CONTRACT, True)
        self.assertLessEqual(low, 0)
        self.assertGreaterEqual(high, 0)
        self.assertLessEqual(sl, low)
        self.assertLessEqual(sh, high)

    def test_zero_capacity_is_zero(self):
        m = model()
        m.update(buy_cap_fraction="0", sell_cap_fraction="0")
        self.assertEqual(q.bounds(m, CONTRACT), (D(0), D(0)))

    def test_paired_not_min_minus_min(self):
        self.assertEqual(q.paired_outer((D(-2), D(10)), (D(-3), D(11))), (D(-13), D(13)))

    def test_invalid_caps(self):
        for b, s in ((D(-1), D(0)), (D(0), D(2))):
            with self.assertRaisesRegex(ValueError, "CAP_RANGE"):
                q.vertices(b, s)

    def test_adaptation_units_and_clamp(self):
        e = q.update((D(2), D(2)), D(-10), CONTRACT)
        self.assertEqual(e, (D("3.6"), D("1.6")))
        self.assertEqual(q.widths(e, CONTRACT), (D("6.6"), D(5)))
        self.assertEqual(q.widths((D(100), D(0)), CONTRACT), (D(25), D(5)))


class QuoteCausality(unittest.TestCase):
    def test_mature_label_only_changes_later_quote(self):
        p = prepared()
        t = p["decisions"][0]
        p["snapshots"][t + CONTRACT["label_ms"] * q.NS] = {s: (D("98.99"), D("99.01")) for s in CONTRACT["symbols"]}
        rows, counts, by_symbol = q.prepare_models(p, CONTRACT)
        first = [r for r in rows if r["cycle"] == 0]
        later = [r for r in rows if r["cycle"] == 1]
        self.assertTrue(all(r["arms"]["adaptive"]["model"]["buy_halfwidth_bps"] == "5" for r in first))
        self.assertTrue(all(D(r["arms"]["adaptive"]["model"]["buy_halfwidth_bps"]) > 5 for r in later))
        self.assertEqual(counts["mature_updates"], 3)
        self.assertEqual(by_symbol["BTCUSDT"]["mature_updates"], 1)

    def test_missing_label_does_not_update(self):
        p = prepared()
        p["snapshots"][p["decisions"][0] + CONTRACT["label_ms"] * q.NS] = {}
        _rows, counts, _ = q.prepare_models(p, CONTRACT)
        self.assertEqual(counts["mature_updates"], 0)
        self.assertEqual(counts["labels_unavailable"], 3)

    def test_segment_state_resets(self):
        p = prepared()
        a = q.prepare_models(p, CONTRACT)
        self.assertEqual(a, q.prepare_models(p, CONTRACT))

    def test_post_only_rejection(self):
        result = q.model_for_cycle(D(100), (D(5), D(5)), (D("99.8"), D("99.9")),
                                  (D("99.99"), D("100.01")), [(1000*q.NS, "Sell", D(99), D(10), False)], 0, CONTRACT)
        self.assertEqual(result["buy_cap_fraction"], "0")
        self.assertEqual(result["post_only_rejected_sides"], 1)

    def test_cancel_latency_and_halfopen_boundaries(self):
        events = [(ms*q.NS, "Sell", D(99), D(".1"), False) for ms in (999, 1000, 120500, 121000)]
        result = q.model_for_cycle(D(100), (D(5), D(5)), (D("99.99"), D("100.01")),
                                  (D("99.99"), D("100.01")), events, 0, CONTRACT)
        self.assertEqual(D(result["buy_cap_fraction"]), D(".2"))

    def test_block_rpi_wrong_side_do_not_create_capacity(self):
        events = [(1000*q.NS, "Sell", D(99), D(1), True), (1000*q.NS, "Buy", D(99), D(1), False)]
        result = q.model_for_cycle(D(100), (D(5), D(5)), (D("99.99"), D("100.01")),
                                  (D("99.99"), D("100.01")), events, 0, CONTRACT)
        self.assertEqual(D(result["buy_cap_fraction"]), 0)
        self.assertEqual(D(result["sell_cap_fraction"]), 0)

    def test_short_segment_not_retimed(self):
        self.assertEqual(q.decision_times(0, 65000*q.NS, CONTRACT), [])
        self.assertEqual(len(q.decision_times(0, 899000*q.NS, CONTRACT)), 3)

    def test_funding_guard_edges(self):
        hour = 3600000*q.NS
        self.assertTrue(q.funding_overlap(hour-130000*q.NS, hour-10000*q.NS, 10000))
        self.assertTrue(q.funding_overlap(hour+10000*q.NS, hour+130000*q.NS, 10000))
        self.assertFalse(q.funding_overlap(hour+10001*q.NS, hour+130000*q.NS, 10000))

    def test_missing_exit_is_coverage_insufficient_and_coin_retained(self):
        p = prepared()
        for t in p["decisions"]:
            p["snapshots"][t + CONTRACT["liquidate_ms"]*q.NS].pop("SOLUSDT")
        rows, counts, by_symbol = q.prepare_models(p, CONTRACT)
        summary = q.aggregate(q.evaluate(rows, CONTRACT), CONTRACT)
        self.assertEqual(summary["SOLUSDT"]["cycles"], 0)
        self.assertEqual(by_symbol["SOLUSDT"]["quote_excluded"], 2)
        self.assertEqual(q.adjudicate(summary, counts), "INSUFFICIENT_INPUT_COVERAGE")


class InputContract(unittest.TestCase):
    def test_trade_id_duplicates_rejected(self):
        seen = set()
        q.validated_trade(trade(), "BTCUSDT", 1000*q.NS, CONTRACT, seen)
        with self.assertRaisesRegex(ValueError, "DUPLICATE_TRADE_ID"):
            q.validated_trade(trade(), "BTCUSDT", 1000*q.NS, CONTRACT, seen)

    def test_nonfinite_or_negative_values_rejected(self):
        for value in ("NaN", "Infinity", "-1", "0"):
            with self.assertRaises(ValueError):
                q.number(value)

    def test_unknown_flag_and_side_rejected(self):
        for field, value in (("BT", None), ("RPI", 0), ("S", "unknown")):
            t = trade()
            t[field] = value
            with self.assertRaises(ValueError):
                q.validated_trade(t, "BTCUSDT", 1000*q.NS, CONTRACT, set())

    def test_delay_model_rejects_beyond_bounds(self):
        for received in (998, 3001):
            with self.assertRaisesRegex(ValueError, "TRADE_CLOCK_OR_DELAY_OUTSIDE_MODEL"):
                q.validated_trade(trade(), "BTCUSDT", received*q.NS, CONTRACT, set())

    def test_late_received_trade_preserves_matching_clock(self):
        row = q.validated_trade(trade(), "BTCUSDT", 2999*q.NS, CONTRACT, set())
        self.assertEqual(row[0], 1000*q.NS)

    def test_original_replay_and_complete_synthetic_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = write_fixture(tmp)
            c = dict(CONTRACT, segments=1)
            result = q.execute(c, manifest, tmp, ROOT / "tools")
            self.assertEqual(result["status"], "SCREEN_COMPLETED")
            self.assertEqual(result["replayed_segments"], 1)
            self.assertEqual(result["summary"]["ALL"]["cycles"], 3)
            self.assertFalse(result["profitability_proven"])
            self.assertEqual(result["verdict"], "INSUFFICIENT_EXECUTION_IDENTIFICATION")
            self.assertEqual(audit.audit(result, c)["audited_arm_cost_ledgers"], 12)

    def test_independent_audit_detects_ledger_and_verdict_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = write_fixture(tmp)
            c = dict(CONTRACT, segments=1)
            result = q.execute(c, m, tmp, ROOT / "tools")
            corrupt = copy.deepcopy(result)
            corrupt["normalized_ledger"][0]["arms"]["fixed"]["base_bps"][0] = "999"
            with self.assertRaisesRegex(ValueError, "INDEPENDENT_LEDGER_MISMATCH"):
                audit.audit(corrupt, c)
            result["verdict"] = "PROFITABLE"
            with self.assertRaisesRegex(ValueError, "VERDICT_SCOPE"):
                audit.audit(result, c)

    def test_hash_mismatch_never_evaluates_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = write_fixture(tmp, 3)
            manifest["segments"][0]["raw_sha256"] = "0"*64
            with patch.object(q, "evaluate") as evaluate, self.assertRaisesRegex(ValueError, "SEGMENT_HASH"):
                q.execute(dict(CONTRACT, segments=1), manifest, tmp, ROOT / "tools")
            evaluate.assert_not_called()

    def test_snapshot_is_asof_not_next_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = write_fixture(tmp)
            p = q.prepare_segment(Path(tmp)/"segment-000", m["segments"][0], CONTRACT, original, set())
            t = p["decisions"][0]
            self.assertEqual(p["snapshots"][t]["BTCUSDT"], (D("99.99"), D("100.01")))


class TransportContract(unittest.TestCase):
    def test_absolute_budget_cannot_be_reused_later(self):
        remote.budget_check(CONTRACT, datetime.datetime(2026, 10, 12, 4, 30, tzinfo=datetime.timezone.utc))
        for hour in (3, 8):
            with self.assertRaisesRegex(ValueError, "BATCH_BUDGET_CANNOT_FIT"):
                remote.budget_check(CONTRACT, datetime.datetime(2026, 10, 12, hour, 30, tzinfo=datetime.timezone.utc))

    def bundle(self):
        sources = {name: (ROOT / "tools" / name).read_text() for name in remote.SOURCES}
        manifest = json.loads((ROOT / (remote.PREFIX + ".manifest.json")).read_text())
        binding = {"workflow_sha": "a"*40, "run_id": "12345", "run_attempt": "1",
                   "contract_sha256": remote.digest(remote.canonical(CONTRACT)),
                   "manifest_sha256": remote.digest(remote.canonical(manifest)),
                   "source_hashes": {k: remote.digest(v.encode()) for k,v in sources.items()}}
        return {"contract": copy.deepcopy(CONTRACT), "manifest": manifest, "sources": sources, "binding": binding}

    def test_exact_bundle_and_env_size(self):
        b = self.bundle()
        remote.validate_binding(b)
        self.assertLess(len(remote.transport_script(b).encode()), 96*1024)

    def test_changed_manifest_source_and_second_attempt_rejected(self):
        for case in ("manifest", "source", "attempt"):
            b = self.bundle()
            if case == "manifest":
                b["manifest"]["segments"].pop()
            elif case == "source":
                b["sources"]["two_sided_quote_screen.py"] += "#changed"
            else:
                b["binding"]["run_attempt"] = "2"
            with self.assertRaises(ValueError):
                remote.validate_binding(b)

    def test_once_marker_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/"attempt.json"
            remote.exclusive(p, {"first": True})
            with self.assertRaises(FileExistsError):
                remote.exclusive(p, {"second": True})
            self.assertEqual(json.loads(p.read_text()), {"first": True})

    def test_two_exact_readonly_mounts_and_no_credentials(self):
        spec = remote.docker_spec(CONTRACT, Path("/synthetic/root"), "12345")
        mounts = [spec[i+1] for i, value in enumerate(spec) if value == "--mount"]
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(v.endswith(",readonly") for v in mounts))
        self.assertTrue(any("/capture," in v for v in mounts))
        self.assertNotIn("--privileged", spec)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--memory=512m", "--cpus=1", "--pull=never"):
            self.assertIn(flag, spec)
        self.assertEqual(spec[spec.index("--user")+1], "65534:65534")

    def test_error_does_not_echo_secret_text(self):
        self.assertEqual(remote.reason(ValueError("private token=synthetic")), "ValueError")

    def test_invalid_binding_never_touches_remote(self):
        b = self.bundle()
        b["binding"]["run_attempt"] = "2"
        with patch.object(remote.subprocess, "run") as command:
            result = remote.remote(b)
        self.assertEqual(result["status"], "INCOMPLETE")
        command.assert_not_called()

    def test_cleanup_refuses_foreign_container(self):
        fake = type("Result", (), {"returncode": 0, "stdout": b"different"})()
        with patch.object(remote.subprocess, "run", return_value=fake) as command:
            with self.assertRaisesRegex(ValueError, "CLEANUP_CONTAINER_IDENTITY"):
                remote.stop_own_container("12345")
        self.assertEqual(command.call_count, 1)


if __name__ == "__main__":
    unittest.main()
