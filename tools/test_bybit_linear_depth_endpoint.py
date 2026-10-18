"""Synthetic fixed-endpoint, state-machine and unchanged cash-kernel regressions."""
import copy
import json
from decimal import Decimal as D
import unittest
import bybit_linear_depth_endpoint as p
from test_bybit_linear_basis_panel import instrument

END=1000000


def message(symbol='BTCUSDT',kind='snapshot',u=10,ts=END-100,bid='99999',ask='100000'):
    return dict(topic='orderbook.50.'+symbol,type=kind,ts=ts,cts=ts-10,
                data=dict(s=symbol,u=u,seq=u*2,b=[[bid,'1']],a=[[ask,'1']]))


def row(m,mono=119950,wall=END-50):
    return dict(kind='message',source='spot' if m.get('data',{}).get('s')=='BTCUSDT' else 'linear',
                mono_ms=mono,wall_ms=wall,payload=dict(raw=json.dumps(m)))


def book(symbol='BTCUSDT',bid='99999',ask='100000'):
    b=p.Depth(symbol);m=message(symbol,bid=bid,ask=ask);b.update(m,row(m));return b


def catalog():
    fs={s:instrument(s) for s in p.SYMBOLS}
    spot=dict(symbol='BTCUSDT',status='Trading',baseCoin='BTC',quoteCoin='USDT',
              lotSizeFilter=dict(basePrecision='0.000001',minOrderAmt='5',maxMarketOrderQty='120'))
    return fs,spot


