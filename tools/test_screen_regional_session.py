#!/usr/bin/env python3
"""Synthetic tests only. Never fetch market data or choose a profitable rule."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import screen_regional_session as m


def fixture(funding=False):
    c = m.contract()
    c["final_cycle_boundary_utc"] = "2026-04-02T06:35:00Z"
    s = m.cycles(c)
    a, b = s[0]["entry"], s[-1]["end"]
    trade = {t: (100.,100.,100.,100.) for t in range(a,b+m.STEP,m.STEP)}
    mark = dict(trade)
    rates = {t: .001 for t in range((a+8*m.HOUR-1)//(8*m.HOUR)*8*m.HOUR,b,8*m.HOUR)} if funding else {}
    return c, trade, mark, rates, s


def response(rows, kind="trade", symbol="BTCUSDT"):
    return json.dumps({"retCode":0,"result":{"category":"linear","symbol":symbol,"list":rows}}).encode()


class RegionalScreenTest(unittest.TestCase):
    def test_contract_scope_and_one_shot_limits(self):
        c = m.contract()
        self.assertEqual(c["budget"]["maximum_public_gets"],120)
        self.assertEqual(c["budget"]["market_experiments"],1)
        self.assertFalse(any(c["authority"].values()))
        with patch.object(m, "CONTRACT_SHA", "bad"):
            with self.assertRaisesRegex(ValueError,"FROZEN_CONTRACT"):
                m.contract()

    def test_calendar_delay_lunch_weekend_holiday(self):
        c = m.contract()
        c["first_entry_utc"]="2026-09-17T06:35:00Z"
        c["final_cycle_boundary_utc"]="2026-09-25T06:35:00Z"
        s=m.cycles(c)
        self.assertEqual(len(s),3)
        self.assertEqual(s[1]["entry"],m.ms("2026-09-18T06:35:00Z"))
        self.assertEqual(s[1]["exit"],m.ms("2026-09-24T00:05:00Z"))
        self.assertEqual(s[1]["end"],m.ms("2026-09-24T06:35:00Z"))
        self.assertAlmostEqual(s[1]["rho"],137.5/144)
        self.assertTrue(all(0<x["rho"]<1 and x["entry"]<x["exit"]<x["end"] for x in s))

    def test_invalid_final_calendar_boundary_refused(self):
        c=m.contract();c["final_cycle_boundary_utc"]="2026-09-27T06:35:00Z"
        with self.assertRaisesRegex(ValueError,"CALENDAR_BOUNDARIES"):
            m.cycles(c)

    def test_request_plan_is_public_exact_and_under_budget(self):
        c=m.contract();reqs=m.requests(c)
        self.assertLessEqual(len(reqs),120)
        self.assertEqual(reqs[0]["kind"],"funding")
        for r in reqs:
            self.assertTrue(r["url"].startswith("https://api.bybit.com/v5/market/"))
            self.assertIn("symbol=BTCUSDT",r["url"])
            self.assertNotIn("api_key",r["url"])
        c["budget"]["maximum_public_gets"]=1
        with self.assertRaisesRegex(ValueError,"REQUEST_PLAN_OVER_BUDGET"):
            m.requests(c)

    def test_page_grid_missing_duplicates_wrong_venue_or_symbol(self):
        req={"kind":"trade","start":0,"end":2*m.STEP-1,"step":m.STEP,"limit":1000}
        rows=[[str(t),"100","110","90","100","1","100"] for t in (m.STEP,0)]
        self.assertEqual(m.validate_page(response(rows),req),rows)
        for bad in (rows[:1],rows+rows[:1],rows[::-1]):
            with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,"GRID"):
                m.validate_page(response(bad),req)
        with self.assertRaisesRegex(ValueError,"SYMBOL"):
            m.validate_page(response(rows,symbol="ETHUSDT"),req)
        raw=json.loads(response(rows));raw["result"]["category"]="spot"
        with self.assertRaisesRegex(ValueError,"CATEGORY"):
            m.validate_page(json.dumps(raw),req)

    def test_ohlc_and_volume_are_validated_not_silently_repaired(self):
        req={"kind":"trade","start":0,"end":m.STEP-1,"step":m.STEP,"limit":1000}
        row=["0","100","110","90","100","1","100"]
        for idx,value in ((1,"0"),(2,"99"),(3,"101"),(4,"NaN"),(5,"-1"),(6,"-1")):
            bad=list(row);bad[idx]=value
            with self.subTest(idx=idx),self.assertRaises(ValueError):
                m.validate_page(response([bad]),req)

    def test_funding_missing_grid_nan_and_wrong_symbol_refused(self):
        req={"kind":"funding","start":0,"end":16*m.HOUR-1,"step":8*m.HOUR,"limit":200}
        rows=[{"symbol":"BTCUSDT","fundingRateTimestamp":str(t),"fundingRate":".0001"}
              for t in (8*m.HOUR,0)]
        self.assertEqual(len(m.validate_page(response(rows),req)),2)
        for bad in (rows[:1],[{**rows[0],"symbol":"ETHUSDT"},rows[1]],
                    [{**rows[0],"fundingRate":"NaN"},rows[1]]):
            with self.assertRaises(ValueError):m.validate_page(response(bad),req)

    def test_strict_json_and_numeric_refusals(self):
        for raw in ('{"a":1,"a":2}','{"a":NaN}'):
            with self.assertRaises(ValueError):m.strict(raw)
        for value in (True,math.inf,math.nan):
            with self.assertRaises(ValueError):m.number(value)
        for value in (1.5,True,"1e3"):
            with self.assertRaises(ValueError):m.stamp(value)

    def test_append_only_marker_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"economic-start.json"
            m.save_new(p,{"runs":1})
            with self.assertRaises(FileExistsError):m.save_new(p,{"runs":2})
            self.assertEqual(m.strict(p.read_bytes()),{"runs":1})

    def test_tls_verification_and_redirect_refusal(self):
        with patch.object(m.urllib.request,"build_opener") as build:
            m.transport()
            handlers=build.call_args.args
            https=next(h for h in handlers if isinstance(h,m.urllib.request.HTTPSHandler))
            self.assertTrue(https._context.check_hostname)
            self.assertEqual(https._context.verify_mode,m.ssl.CERT_REQUIRED)
            proxy=next(h for h in handlers if isinstance(h,m.urllib.request.ProxyHandler))
            self.assertEqual(proxy.proxies,{})
        with self.assertRaisesRegex(ValueError,"REDIRECT"):
            m.NoRedirect().redirect_request(None,None,None,None,None,None)

    def test_flat_price_double_fees_slippage_terminal_exit_and_cash_audit(self):
        args=fixture();r=m.simulate(*args)
        q=.25/(100*1.0002)
        expected=q*(100*.9998-100*1.0002)-q*(100*.9998+100*1.0002)*.00055
        self.assertAlmostEqual(r["candidate"]["cash_lo"]-1,expected)
        self.assertEqual(r["candidate"]["q"],0)
        self.assertEqual(r["control"]["cash_lo"],1)
        self.assertEqual(r["completed_cycles"],1)
        self.assertEqual(len(r["candidate"]["events"]),2)
        self.assertAlmostEqual(sum(d["net_lo"] for d in r["daily"]),expected)
        self.assertEqual(m.audit_cash(r,*args)["cash_reconstruction"],"PASS")

    def test_funding_sign_and_interval_and_independent_audit(self):
        for rate in (.001,-.001):
            c,trade,mark,funding,s=fixture(True)
            funding={t:rate for t in funding}
            for t in funding:mark[t]=(100.,102.,98.,100.)
            r=m.simulate(c,trade,mark,funding,s)
            self.assertTrue(r["full_window_evaluated"])
            cash=r["candidate"]
            self.assertLess(cash["funding_lo"],cash["funding_hi"])
            self.assertEqual(cash["funding_hi"]<0,rate>0)
            q=cash["events"][0]["q"]
            self.assertAlmostEqual(cash["funding_lo"],min(-q*rate*98,-q*rate*102)*3)
            self.assertEqual(m.audit_cash(r,c,trade,mark,funding,s)["cash_reconstruction"],"PASS")

    def test_one_bar_delay_does_not_use_entry_close_or_future_signals(self):
        c,trade,mark,funding,s=fixture()
        a=s[0]["entry"]
        r1=m.simulate(c,trade,mark,funding,s)
        trade[a]=(100.,200.,1.,2.)
        r2=m.simulate(c,trade,mark,funding,s)
        self.assertEqual(r1["candidate"]["events"],r2["candidate"]["events"])

    def test_control_matches_quantity_time_not_a_second_selected_strategy(self):
        r=m.simulate(*fixture())
        self.assertAlmostEqual(r["planned_candidate_coin_ms"],r["planned_control_coin_ms"])
        self.assertEqual(r["control"]["fees"],0)
        self.assertFalse(r["promotion_authority"])
        self.assertFalse(r["profitability_qualified"])

    def test_funding_at_execution_is_refused(self):
        c,trade,mark,funding,s=fixture()
        funding[s[0]["entry"]]=.0001
        with self.assertRaisesRegex(ValueError,"FUNDING_EXECUTION"):
            m.simulate(c,trade,mark,funding,s)

    def test_definite_risk_stops_without_future_prices_or_fake_exit(self):
        c,trade,mark,funding,s=fixture()
        t=s[0]["entry"]+m.STEP
        mark[t]=(40.,40.,40.,40.)
        trade={k:v for k,v in trade.items() if k<=t};mark={k:v for k,v in mark.items() if k<=t}
        r=m.simulate(c,trade,mark,funding,s)
        self.assertEqual(r["decision"],"REJECT")
        self.assertFalse(r["full_window_evaluated"])
        self.assertGreater(r["candidate"]["q"],0)
        self.assertEqual(len(r["candidate"]["events"]),1)
        self.assertFalse(r["stop_fill_simulated"])
        self.assertEqual(m.statistics(r,c),r)
        self.assertEqual(m.audit_cash(r,c,trade,mark,funding,s)["cash_reconstruction"],"PASS")

    def test_intrabar_high_low_order_is_insufficient_not_invented_risk_path(self):
        c,trade,mark,funding,s=fixture()
        mark[s[0]["entry"]]=(100.,150.,99.,100.)
        r=m.simulate(c,trade,mark,funding,s)
        self.assertEqual(r["decision"],"INSUFFICIENT_EVIDENCE")
        self.assertEqual(r["reason"],"INTRABAR_OR_FUNDING_RISK_AMBIGUITY")
        self.assertLess(r["drawdown_lower"],.08)
        self.assertGreaterEqual(r["drawdown_upper"],.08)

    def test_expired_compute_budget_stops_before_iteration(self):
        with self.assertRaisesRegex(ValueError,"COMPUTE_DEADLINE"):
            m.simulate(*fixture(),deadline=0)

    def test_independent_audit_detects_cash_fee_and_funding_tampering(self):
        args=fixture(True);report=m.simulate(*args)
        for field in ("cash_lo","fees","funding_hi"):
            r=copy.deepcopy(report);r["candidate"][field]+=.01
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,"AUDIT"):
                m.audit_cash(r,*args)
        r=copy.deepcopy(report);r["candidate"]["funding_events"].pop()
        with self.assertRaisesRegex(ValueError,"AUDIT_FUNDING_COUNT"):m.audit_cash(r,*args)

    def test_joint_block_statistics_three_outcomes_and_determinism(self):
        c=m.contract();c["statistics"]["bootstrap_replicates"]=50
        for net,timing,expected in ((.001,.0005,"WORTH_FURTHER_REVIEW"),
                                    (-.001,-.0005,"REJECT"),(.001,-.0005,"INSUFFICIENT_EVIDENCE")):
            r={"full_window_evaluated":True,"completed_cycles":60,
               "daily":[{"net_lo":net,"net_hi":net,"timing_lo":timing,"timing_hi":timing} for _ in range(120)],
               "promotion_authority":False}
            got=m.statistics(r,c)
            self.assertEqual(got["decision"],expected)
            self.assertEqual(got,m.statistics(r,c))
            self.assertFalse(got["promotion_authority"])
            r["completed_cycles"]=59
            self.assertEqual(m.statistics(r,c)["reason"],"SAMPLE_FLOOR")


if __name__=="__main__":
    unittest.main()
