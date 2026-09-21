#!/usr/bin/env python3
"""Invented stopped-ledger fixtures only; no historical archive or strategy run.

The pipeline's input/identity verifier has separate integration tests. Here its
boundary is mocked so the independent reconciliation arithmetic is tested on
tiny manually specified observations, not by replaying a closed experiment.
"""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audit_mvp_reference_stop as audit


class StopLedgerAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = self.root / "run-plan.json"
        self.plan.write_text('{"synthetic_unit_fixture":true}\n')
        verify = mock.patch.object(audit.pipeline, "verify", return_value=({}, self.root))
        verify.start()
        self.addCleanup(verify.stop)
        execute = mock.patch.object(audit.pipeline, "execute",
                                    side_effect=AssertionError("diagnostic cannot execute strategy"))
        execute.start()
        self.addCleanup(execute.stop)

    def fixture(self, side=1, partial_exit=False):
        # Two initial fill parts, same order. Adverse execution costs 3 units.
        price = 100 + side * 0.1
        fee = 30 * price * 0.00055
        equity = 10000 - fee - 3
        self.fills = [dict(ts=300000, order="entry", fill=f"part-{i}",
                           qty=n, price=price, fee=n * price * 0.00055)
                      for i, n in enumerate((18, 12))]
        self.sides = [side, side]
        self.bars = [dict(ts=600000, equity=equity, funding_uncertainty=0,
                          drawdown_upper=(10000-equity)/10000)]
        self.reason = "INSUFFICIENT_ACCOUNTING_CONTROL_PATH"
        rows = [dict(timestamp=300000, open=100, mark_open=100, mark_close=100,
                     mark_high=100, mark_low=100, funding_rate_per_interval=0),
                dict(timestamp=600000, open=100, mark_open=100,
                     mark_close=70 if side > 0 else 130,
                     mark_high=100 if side > 0 else 130,
                     mark_low=70 if side > 0 else 100, funding_rate_per_interval=0.001)]
        if partial_exit:
            self.fills.append(dict(ts=600000, order="exit", fill="exit-part", qty=10,
                                   price=99, fee=10*99*0.00055))
            self.sides.append(-1)
            rows[1].update(mark_close=55, mark_low=55)
        with (self.root/"replay.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.write_log()

    def write_log(self):
        lines = []
        for fill, side in zip(self.fills, self.sides):
            lines += [f'FILL_APPLIED: fill_id={fill["fill"]}, direction={side},',
                      "REFERENCE_FILL_JSON " + json.dumps(fill)]
        lines += ["REFERENCE_BAR_JSON " + json.dumps(bar) for bar in self.bars]
        lines += ["REFERENCE_STOP_JSON " + json.dumps({"reason": self.reason})]
        (self.root/"base.log").write_text("\n".join(lines)+"\n")
        result = {"decision": "INSUFFICIENT", "receipts": [
            {"log_sha256": audit.pipeline.digest(self.root/"base.log")}]}
        (self.root/"result.json").write_text(json.dumps(result))

    def test_long_and_short_cash_funding_slippage_and_first_stop(self):
        for side in (1, -1):
            with self.subTest(side=side):
                self.fixture(side)
                result = audit.audit(self.plan)
                stop = result["stop"]
                fee = 30*(100+side*0.1)*0.00055
                self.assertEqual(result["matched_closed_bars"], 1)
                self.assertEqual(result["matched_fill_parts"], 2)
                self.assertEqual(result["distinct_filled_orders"], 1)
                self.assertEqual(stop["ts"], 900000)
                self.assertEqual(stop["phase"], "close_before_alpha")
                self.assertAlmostEqual(stop["equity"], 10000-fee-side*3-903)
                self.assertAlmostEqual(stop["funding_paid"], side*3)
                self.assertAlmostEqual(stop["fees"], fee)
                self.assertAlmostEqual(stop["slippage_and_adverse_tick_cost_on_observed_fills"], 3)
                self.assertAlmostEqual(stop["price_pnl_before_execution_costs_on_same_fill_path"], -900)
                self.assertGreaterEqual(stop["drawdown_upper"], 0.08)
                self.assertFalse(result["strategy_rerun"])
                self.assertFalse(result["annual_result_available"])
                self.assertFalse(result["stress_executed"])

    def test_partial_exit_releases_collateral_and_books_once(self):
        self.fixture(partial_exit=True)
        result = audit.audit(self.plan)
        stop = result["stop"]
        self.assertEqual(result["distinct_filled_orders"], 2)
        self.assertEqual(stop["qty"], 20)
        self.assertAlmostEqual(stop["realized_pnl_after_slippage_before_fees"], -11)
        self.assertAlmostEqual(stop["collateral"], (30*100.1/2-30*100.1*.00055-3)*2/3)
        self.assertAlmostEqual(stop["equity"], 10000-30*100.1*.00055-3-11-10*99*.00055-902)

    def test_bound_log_tampering_is_rejected(self):
        self.fixture()
        with (self.root/"base.log").open("a") as stream:
            stream.write("changed\n")
        with self.assertRaisesRegex(ValueError, "LOG_IDENTITY"):
            audit.audit(self.plan)

    def test_wrong_stop_is_not_reclassified(self):
        self.fixture()
        self.reason = "REJECT_REFERENCE_MAINTENANCE"
        self.write_log()
        with self.assertRaisesRegex(ValueError, "EXPECTED_ACCOUNTING_STOP"):
            audit.audit(self.plan)

    def test_missing_fill_direction_is_rejected(self):
        self.fixture()
        self.sides.pop()
        # Preserve both fill observations but omit one direction record.
        text = (self.root/"base.log").read_text().replace(
            "FILL_APPLIED: fill_id=part-1, direction=1,\n", "")
        (self.root/"base.log").write_text(text)
        (self.root/"result.json").write_text(json.dumps({"decision":"INSUFFICIENT", "receipts":[
            {"log_sha256":audit.pipeline.digest(self.root/"base.log")}]}))
        with self.assertRaisesRegex(ValueError, "DIRECTION_LOG_COVERAGE"):
            audit.audit(self.plan)

    def test_wrong_fee_formula_is_rejected(self):
        self.fixture()
        self.fills[0]["fee"] += 0.1
        self.write_log()
        with self.assertRaisesRegex(ValueError, "FEE_FORMULA"):
            audit.audit(self.plan)

    def test_wrong_observed_equity_is_rejected(self):
        self.fixture()
        self.bars[0]["equity"] += 1
        self.write_log()
        with self.assertRaisesRegex(ValueError, "OBSERVED_LEDGER_MISMATCH"):
            audit.audit(self.plan)

    def test_unreconciled_extra_bar_is_rejected(self):
        self.fixture()
        self.bars.append({**self.bars[0], "ts":900000})
        self.write_log()
        with self.assertRaisesRegex(ValueError, "PREFIX_RECONCILIATION_INCOMPLETE"):
            audit.audit(self.plan)


if __name__ == "__main__":
    unittest.main()