class DepthTests(unittest.TestCase):
    def test_snapshot_and_absolute_delta(self):
        b=book();m=message(kind='delta',u=11,ts=END-90)
        m['data']['b']=[['99999','0'],['99998','2']];m['data']['a']=[['100000','3']]
        b.update(m,row(m));self.assertIsNone(b.issue)
        self.assertEqual(b.b,{D(99998):D(2)});self.assertEqual(b.a,{D(100000):D(3)})

    def test_empty_delta_does_not_renew_quote(self):
        b=book();q=b.quote.copy();m=message(kind='delta',u=11,ts=END-90)
        m['data']['b']=[];m['data']['a']=[];b.update(m,row(m))
        self.assertEqual(b.quote,q);self.assertEqual(b.empty_deltas,1)

    def test_exact_duplicate_does_not_renew_receipt(self):
        b=book();q=b.quote.copy();m=message();b.update(m,row(m,120000,END))
        self.assertEqual(b.quote,q);self.assertEqual(b.duplicates,1)

    def test_reset_and_new_snapshot_latch(self):
        for kind,u in [('snapshot',11),('delta',1)]:
            b=book();m=message(kind=kind,u=u);b.update(m,row(m))
            self.assertEqual(b.issue,'SNAPSHOT_OR_SERVICE_RESET')
            b.update(message(),row(message()));self.assertIsNotNone(b.issue)

    def test_delta_before_snapshot(self):
        b=p.Depth('BTCUSDT');m=message(kind='delta');b.update(m,row(m))
        self.assertEqual(b.issue,'DELTA_WITHOUT_SNAPSHOT')

    def test_regressions(self):
        for key,value in [('u',9),('seq',19)]:
            b=book();m=message(kind='delta',u=11);m['data'][key]=value;b.update(m,row(m))
            self.assertEqual(b.issue,'SEQUENCE_REGRESSION')

    def test_gaps_are_not_falsely_rejected(self):
        b=book();m=message(kind='delta',u=100);b.update(m,row(m));self.assertIsNone(b.issue)

    def test_same_update_changed_quantity(self):
        b=book();m=message(kind='delta');m['data']['b'][0][1]='2';b.update(m,row(m))
        self.assertEqual(b.issue,'SAME_UPDATE_CHANGED_DATA')

    def test_clock_regression_and_future_clock(self):
        for delta,expected in [(-1,'CLOCK_REGRESSION'),(100,'CLOCK_ORDER')]:
            b=book();m=message(kind='delta',u=11,ts=END-100+delta);b.update(m,row(m))
            self.assertEqual(b.issue,expected)

    def test_identity(self):
        b=book();m=message(kind='delta',u=11);r=row(m);r['source']='linear';b.update(m,r)
        self.assertEqual(b.issue,'IDENTITY')

    def test_price_duplicates_order_and_cross(self):
        for bids,expected in [([['99999','1'],['99999','2']],'DUPLICATE_PRICE'),
                              ([['99998','1'],['99999','1']],'SNAPSHOT_ORDER'),
                              ([['100000','1']],'CROSSED_BOOK')]:
            b=p.Depth('BTCUSDT');m=message();m['data']['b']=bids;b.update(m,row(m));self.assertEqual(b.issue,expected)

    def test_decimal_contract(self):
        for value in ['.1','NaN','Infinity','-1','',True,1]:
            with self.subTest(value=value),self.assertRaises(ValueError):p.levels([['1',value]],True,'b')

    def test_snapshot_zero_invalid_delta_zero_valid(self):
        with self.assertRaises(ValueError):p.levels([['1','0']],True,'b')
        self.assertEqual(p.levels([['1','0']],False,'b'),{D(1):D(0)})

    def test_top50_lost_tail_does_not_revive(self):
        b=p.Depth('BTCUSDT');m=message(bid='100',ask='200')
        m['data']['b']=[[str(v),'1'] for v in range(100,50,-1)];b.update(m,row(m))
        d=message(kind='delta',u=11);d['data']['b']=[['101','1']];d['data']['a']=[];b.update(d,row(d))
        self.assertNotIn(D(51),b.b);d=copy.deepcopy(d);d['data']['u']=12;d['data']['seq']=24;d['data']['b']=[['101','0']]
        b.update(d,row(d));self.assertEqual(len(b.b),49);self.assertNotIn(D(51),b.b)

    def test_old_cts_not_changed_by_new_clock(self):
        s,f=book(),book(p.SYMBOLS[0],'103000','103001')
        s.quote['cts']=1;f.quote['cts']=1
        r=p.economic(s,f,instrument(),END);self.assertFalse(r['legacy_cts_condition'])
        self.assertEqual(len(r['cases']),36);self.assertEqual(s.quote['cts'],1)

    def test_clock_thresholds(self):
        s,f=book(),book(p.SYMBOLS[0],'103000','103001')
        f.quote['ts']=s.quote['ts']-500;self.assertEqual(p.pair_reasons(s,f,END),[])
        f.quote['ts']-=1;self.assertIn('PUBLICATION_SKEW',p.pair_reasons(s,f,END))
        f.quote['mono']=118999;self.assertIn('future:STALE_RECEIVE',p.pair_reasons(s,f,END))

    def test_base_positive_pressure_negative_explicit(self):
        r=p.economic(book(),book(p.SYMBOLS[0],'100350','100351'),instrument(),END)
        self.assertGreater(D(r['base_cash_range'][1]),0)
        self.assertLess(D(r['stress_cash_range'][1]),0)
        self.assertEqual(r['status'],'NO_PRESSURE_CASH_MARGIN')

    def test_pressure_depth_not_filled_at_last_price(self):
        s=book();s.a={D(100000):D('.02504')}
        r=p.economic(s,book(p.SYMBOLS[0],'103000','103001'),instrument(),END)
        self.assertEqual(r['reasons'],['stress:DEPTH_INSUFFICIENT']);self.assertEqual(len(r['cases']),18)
        self.assertEqual(r['status'],'INPUT_INSUFFICIENT')

    def test_cash_kernel_endpoint_and_qualification(self):
        r=p.economic(book(),book(p.SYMBOLS[0],'103000','103001'),instrument(),END)
        self.assertEqual(len(r['cases']),36);self.assertFalse(r['qualified']);self.assertFalse(r['actual_fills'])
        self.assertGreater(r['terminal_margin_failure_cases'],0)
        for c in r['cases']:
            self.assertAlmostEqual(D(c['days']),D(int(instrument()['deliveryTime'])-END)/86400000,places=24)
            self.assertGreaterEqual(D(c['net_btc']),D(c['q']))
            self.assertLess(D(c['remaining_btc']),D('.000001'))
        self.assertEqual(sum(D(q) for _,q in r['depth_used']['base']['spot_asks']),D(r['cases'][0]['gross_spot_btc']))

    def test_catalog_changes_rejected(self):
        fs,spot=catalog();ref=dict(instruments=copy.deepcopy(fs),spot=copy.deepcopy(spot))
        def run():return p.validate_catalog(dict(retCode=0,result=dict(list=list(fs.values()))),
                                            dict(retCode=0,result=dict(list=[spot])),ref)
        self.assertEqual(len(run()),8)
        fs[p.SYMBOLS[0]]['lotSizeFilter']['qtyStep']='0.01'
        with self.assertRaisesRegex(ValueError,'GRID'):run()
        fs=copy.deepcopy(ref['instruments']);spot['lotSizeFilter']['basePrecision']='0.001'
        with self.assertRaisesRegex(ValueError,'SPOT_METADATA_CHANGED'):run()

    def test_fixed_endpoint_keeps_all_eight_and_ignores_after(self):
        rows=[]
        for source in ['spot','linear']:
            rows.append(dict(kind='message',source=source,mono_ms=0,wall_ms=END-120000,
                             payload=dict(raw=json.dumps(dict(op='subscribe',success=True,req_id='obs-'+source)))))
        for s in ('BTCUSDT',)+p.SYMBOLS:
            m=message(s);rows.append(row(m))
        after=message(kind='delta',u=11);after['data']['b'][0][1]='999';rows.append(row(after,120001,END+1))
        receipt=dict(status='CAPTURE_COMPLETE',observation_stop_ms=120000,reconnects=0,
                     fixed_duration_ms=120000,start_wall_ms=END-120000)
        r=p.endpoint(rows,receipt,catalog()[0]);self.assertEqual(len(r['results']),8)
        self.assertEqual(r['books']['BTCUSDT']['bids'][0][1],'1');self.assertEqual(r['economic_endpoints'],1)
        receipt['status']='INCOMPLETE'
        with self.assertRaisesRegex(ValueError,'CAPTURE_INCOMPLETE'):p.endpoint(rows,receipt,catalog()[0])


if __name__=='__main__':unittest.main()
