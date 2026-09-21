#!/usr/bin/env python3
"""Synthetic accounting and refusal checks; no public data or strategy selection."""
import json
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

import screen_weekly_momentum as m


def fixture(weeks=1):
    first = m.WEEK + m.HOUR
    final = first + weeks*m.WEEK
    trade = {}
    for t in range(0, final+m.HOUR, m.HOUR):
        price = 100 if t < m.WEEK-m.HOUR else 110
        trade[t] = (price, price, price, price)
    mark = {t: (110, 110, 110, 110) for t in trade}
    return trade, mark, first, final


class ScreenTests(unittest.TestCase):
    def test_same_side_quantity_is_not_weekly_rebalanced(self):
        self.assertEqual(m.target_quantity(.01, 1, 200), .01)
        self.assertEqual(m.target_quantity(-.01, -1, 200), -.01)
        self.assertEqual(m.target_quantity(.01, -1, 200), -.005)
        self.assertEqual(m.target_quantity(.01, 0, 200), 0)
        self.assertEqual(abs(-.005-.01)*200, 3)  # Closing + opening notionals, not one side.

    def test_flat_price_round_trip_cash_and_final_exit_fee(self):
        trade, mark, first, final = fixture()
        r = m.simulate(trade, mark, {}, first, final)
        self.assertTrue(r["full_window_evaluated"])
        self.assertEqual(r["complete_week_count"], 1)
        self.assertAlmostEqual(r["nav_lower"], 1-.0011)
        self.assertAlmostEqual(r["turnover"], 2)
        self.assertAlmostEqual(r["weekly"][0]["lo"], -.0011)
        self.assertAlmostEqual(r["weekly"][0]["turnover"], 2)
        self.assertEqual(r["events"][-1]["new_q"], 0)

    def test_funding_sign_and_bound_in_all_quadrants(self):
        for q, rate, expected in ((1,.01,(-1.2,-1)),(-1,.01,(1,1.2)),
                                  (1,-.01,(1,1.2)),(-1,-.01,(-1.2,-1))):
            with self.subTest(q=q,rate=rate):
                lo, hi = m.funding_bounds(q,rate,100,120)
                self.assertAlmostEqual(lo,expected[0])
                self.assertAlmostEqual(hi,expected[1])

    def test_funding_updates_cash_not_nominal_principal(self):
        trade, mark, first, final = fixture()
        t = first+7*m.HOUR
        mark[t] = (110,120,100,110)
        r = m.simulate(trade,mark,{t:.01},first,final)
        self.assertAlmostEqual(r["nav_lower"],1-.0011-1.2/110)
        self.assertAlmostEqual(r["nav_upper"],1-.0011-1/110)

    def test_zero_signal_is_not_zero_activity_success(self):
        trade,mark,first,final = fixture()
        trade = {t:(110,110,110,110) for t in trade}
        r = m.classify(m.simulate(trade,mark,{},first,final),{})
        self.assertEqual(r["decision"],"INSUFFICIENT_EVIDENCE")
        self.assertEqual(r["reason"],"NO_ACTIVITY")

    def test_risk_breach_stops_before_unavailable_future_and_bootstrap(self):
        trade,mark,first,final = fixture(2)
        stop = first+10*m.HOUR
        mark = {t:v for t,v in mark.items() if t <= stop}
        mark[stop]=(80,80,80,80)
        r = m.simulate(trade,mark,{},first,final)
        self.assertEqual(r["decision"],"REJECT")
        self.assertFalse(r["full_window_evaluated"])
        self.assertEqual(r["complete_week_count"],0)
        self.assertGreaterEqual(r["drawdown_lower_bound_within_model"],.2)
        self.assertEqual(r["stopped_at"],m.utc(stop+m.HOUR))
        self.assertTrue(r["position_not_simulated_closed"])
        self.assertEqual(m.classify(r,{}),r)

    def test_execution_funding_boundary_is_refused(self):
        trade,mark,first,final = fixture()
        with self.assertRaisesRegex(ValueError,"ambiguous"):
            m.simulate(trade,mark,{first:.0001},first,final)

    def test_closed_past_signal_does_not_use_execution_hour_close(self):
        trade,mark,first,final = fixture()
        r1 = m.simulate(trade,mark,{},first,final)
        trade[first]=(110,1000,1,1)
        r2 = m.simulate(trade,mark,{},first,final)
        self.assertEqual(r1["events"][0],r2["events"][0])

    def test_ohlc_missing_duplicate_and_bad_bounds_refused(self):
        valid=[[0,100,110,90,105],[m.HOUR,105,115,100,110]]
        self.assertEqual(len(m.bars(valid,0,2*m.HOUR)),2)
        for rows in (valid[:1],valid+valid[:1],[[0,100,99,90,105],valid[1]],
                     [[0,100,float("nan"),90,105],valid[1]]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):
                m.bars(rows,0,2*m.HOUR)

    def test_funding_gap_not_silently_zero_filled(self):
        rows=[{"symbol":"BTCUSDT","fundingRateTimestamp":t,"fundingRate":"0.0001"}
              for t in (0,8*m.HOUR)]
        self.assertEqual(len(m.rates(rows,0,16*m.HOUR)),2)
        for bad in (rows[:1],rows+rows[:1],[{**rows[0],"symbol":"ETHUSDT"},rows[1]]):
            with self.subTest(rows=bad),self.assertRaises(ValueError):
                m.rates(bad,0,16*m.HOUR)

    def test_fee_only_negative_reference_is_rejected_not_actual_loss_claim(self):
        trade,mark,first,final=fixture()
        r=m.classify(m.simulate(trade,mark,{},first,final),{})
        self.assertEqual(r["reason"],"NONPOSITIVE_OPTIMISTIC_REFERENCE_RESULT")
        self.assertFalse(r["profitability_qualified"])
        self.assertIsNone(r["unknown_execution_cost_bps"])

    def test_bootstrap_is_deterministic_and_cannot_grant_trading(self):
        c={"sensitivity_fee_bps":11,"base_reference_fee_bps":5.5,"bootstrap_seed":20260921,
           "bootstrap_block_weeks":[4,8,13],"bootstrap_trials":100,"lower_percentile":.05}
        r={"turnover":2,"nav_upper":1.1,"weekly":[{"lo":.001,"turnover":.02} for _ in range(104)],
           "profitability_qualified":False}
        first=m.classify(r,c)
        self.assertEqual(first,m.classify(r,c))
        self.assertEqual(first["decision"],"WORTH_FURTHER_REVIEW")
        self.assertFalse(first["profitability_qualified"])

    def test_public_source_budget_and_endpoint_reject_before_network(self):
        c,sha=m.load_contract()
        with tempfile.TemporaryDirectory() as tmp:
            src=m.PublicSource(pathlib.Path(tmp),c,sha,time.monotonic()+60)
            with patch.object(src.opener,"open",side_effect=AssertionError("network forbidden")):
                with self.assertRaisesRegex(ValueError,"endpoint"):
                    src.get("/v5/order/create",{})
                src.manifest["attempted_gets"]=100
                with self.assertRaisesRegex(ValueError,"budget"):
                    src.get("/v5/market/kline",{})
            self.assertTrue(src.context.check_hostname)

    def test_cached_bytes_require_identity_and_do_not_consume_get(self):
        c,sha=m.load_contract()
        with tempfile.TemporaryDirectory() as tmp:
            src=m.PublicSource(pathlib.Path(tmp),c,sha,time.monotonic()+60)
            url=m.HOST+"/v5/market/kline?category=linear&symbol=BTCUSDT"
            key=m.digest(url.encode())
            raw=json.dumps({"retCode":0,"result":{"category":"linear","list":["fixture"]}}).encode()
            path=pathlib.Path(tmp)/(key+".raw.json")
            path.write_bytes(raw)
            src.manifest["responses"][key]={"url":url,"sha256":m.digest(raw)}
            self.assertEqual(src.get("/v5/market/kline",{}),["fixture"])
            self.assertEqual(src.manifest["attempted_gets"],0)
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError,"identity"):
                src.get("/v5/market/kline",{})


if __name__=="__main__":
    unittest.main()
