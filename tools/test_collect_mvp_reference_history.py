#!/usr/bin/env python3
"""Synthetic request/response checks; no HTTP requests."""
import json
import unittest
import collect_mvp_reference_history as c
from mvp_reference_inputs import load_contract,window


class CollectorTest(unittest.TestCase):
    def test_fixed_budget_and_contiguous_requests(self):
        contract=load_contract();start,_,end=window(contract);plan=c.requests(contract)
        self.assertEqual(len(plan),218)
        for kind,total in (("trade",105409),("mark",105409),("funding",1099)):
            rows=[r for r in plan if r["kind"]==kind];cursor=start;points=0
            for r in rows:
                self.assertEqual(r["start"],cursor);cursor=r["end"]+1
                points+=len(range(r["start"],cursor,r["step"]))
                self.assertTrue(r["url"].startswith("https://api.bybit.com/v5/market/"))
            self.assertEqual(cursor,end);self.assertEqual(points,total)

    def response(self,kind):
        r=next(r for r in c.requests(load_contract()) if r["kind"]==kind)
        r={**r,"end":r["start"]+2*r["step"]-1}
        times=list(range(r["start"],r["end"]+1,r["step"]))[::-1]
        rows=([{"symbol":"BTCUSDT","fundingRateTimestamp":str(t),"fundingRate":"0.0001"} for t in times]
              if kind=="funding" else [[str(t),"100","101","99","100"]+(["1","100"] if kind=="trade" else []) for t in times])
        return r,{"retCode":0,"result":{"category":"linear","symbol":"BTCUSDT","list":rows}}

    def test_valid_shapes(self):
        for kind in ("trade","mark","funding"):
            r,v=self.response(kind);self.assertEqual(c.validate_page(json.dumps(v),r),2)

    def test_missing_or_changed_grid_stops(self):
        r,v=self.response("funding");v["result"]["list"].pop()
        with self.assertRaisesRegex(ValueError,"GRID"):c.validate_page(json.dumps(v),r)

    def test_bad_ohlc_stops(self):
        r,v=self.response("mark");v["result"]["list"][0][2]="98"
        with self.assertRaisesRegex(ValueError,"OHLC"):c.validate_page(json.dumps(v),r)

    def test_api_error_stops(self):
        r,v=self.response("trade");v["retCode"]=10001
        with self.assertRaisesRegex(ValueError,"API_ERROR"):c.validate_page(json.dumps(v),r)

    def test_redirects_forbidden(self):
        self.assertIsNone(c.NoRedirect().redirect_request(None,None,302,None,None,"https://example.com"))


if __name__=="__main__":unittest.main()
