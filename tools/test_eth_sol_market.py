import copy
import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

import eth_sol_market as m


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.c=json.loads(m.CONTRACT.read_text())
        self.r=dict(symbol='SOLUSDT',kind='trade',start=0,end=119999,limit=1000,interval_minutes=1)
        self.obj=dict(retCode=0,time=200000,result=dict(category='linear',symbol='SOLUSDT',
            list=[['60000','100','102','99','101','20','2000'],['0','100','102','99','101','20','2000']]))

    def raw(self):return json.dumps(self.obj).encode()

    def test_plan_budget_and_controls(self):
        p=m.plan(self.c)
        self.assertEqual(len(p),1113)
        self.assertLessEqual(len(p)+2+30,1500)
        self.assertEqual(len({r['url'] for r in p}),len(p))
        self.assertEqual([(r['symbol'],r['kind'],r['interval_minutes']) for r in p[:5]],
            [('SOLUSDT','funding',0),('SOLUSDT','trade',1),('SOLUSDT','mark',1),('ETHUSDT','trade',60),('SOLUSDT','trade',60)])

    def test_all_grids_partition(self):
        p=m.plan(self.c)
        for symbol,kind,interval in { (r['symbol'],r['kind'],r['interval_minutes']) for r in p }:
            rows=sorted((r for r in p if (r['symbol'],r['kind'],r['interval_minutes'])==(symbol,kind,interval)),key=lambda r:r['start'])
            self.assertTrue(all(a['end']+1==b['start'] for a,b in zip(rows,rows[1:])))
            self.assertEqual(rows[-1]['end']+1,m.ms(self.c['formation_start_utc'] if interval==60 else self.c['evaluation_end_utc']))

    def test_valid_trade(self):self.assertEqual(len(m.validate(self.raw(),self.r)),2)

    def test_symbol(self):
        self.obj['result']['symbol']='ETHUSDT'
        with self.assertRaisesRegex(ValueError,'SYMBOL'):m.validate(self.raw(),self.r)

    def test_missing_duplicate_unordered(self):
        for rows in [self.obj['result']['list'][:1],self.obj['result']['list']*2,self.obj['result']['list'][::-1]]:
            obj=copy.deepcopy(self.obj);obj['result']['list']=rows
            with self.assertRaises(ValueError):m.validate(json.dumps(obj).encode(),self.r)

    def test_invalid_price_and_volume(self):
        for index,value in [(1,'0'),(2,'98'),(3,'103'),(5,'-1'),(4,'NaN')]:
            obj=copy.deepcopy(self.obj);obj['result']['list'][0][index]=value
            with self.assertRaises(ValueError):m.validate(json.dumps(obj).encode(),self.r)

    def test_funding_symbol_grid_and_censor(self):
        r=dict(self.r,kind='funding',limit=200,interval_minutes=0)
        self.obj['result']['list']=[dict(symbol='SOLUSDT',fundingRate='-0.001',fundingRateTimestamp='60000')]
        self.assertEqual(m.validate(self.raw(),r),[(60000,'-0.001')])
        for field,value in [('symbol','ETHUSDT'),('fundingRateTimestamp','60001')]:
            obj=copy.deepcopy(self.obj);obj['result']['list'][0][field]=value
            with self.assertRaises(ValueError):m.validate(json.dumps(obj).encode(),r)
        with self.assertRaisesRegex(ValueError,'SATURATED'):m.validate(self.raw(),dict(r,limit=1))

    def wire(self,obj):return b'HTTP/2 200\r\ncontent-type: application/json\r\n\r\n'+json.dumps(obj).encode()+b'\n200'

    def test_interpret_success(self):
        self.assertEqual(m.interpret(self.wire(self.obj),0,self.r,self.c['input'],1,200000)['status'],'VALID')

    def test_only_known_one_recovery(self):
        obj=dict(retCode=10016,time=200000)
        v=m.interpret(self.wire(obj),0,self.r,self.c['input'],1,200000)
        self.assertEqual(v['wait_seconds'],2)
        for code,attempt in [(10016,2),(10006,1),(10001,1)]:
            with self.assertRaises(ValueError):m.interpret(self.wire(dict(obj,retCode=code)),0,self.r,self.c['input'],attempt,200000)

    def test_transport_and_status_not_retried(self):
        with self.assertRaises(ValueError):m.interpret(self.wire(self.obj),28,self.r,self.c['input'],1,200000)
        with self.assertRaises(ValueError):m.interpret(self.wire(self.obj).replace(b'200',b'403',1),0,self.r,self.c['input'],1,200000)

    def test_contract_scope_and_grid(self):
        c=self.c
        self.assertEqual(c['symbols'],['ETHUSDT','SOLUSDT'])
        g=c['selection']['ordered_grid'];self.assertEqual(len(g),9);self.assertEqual(len(set(map(tuple,g))),9)
        self.assertEqual(g[0],c['selection']['baseline'])
        self.assertEqual(c['risk']['research_drawdown_stop'],'0.08')
        self.assertFalse(c['decision']['g4_or_product_pass_from_backtest'])
        self.assertFalse(c['execution']['historical_filters_verified'])

    def test_only_reused_recovery_uses_bound_interpretation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'attempts'/'0000';folder.mkdir(parents=True)
            wire=self.wire(self.obj);(folder/'response.bin').write_bytes(wire)
            r=dict(status='FAILED',request=self.r,wire_sha256=m.sha(folder/'response.bin'))
            (folder/'record.json').write_text(json.dumps(r))
            with mock.patch.object(m,'RECOVERED',root), mock.patch.object(m,'effective_record',return_value=dict(r,status='VALID')) as effective:
                self.assertEqual(m.raw_page(folder,self.r,True),json.dumps(self.obj).encode())
                effective.assert_called_once_with(folder)
                with self.assertRaisesRegex(ValueError,'NOT_VALID'):m.raw_page(folder,self.r,False)

    def test_unbound_recovery_annotation_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);folder=root/'attempts'/'0000';folder.mkdir(parents=True)
            (folder/'record.json').write_text(json.dumps(dict(status='FAILED')))
            with mock.patch.object(m,'RECOVERED',root), mock.patch.object(m,'effective_record',side_effect=ValueError('CORRECTED_INTERPRETATION_BINDING')):
                with self.assertRaisesRegex(ValueError,'BINDING'):m.raw_page(folder,self.r,True)


if __name__=='__main__':unittest.main()
