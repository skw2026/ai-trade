#!/usr/bin/env python3
"""Synthetic-only tests; no market requests, returns or parameter selection."""
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import screen_token_unlock as m


def fixture(funding=False):
    c=m.contract();c["last_month"]="2025-01"
    c["data_end"]="2025-02-09T01:00:00Z"
    s=m.schedule(c);a,b=s[0]["entry"],s[-1]["end"]
    bars={t:(100.,100.,100.,100.) for t in range(a,b+m.STEP,m.STEP)}
    rates={t:.001 for t in range((a+8*m.HOUR-1)//(8*m.HOUR)*8*m.HOUR,b,8*m.HOUR)} if funding else {}
    return c,dict(trade=dict(bars),mark=dict(bars),funding=rates),s


def page(rows,kind="trade",symbol="ARBUSDT"):
    return json.dumps(dict(retCode=0,result=dict(category="linear",symbol=symbol,list=rows)))


class UnlockTest(unittest.TestCase):
    def test_scope_no_actuation(self):
        c=m.contract();self.assertFalse(any(c["authority"].values()))
        self.assertEqual(c["budget"]["experiments_for_this_mechanism"],1)
        self.assertEqual(c["budget"]["batch_market_gets"],240)

    def test_schedule_complete_months_and_year_rollover(self):
        s=m.schedule(m.contract());self.assertEqual(len(s),20)
        self.assertEqual(s[0]["entry"],m.ms("2025-01-09T00:30:00Z"))
        self.assertEqual(s[-1]["end"],m.ms("2026-09-09T00:30:00Z"))
        self.assertEqual(s[11]["end"],m.ms("2026-01-09T00:30:00Z"))
        for x in s:self.assertEqual(x["exit"]-x["entry"],8*m.DAY)

    def test_calendar_coverage_refused(self):
        c=m.contract();c["data_end"]="2026-08-01T00:00:00Z"
        with self.assertRaisesRegex(ValueError,"CALENDAR"):m.schedule(c)

    def test_public_requests_bounded_funding_not_truncated(self):
        p=m.request_plan(m.contract());self.assertLessEqual(len(p),240)
        for r in p:
            self.assertTrue(r["url"].startswith("https://api.bybit.com/v5/market/"))
            self.assertIn("ARBUSDT",r["url"])
            if r["kind"]=="funding":self.assertLess((r["end"]-r["start"])/m.HOUR,200)

    def test_request_budget_refused(self):
        c=m.contract();c["budget"]["batch_market_gets"]=1
        with self.assertRaisesRegex(ValueError,"GET_BUDGET"):m.request_plan(c)

    def test_bar_page_identity_grid_and_missing(self):
        req=dict(kind="trade",start=0,end=2*m.STEP-1,limit=1000)
        rows=[[str(t),"100","101","99","100","1","100"] for t in (m.STEP,0)]
        self.assertEqual(m.validate_page(page(rows),req),rows)
        for bad in (rows[:1],rows[::-1],rows+rows[:1]):
            with self.assertRaises(ValueError):m.validate_page(page(bad),req)
        with self.assertRaisesRegex(ValueError,"SYMBOL"):m.validate_page(page(rows,symbol="BTCUSDT"),req)

    def test_bad_price_volume_and_nonfinite(self):
        req=dict(kind="trade",start=0,end=m.STEP-1,limit=1000)
        row=["0","100","101","99","100","1","100"]
        for i,v in ((1,"0"),(2,"99"),(3,"101"),(4,"nan"),(5,"-1")):
            bad=list(row);bad[i]=v
            with self.assertRaises(ValueError):m.validate_page(page([bad]),req)

    def test_funding_rows_no_truncation_collision_or_wrong_symbol(self):
        req=dict(kind="funding",start=0,end=8*m.HOUR-1,limit=200)
        rows=[dict(symbol="ARBUSDT",fundingRateTimestamp="0",fundingRate=".001")]
        self.assertEqual(m.validate_page(page(rows),req),rows)
        for bad in ([],rows*200,[{**rows[0],"symbol":"BTCUSDT"}],[{**rows[0],"fundingRateTimestamp":str(m.STEP)}]):
            with self.assertRaises(ValueError):m.validate_page(page(bad),req)

    def test_funding_gap_and_edges(self):
        c=m.contract();c["data_start"]="2025-01-01T00:00:00Z";c["data_end"]="2025-01-02T00:00:00Z"
        a=m.ms(c["data_start"]);r={a+i*m.HOUR:.001 for i in (0,8,12,16,20,22,23)}
        m.funding_coverage(c,r)
        for bad in ({a:0},{a+8*m.HOUR:0,a+16*m.HOUR:0},{a:0,a+16*m.HOUR:0}):
            with self.assertRaises(ValueError):m.funding_coverage(c,bad)

    def test_flat_short_two_sided_cost_and_independent_cash(self):
        args=fixture();r=m.simulate(*args)
        q=.25/(100*.999)
        expected=-q*(100*1.001-100*.999)-q*(100*1.001+100*.999)*.00055
        self.assertAlmostEqual(r["candidate"]["cash_lo"]-1,expected)
        self.assertEqual(r["candidate"]["q"],0)
        self.assertEqual(r["control"]["cash_lo"],1)
        self.assertEqual(m.audit_cash(r,*args)["status"],"PASS")

    def test_short_down_move_positive_gross(self):
        c,d,s=fixture();x=s[0]["exit"]
        for k in d["trade"]:
            if k>=x:d["trade"][k]=d["mark"][k]=(90.,90.,90.,90.)
        r=m.simulate(c,d,s)
        self.assertGreater(r["candidate"]["gross_after_slippage"],0)
        self.assertEqual(m.audit_cash(r,c,d,s)["status"],"PASS")

    def test_funding_short_sign_and_bounds(self):
        for sign in (1,-1):
            c,d,s=fixture(True);d["funding"]={k:v*sign for k,v in d["funding"].items()}
            for t in d["funding"]:d["mark"][t]=(100.,101.,99.,100.)
            r=m.simulate(c,d,s);v=r["candidate"]
            self.assertLess(v["funding_lo"],v["funding_hi"])
            self.assertEqual(v["funding_lo"]>0,sign==1)
            self.assertEqual(m.audit_cash(r,c,d,s)["status"],"PASS")

    def test_no_funding_execution_collision(self):
        c,d,s=fixture();d["funding"][s[0]["entry"]]=.001
        with self.assertRaisesRegex(ValueError,"COLLISION"):m.simulate(c,d,s)

    def test_no_future_signal_or_entry_close(self):
        c,d,s=fixture();r=m.simulate(c,d,s)
        d["trade"][s[0]["entry"]]=(100.,300.,1.,3.)
        self.assertEqual(r["candidate"]["events"],m.simulate(c,d,s)["candidate"]["events"])

    def test_coin_time_control_not_beta_claim(self):
        r=m.simulate(*fixture())
        self.assertAlmostEqual(r["planned_coin_ms"],r["planned_control_coin_ms"])
        self.assertEqual(r["control"]["fees"],0)
        self.assertFalse(r["promotion_authority"])

    def test_short_up_gap_stops_without_future_or_fake_exit(self):
        c,d,s=fixture();t=s[0]["entry"]+m.STEP
        for k in ("trade","mark"):
            d[k]={a:b for a,b in d[k].items() if a<=t};d[k][t]=(140.,140.,140.,140.)
        r=m.simulate(c,d,s);self.assertEqual(r["decision"],"REJECT")
        self.assertLess(r["candidate"]["q"],0)
        self.assertFalse(r["stop_fill_simulated"])
        self.assertEqual(m.statistics(r,c),r)
        self.assertEqual(m.audit_cash(r,c,d,s)["status"],"PASS")
        self.assertEqual(m.audit_statistics(r,c)["status"],"NOT_APPLICABLE_RISK_STOP")

    def test_short_intrabar_peak_order_uncertainty(self):
        c,d,s=fixture();d["mark"][s[0]["entry"]]=(100.,110.,60.,100.)
        r=m.simulate(c,d,s)
        self.assertEqual(r["reason"],"INTRABAR_OR_FUNDING_RISK_AMBIGUITY")
        self.assertLess(r["dd_lo"],.08);self.assertGreaterEqual(r["dd_hi"],.08)

    def test_timeout_before_strategy(self):
        with self.assertRaisesRegex(ValueError,"COMPUTE_BUDGET"):m.simulate(*fixture(),deadline=0)

    def test_audit_refuses_missing_events_and_tampering(self):
        args=fixture(True);r=m.simulate(*args)
        for key in ("cash_lo","fees","funding_hi"):
            bad=copy.deepcopy(r);bad["candidate"][key]+=.01
            with self.assertRaisesRegex(ValueError,"AUDIT"):m.audit_cash(bad,*args)
        bad=copy.deepcopy(r);bad["candidate"]["events"].pop()
        with self.assertRaisesRegex(ValueError,"CALENDAR"):m.audit_cash(bad,*args)

    def test_statistics_all_outcomes_and_independent_implementation(self):
        c=m.contract();c["statistics"]["replicates"]=50
        for net,timing,decision in ((.001,.0005,"WORTH_FURTHER_REVIEW"),(-.001,-.0005,"REJECT"),(.001,-.001,"INSUFFICIENT_EVIDENCE")):
            rows=[dict(net_lo=net,net_hi=net,timing_lo=timing,timing_hi=timing) for _ in range(20)]
            r=dict(full_window_evaluated=True,cycles=rows,candidate=dict(cash_lo=1+20*net,cash_hi=1+20*net))
            out=m.statistics(r,c);self.assertEqual(out["decision"],decision)
            self.assertEqual(m.statistics(r,c),out)
            self.assertEqual(m.audit_statistics(out,c)["status"],"PASS")
            bad=copy.deepcopy(out);bad["bootstrap"][0]["net_lo"]+=.01
            with self.assertRaisesRegex(ValueError,"BOOTSTRAP"):m.audit_statistics(bad,c)

    def test_minimum_cycles_not_weakened(self):
        c=m.contract();r=dict(full_window_evaluated=True,cycles=[])
        self.assertEqual(m.statistics(r,c)["reason"],"CYCLE_COUNT")

    def test_strict_parse_and_immutable_marker(self):
        with self.assertRaises(ValueError):m.strict('{"a":1,"a":2}')
        with self.assertRaises(ValueError):m.number(math.inf)
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"start.json";m.save_new(p,dict(experiments=1))
            with self.assertRaises(FileExistsError):m.save_new(p,dict(experiments=2))


if __name__=="__main__":unittest.main()
