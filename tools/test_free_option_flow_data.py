#!/usr/bin/env python3
import gzip
import json
from pathlib import Path
import tempfile
import unittest
import free_option_flow_data as f


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'input.gz'
        self.start = f.ms('2023-01-01T00:00:00Z')*1000

    def csv(self, extra=()):
        rows = ['exchange,symbol,timestamp,local_timestamp,id,side,price,amount']
        for i, minute in enumerate((1,446,451,476,1439)):
            ts = self.start+minute*60000000
            rows.append(f'deribit,BTC-1JAN23-20000-C,{ts},{ts},{i},sell,0.001,1')
        rows.extend(extra)
        with gzip.open(self.path,'wb') as g:
            g.write(('\n'.join(rows)+'\n').encode())
        return self.path

    def test_calendar(self):
        self.assertEqual((len(f.DATES),f.DATES[0],f.DATES[-1]),(45,'2023-01-01','2026-09-01'))
        self.assertEqual(len({u for d in f.DATES for u in f.specs(d).values()}),180)

    def test_schema_and_window(self):
        r = f.options(self.csv(),'2023-01-01',True)
        self.assertTrue(r['quality'])
        self.assertEqual(r['expiring_calls'],1)
        self.assertEqual(r['calls'][0]['id'],'2')

    def test_late_excluded(self):
        ts,local = self.start+452*60000000,self.start+1439*60000000
        r=f.options(self.csv([f'deribit,BTC-1JAN23-20000-C,{ts},{local},9,sell,0.001,1']),'2023-01-01')
        self.assertEqual(r['expiring_calls'],1)
        self.assertEqual(r['late_calls_excluded'],1)

    def test_exact_duplicate(self):
        ts,local = self.start+451*60000000,self.start+1439*60000000
        r=f.options(self.csv([f'deribit,BTC-1JAN23-20000-C,{ts},{local},2,sell,0.001,1']),'2023-01-01')
        self.assertEqual(r['duplicates'],1)

    def test_conflict_rejected(self):
        ts,local = self.start+451*60000000,self.start+1439*60000000
        with self.assertRaisesRegex(ValueError,'CONFLICTING_DUPLICATE'):
            f.options(self.csv([f'deribit,BTC-1JAN23-20000-C,{ts},{local},2,buy,0.001,1']),'2023-01-01')

    def test_unknown_side_rejected(self):
        ts=self.start+1439*60000000
        with self.assertRaisesRegex(ValueError,'BTC_ID_SIDE'):
            f.options(self.csv([f'deribit,BTC-1JAN23-20000-C,{ts},{ts},9,unknown,0.001,1']),'2023-01-01')

    def test_nan_rejected(self):
        ts=self.start+1439*60000000
        with self.assertRaisesRegex(ValueError,'NONFINITE'):
            f.options(self.csv([f'deribit,BTC-1JAN23-20000-C,{ts},{ts},9,sell,0.001,NaN']),'2023-01-01')

    def test_disallowed_date(self):
        with self.assertRaisesRegex(ValueError,'UNAPPROVED_DATE'):
            f.specs('2023-01-02')

    def market(self,kind='trade'):
        start=self.start//1000
        rows=[[str(t),'100','101','99','100']+(['1','100'] if kind=='trade' else [])
              for t in reversed(range(start,start+f.DAY,f.STEP))]
        return dict(retCode=0,result=dict(category='linear',symbol='BTCUSDT',list=rows))

    def check_market(self,obj,kind='trade'):
        self.path.write_text(json.dumps(obj))
        return f.bybit(self.path,'2023-01-01',kind)

    def test_full_grid(self):
        self.assertEqual(len(self.check_market(self.market())),288)
        self.assertEqual(len(self.check_market(self.market('mark'),'mark')),288)

    def test_gap_rejected(self):
        obj=self.market(); obj['result']['list'].pop()
        with self.assertRaisesRegex(ValueError,'KLINE_FULL_GRID'):
            self.check_market(obj)

    def test_ohlc_rejected(self):
        obj=self.market(); obj['result']['list'][0][2]='98'
        with self.assertRaisesRegex(ValueError,'OHLC'):
            self.check_market(obj)

    def test_funding(self):
        rows=[dict(symbol='BTCUSDT',fundingRate='0.0001',fundingRateTimestamp=str(self.start//1000+h*3600000)) for h in (16,8,0)]
        obj=dict(retCode=0,result=dict(category='linear',list=rows))
        self.assertEqual(len(self.check_market(obj,'funding')),3)
        rows.pop(1)
        with self.assertRaisesRegex(ValueError,'FUNDING_COVERAGE'):
            self.check_market(obj,'funding')


if __name__ == '__main__':
    unittest.main()
