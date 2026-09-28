#!/usr/bin/env python3
import copy
import json
import unittest
import option_flow_probe as p

class ProbeTests(unittest.TestCase):
    def row(self):
        return dict(trade_id='123',timestamp=100,instrument_name='BTC-3AUG26-100000-C',direction='sell',amount=1)

    def payload(self, rows=None, more=False):
        return json.dumps(dict(jsonrpc='2.0',result=dict(trades=[self.row()] if rows is None else rows,has_more=more))).encode()

    def test_expiry(self):
        self.assertEqual(p.option(self.row()['instrument_name']), (p.ms('2026-08-03T08:00:00Z'),'C'))

    def test_noninverse(self):
        for name in ('BTC_USDC-3AUG26-100000-C','BTC-PERPETUAL','ETH-3AUG26-1000-C','BTC-3AUG26-0-C'):
            with self.assertRaises(ValueError): p.option(name)

    def test_good(self):
        self.assertEqual(p.validate_trades(self.payload(),100,100),([self.row()],False))

    def test_bad_fields(self):
        for key,val in (('timestamp',101),('direction','maker'),('amount',0),('amount',True),('amount','nan'),('trade_id',123)):
            row=self.row();row[key]=val
            with self.assertRaises(ValueError): p.validate_trades(self.payload([row]),100,100)

    def test_duplicate(self):
        with self.assertRaises(ValueError): p.validate_trades(self.payload([self.row(),self.row()]),100,100)

    def test_more_type(self):
        with self.assertRaises(ValueError): p.validate_trades(self.payload(more=1),100,100)

    def test_empty_complete(self):
        self.assertEqual(p.validate_trades(self.payload([]),100,100),([],False))
        with self.assertRaises(ValueError): p.validate_trades(self.payload([],True),100,100)

    def test_split_no_gaps(self):
        for end in range(1,100):
            intervals=p.split(0,end)
            self.assertEqual([t for a,b in intervals for t in range(a,b+1)],list(range(end+1)))
        with self.assertRaises(ValueError): p.split(1,1)

    def test_split_same_ms_preserved(self):
        row=self.row();other=copy.deepcopy(row);other['trade_id']='124'
        class Fake:
            def get(self,key,url):
                qs=p.urllib.parse.parse_qs(p.urllib.parse.urlsplit(url).query)
                a,b=int(qs['start_timestamp'][0]),int(qs['end_timestamp'][0])
                rows=[x for x in [row,other] if a<=x['timestamp']<=b]
                return json.dumps(dict(jsonrpc='2.0',result=dict(trades=rows,has_more=(b-a)>1))).encode()
        self.assertEqual(len(p.complete_trades(Fake(),99,102)),2)

    def test_urls(self):
        u=p.trade_url(100,101)
        self.assertIn('https://history.deribit.com/api/v2/public/',u)
        self.assertIn('include_old=true',u)
        self.assertEqual(len(p.OLD),10)

if __name__ == '__main__': unittest.main()
