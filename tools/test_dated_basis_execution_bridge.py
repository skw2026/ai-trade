from copy import deepcopy
from decimal import Decimal as D, localcontext
import json
from pathlib import Path
import tempfile
import unittest

from dated_basis_execution_bridge import AssetLedger, Instrument, decimal, rate, spot_requirement
from analyze_dated_basis_execution import replay
from audit_dated_basis_execution import verify


def metadata(kind):
    lot=dict(filterType='LOT_SIZE',minQty='.00001' if kind=='spot' else '1',
             maxQty='1000',stepSize='.00001' if kind=='spot' else '1')
    market=dict(filterType='MARKET_LOT_SIZE',minQty='0',maxQty='1000',stepSize='0')
    if kind=='spot':
        return dict(symbol='BTCUSDT',status='TRADING',baseAsset='BTC',quoteAsset='USDT',
                    isSpotTradingAllowed=True,filters=[lot,market])
    return dict(symbol='BTCUSD_261225',contractStatus='TRADING',baseAsset='BTC',quoteAsset='USD',
                marginAsset='BTC',contractType='CURRENT_QUARTER',contractSize=100,filters=[lot,market])


def instruments():
    return Instrument.metadata(metadata('spot'),'spot'),Instrument.metadata(metadata('future'),'future')


def capture():
    rows=[]
    def row(source,kind,t,payload):
        rows.append(dict(source=source,kind=kind,mono_ms=t,wall_ms=100000+t,
                         payload=dict(raw=json.dumps(payload))))
    for source in ('spot','future'):
        row(source,'snapshot',0,dict(lastUpdateId=10,bids=[['50000','100']],asks=[['50001','100']]))
    last={'spot':10,'future':9};prices={'spot':(50000,50001),'future':(50000,50001)}
    for t in (0,200,500,900):
        for source in ('spot','future'):
            u=last[source]+1;old=prices[source];new=(50000+t//100,50001+t//100)
            bids=([[str(old[0]),'0']] if old[0]!=new[0] else [])+[[str(new[0]),'100']]
            asks=([[str(old[1]),'0']] if old[1]!=new[1] else [])+[[str(new[1]),'100']]
            row(source,'update',t,dict(U=u,u=u,pu=last[source],E=100000+t,b=bids,a=asks))
            last[source]=u;prices[source]=new
    for t in (100,1150): row('clock','tick',t,{})
    row('clock','end',1200,{})
    return sorted(rows,key=lambda r:r['mono_ms'])


class QuantityTests(unittest.TestCase):
    def test_contract_units(self):
        s,f=instruments();self.assertEqual(f.contracts(2500),25);self.assertEqual(s.unit,'BTC')
    def test_coin_quantity_not_contracts(self):
        with self.assertRaises(ValueError):instruments()[1].validate('.05')
    def test_nonintegral_face_rejected(self):
        with self.assertRaises(ValueError):instruments()[1].contracts(2501)
    def test_wrong_product_identity(self):
        for k,field,value in [('spot','quoteAsset','USD'),('future','marginAsset','USDT')]:
            m=metadata(k);m[field]=value
            with self.assertRaises(ValueError):Instrument.metadata(m,k)
    def test_duplicate_filter_rejected(self):
        m=metadata('spot');m['filters'].append(m['filters'][0])
        with self.assertRaises(ValueError):Instrument.metadata(m,'spot')
    def test_grid_intersection(self):
        m=metadata('spot');m['filters'][0]['stepSize']='.00002';m['filters'][1]['stepSize']='.00003'
        s=Instrument.metadata(m,'spot');self.assertEqual(s.step(),D('.00006'))
        self.assertEqual(s.round_up('.00007'),D('.00012'))
    def test_zero_market_step_disabled(self):self.assertEqual(instruments()[0].step(),D('.00001'))
    def test_quantity_bound(self):
        with self.assertRaises(ValueError):instruments()[1].validate(1001)
    def test_bad_bounds(self):
        m=metadata('spot');m['filters'][0]['maxQty']='.000001'
        with self.assertRaises(ValueError):Instrument.metadata(m,'spot')
    def test_invalid_numbers(self):
        for n in (True,'NaN','Infinity','bad',-1):
            with self.assertRaises(ValueError):decimal(n)
    def test_fee_range(self):
        for n in (-1,1,2):
            with self.assertRaises(ValueError):rate(n)
    def test_btc_fee_gross_up(self):
        r=spot_requirement(instruments()[0],2500,50000,'BTC')
        self.assertEqual(r['gross_btc'],D('.05011'));self.assertGreaterEqual(r['net_btc'],D('.05'))
        self.assertLess(r['rounding_excess_btc'],D('.00001'))
    def test_usdt_fee_counterfactual(self):
        r=spot_requirement(instruments()[0],2500,50000,'USDT')
        self.assertEqual(r['gross_btc'],D('.05'));self.assertFalse(r['full_order_qualified'])
    def test_unsupported_fee(self):
        with self.assertRaises(ValueError):spot_requirement(instruments()[0],2500,50000,'BNB')


class LedgerTests(unittest.TestCase):
    def setUp(self):self.ledger=AssetLedger(*instruments());self.index=0
    def event(self,kind,**args):
        self.index+=1
        return dict(id=str(self.index),ms=self.index,kind=kind,**args)
    def apply(self,kind,**args):
        e=self.event(kind,**args);self.ledger.apply(e);return e
    def buy(self,asset='USDT',qty='.05'):
        return self.apply('SPOT_BUY',qty=qty,price='50000',fee_rate='.002',fee_asset=asset)
    def funded(self):
        self.buy();e=self.apply('TRANSFER_START',qty='.05')
        self.apply('TRANSFER_CONFIRM',qty='.05',transfer_id=e['id'])
    def opened(self):self.funded();self.apply('SHORT_FILL',qty=25,price=50000,fee_rate='.001')
    def test_principal_cash_debit(self):
        self.buy();self.assertEqual(self.ledger.state['usdt'],7495)
    def test_btc_fee_location(self):
        self.buy('BTC');s=self.ledger.state
        self.assertEqual(s['usdt'],7500);self.assertEqual(s['spot_btc'],D('.0499'))
        self.assertEqual(s['fees_btc'],D('.0001'))
    def test_transfer_single_count(self):
        self.buy();before=self.ledger.equity(50000,50000);self.apply('TRANSFER_START',qty='.05')
        self.assertEqual(self.ledger.equity(50000,50000),before)
    def test_transit_unavailable(self):
        self.buy();self.apply('TRANSFER_START',qty='.05')
        with self.assertRaisesRegex(ValueError,'COLLATERAL_NOT_READY'):
            self.ledger.check_order(self.event('SHORT_FILL',qty=25,price=50000,fee_rate='.001'))
    def test_wrong_transfer_atomic(self):
        self.buy();self.apply('TRANSFER_START',qty='.05');before=deepcopy(self.ledger.state)
        with self.assertRaises(ValueError):self.apply('TRANSFER_CONFIRM',qty='.05',transfer_id='other')
        self.assertEqual(self.ledger.state,before)
    def test_transfer_cancel(self):
        self.buy();e=self.apply('TRANSFER_START',qty='.05')
        self.apply('TRANSFER_CANCEL',qty='.05',transfer_id=e['id'])
        self.assertEqual(self.ledger.state['spot_btc'],D('.05'));self.assertEqual(self.ledger.state['margin_btc'],0)
    def test_idempotent_event(self):
        e=self.buy();before=deepcopy(self.ledger.state)
        self.assertFalse(self.ledger.apply(e));self.assertEqual(self.ledger.state,before)
    def test_conflicting_event(self):
        e=self.buy();e['qty']='.06'
        with self.assertRaisesRegex(ValueError,'CONFLICTING_EVENT'):self.ledger.apply(e)
    def test_clock_regression(self):
        self.buy();e=self.event('RISK_STOP');e['ms']=0
        with self.assertRaises(ValueError):self.ledger.apply(e)
    def test_negative_clock(self):
        with self.assertRaises(ValueError):self.ledger.apply(dict(id='x',ms=-1,kind='RISK_STOP'))
    def test_partial_inverse_close(self):
        self.opened();self.apply('CLOSE_FILL',qty=10,price=40000,fee_rate='.001')
        self.assertEqual(self.ledger.state['contracts'],15)
        self.assertEqual(self.ledger.state['inverse_basis'],D('.03'))
        self.assertEqual(self.ledger.state['margin_btc'],D('.054925'))
    def test_differently_priced_partial_fills(self):
        self.funded();self.apply('SHORT_FILL',qty=10,price=40000,fee_rate='0')
        self.apply('SHORT_FILL',qty=15,price=60000,fee_rate='0')
        self.assertEqual(self.ledger.state['inverse_basis'],D('.05'))
    def test_settlement_once(self):
        self.opened();e=self.apply('SETTLE',price=40000,fee_rate='.001')
        self.assertFalse(self.ledger.apply(e));self.assertEqual(self.ledger.state['contracts'],0)
        with self.assertRaises(ValueError):self.apply('SETTLE',price=40000,fee_rate='.001')
    def test_overclose_rejected(self):
        self.opened()
        with self.assertRaises(ValueError):self.apply('CLOSE_FILL',qty=26,price=40000,fee_rate='.001')
    def test_return_open_margin_rejected(self):
        self.opened()
        with self.assertRaises(ValueError):self.apply('RETURN_MARGIN_CONFIRMED')
    def test_cash_flow_independent_algebra(self):
        self.opened();self.apply('CLOSE_FILL',qty=25,price=40000,fee_rate='.001')
        self.apply('RETURN_MARGIN_CONFIRMED');q,dust=self.ledger.liquidation_quantity()
        self.apply('SPOT_SELL',qty=str(q),price=41000,fee_rate='.002')
        expected_coin=D('.05')-D('2.5')/50000+D(2500)/40000-D(2500)/50000-D('2.5')/40000
        expected=10000-D('.05')*50000*D('1.002')+q*41000*D('.998')+dust*41000
        self.assertEqual(q+dust,expected_coin);self.assertEqual(self.ledger.equity(41000,40000),expected)
        self.assertLess(dust,instruments()[0].step());self.assertGreater(dust,0)
    def test_halt_close_allowed(self):
        self.opened();self.apply('RISK_STOP')
        with self.assertRaises(ValueError):self.ledger.check_order(self.event('SPOT_BUY',qty='.05',price=50000,fee_rate='.002',fee_asset='BTC'))
        self.apply('CLOSE_FILL',qty=25,price=40000,fee_rate='.001')
    def test_unknown_blocks_new_risk(self):
        self.apply('MARK_UNKNOWN')
        with self.assertRaises(ValueError):self.ledger.check_order(self.event('SPOT_BUY',qty='.05',price=50000,fee_rate='.002',fee_asset='BTC'))
    def test_second_leg_rejected_retains_spot(self):
        self.buy();self.apply('SECOND_LEG_REJECTED')
        self.assertEqual(self.ledger.state['spot_btc'],D('.05'))
        self.apply('SPOT_SELL',qty='.05',price=49900,fee_rate='.002')
        self.assertTrue(self.ledger.state['uncertain'])
    def test_unfunded_future_first_rejected(self):
        with self.assertRaises(ValueError):self.ledger.check_order(self.event('SHORT_FILL',qty=25,price=50000,fee_rate='.001'))
    def test_insufficient_cash_atomic(self):
        before=deepcopy(self.ledger.state)
        with self.assertRaises(ValueError):self.ledger.check_order(self.event('SPOT_BUY',qty=1,price=50000,fee_rate='.002',fee_asset='BTC'))
        self.assertEqual(self.ledger.state,before)
    def test_negative_terminal_btc_not_forgiven(self):
        self.buy(qty='.006');e=self.apply('TRANSFER_START',qty='.006')
        self.apply('TRANSFER_CONFIRM',qty='.006',transfer_id=e['id'])
        self.apply('SHORT_FILL',qty=25,price=50000,fee_rate='.001')
        self.apply('SETTLE',price=100000,fee_rate='.001')
        self.assertLess(self.ledger.state['margin_btc'],0);self.assertTrue(self.ledger.state['uncertain'])
        with self.assertRaises(ValueError):self.apply('RETURN_MARGIN_CONFIRMED')
    def test_no_qualification(self):
        r=self.ledger.snapshot()
        for k in ('actual_execution_qualified','risk_engine_qualified','account_permission_verified','g4'):self.assertFalse(r[k])
    def test_confirmed_late_fill_not_dropped(self):
        self.apply('RISK_STOP');self.buy()
        self.assertEqual(self.ledger.state['spot_btc'],D('.05'));self.assertTrue(self.ledger.state['uncertain'])
    def test_confirmed_unfunded_fill_retained(self):
        self.apply('SHORT_FILL',qty=25,price=50000,fee_rate='.001')
        self.assertEqual(self.ledger.state['contracts'],25);self.assertLess(self.ledger.state['margin_btc'],0)
        self.assertTrue(self.ledger.state['uncertain'])
    def test_confirmed_partial_below_order_minimum(self):
        m=metadata('spot');m['filters'][0]['minQty']='.01'
        self.ledger=AssetLedger(Instrument.metadata(m,'spot'),instruments()[1])
        e=self.event('SPOT_BUY',qty='.001',price=50000,fee_rate='.002',fee_asset='BTC')
        with self.assertRaises(ValueError):self.ledger.check_order(e)
        self.ledger.apply(e);self.assertEqual(self.ledger.state['spot_btc'],D('.000998'))
    def test_wrong_sell_fee_currency(self):
        self.buy()
        with self.assertRaises(ValueError):self.apply('SPOT_SELL',qty='.05',price=50000,fee_rate='.002',fee_asset='BTC')
    def test_wrong_future_fee_currency(self):
        self.funded()
        with self.assertRaises(ValueError):self.apply('SHORT_FILL',qty=25,price=50000,fee_rate='.001',fee_asset='USDT')


class LatencyTests(unittest.TestCase):
    def run_capture(self,rows=None):return replay(capture() if rows is None else rows,*instruments())
    def case(self,result,delay=250,order='SPOT_FIRST',asset='BTC',tick=0):
        return next(r for r in result['rows'] if (r['tick'],r['delay_ms'],r['order'],r['fee_asset'])==(tick,delay,order,asset))
    def test_all_cases_registered(self):
        r=self.run_capture();self.assertEqual(len(r['rows']),24);self.assertEqual(len(r['groups']),12)
    def test_zero_delay(self):
        r=self.case(self.run_capture(),delay=0)
        self.assertEqual(D(r['added_delay_cost_usdt_equivalent']),0)
    def test_causal_second_leg(self):
        r=self.case(self.run_capture())
        self.assertEqual(D(r['future']['price_bound']),50002)
        self.assertEqual(D(r['spot']['price_bound']),50001)
    def test_fixed_first_quantity(self):
        r=self.run_capture();a=self.case(r,delay=0);b=self.case(r,delay=250)
        self.assertEqual(a['gross_spot_btc'],b['gross_spot_btc'])
        self.assertNotEqual(a['residual_before_future_fee_btc'],b['residual_before_future_fee_btc'])
    def test_tail_censored_not_deleted(self):
        r=self.run_capture();self.assertEqual(r['summary']['statuses']['RIGHT_CENSORED'],8)
    def test_future_first_distinct_cost(self):
        r=self.case(self.run_capture(),order='FUTURE_FIRST_PREFUNDED_ONLY')
        self.assertGreater(D(r['added_delay_cost_usdt_equivalent']),0)
        self.assertEqual(r['collateral_ready'],'UNVERIFIED')
    def test_second_leg_unavailable_and_unwind(self):
        rows=capture();row=next(r for r in rows if r['mono_ms']==200 and r['source']=='future')
        p=json.loads(row['payload']['raw']);p['b']=[['50000','0'],['50002','1']];row['payload']['raw']=json.dumps(p)
        r=self.case(self.run_capture(rows));self.assertEqual(r['status'],'SECOND_LEG_UNAVAILABLE')
        self.assertEqual(r['unwind']['status'],'DISPLAYED_UNWIND_ONLY')
        self.assertFalse(r['unwind']['full_order_qualified'])
    def test_sequence_failure_never_fills(self):
        rows=capture();row=next(r for r in rows if r['mono_ms']==200 and r['source']=='future')
        p=json.loads(row['payload']['raw']);p['pu']=999;row['payload']['raw']=json.dumps(p)
        r=self.run_capture(rows);self.assertIsNotNone(r['summary']['capture_failure'])
        self.assertEqual(self.case(r)['status'],'CAPTURE_TAINTED')
    def test_duplicate_tick_rejected(self):
        rows=capture();rows.append(deepcopy(next(r for r in rows if r['kind']=='tick')))
        rows.sort(key=lambda r:r['mono_ms'])
        with self.assertRaises(ValueError):self.run_capture(rows)
    def test_reversed_clock_rejected(self):
        with self.assertRaises(ValueError):self.run_capture(capture()[::-1])
    def test_metadata_multiplier_not_assumed(self):
        m=metadata('future');m['contractSize']=50
        with self.assertRaises(ValueError):replay(capture(),instruments()[0],Instrument.metadata(m,'future'))
    def test_no_execution_or_trades_claim(self):
        r=self.run_capture();self.assertFalse(r['summary']['g4']);self.assertEqual(r['summary']['strategy_entries'],0)
        self.assertFalse(r['metadata_semantics']['dynamic_filters_verified'])
    def test_independent_raw_audit(self):
        rows=capture();r=self.run_capture(rows)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'frames.jsonl';p.write_text(''.join(json.dumps(x)+'\n' for x in rows))
            a=verify(p,r);self.assertEqual(a['registered_cases'],24)
            self.assertEqual(a['displayed_leg_bounds'],24)
    def test_auditor_rejects_changed_price(self):
        rows=capture();r=self.run_capture(rows);self.case(r)['future']['price_bound']='99999'
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'frames.jsonl';p.write_text(''.join(json.dumps(x)+'\n' for x in rows))
            with self.assertRaises(AssertionError):verify(p,r)
    def test_same_timestamp_after_tick_not_visible(self):
        rows=capture()
        for row in rows:
            if row['mono_ms']==200:row['mono_ms']=100
        rows.sort(key=lambda r:(r['mono_ms'],0 if r['kind']=='tick' else 1))
        r=self.case(self.run_capture(rows),delay=0)
        self.assertEqual(D(r['future']['price_bound']),50000)
    def test_selected_legs_skew(self):
        r=self.case(self.run_capture(),delay=1000)
        self.assertEqual(r['status'],'SECOND_LEG_UNAVAILABLE');self.assertEqual(r['reason'],'EVENT_SKEW')
    def test_first_leg_current_depth_not_required(self):
        rows=capture();row=next(r for r in rows if r['mono_ms']==200 and r['source']=='spot')
        p=json.loads(row['payload']['raw']);p['a']=[['50001','0'],['50003','.00001']];row['payload']['raw']=json.dumps(p)
        self.assertEqual(self.case(self.run_capture(rows))['status'],'DISPLAYED_BOTH_LEGS')
    def test_delay_cost_includes_future_fee(self):
        r=self.case(self.run_capture())
        with localcontext() as c:
            c.prec=50
            expected=D('2502.5')*D(50001)*(1/D(50002)-1/D(50000))
            self.assertLess(abs(D(r['added_delay_cost_usdt_equivalent'])-expected),D('1e-35'))
    def audit_result(self,r,rows=None):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'frames.jsonl';p.write_text(''.join(json.dumps(x)+'\n' for x in (capture() if rows is None else rows)))
            return verify(p,r)
    def test_auditor_rejects_deleted_case(self):
        r=self.run_capture();r['rows'].pop()
        with self.assertRaises(AssertionError):self.audit_result(r)
    def test_auditor_rejects_duplicate_case(self):
        r=self.run_capture();r['rows'][-1]=deepcopy(r['rows'][0])
        with self.assertRaises(AssertionError):self.audit_result(r)
    def test_auditor_rejects_disposition_tampering(self):
        from collections import Counter
        r=self.run_capture();self.case(r)['status']='INITIAL_UNAVAILABLE'
        r['summary']['statuses']=dict(Counter(x['status'] for x in r['rows']))
        with self.assertRaises(AssertionError):self.audit_result(r)
    def test_auditor_rejects_unwind_tampering(self):
        r=self.run_capture();self.case(r,delay=1000)['unwind']['cash_deficit_before_dust_usdt']='0'
        with self.assertRaises(AssertionError):self.audit_result(r)
    def test_auditor_rejects_summary_promotion(self):
        r=self.run_capture();r['summary']['g4']=True
        with self.assertRaises(AssertionError):self.audit_result(r)
    def test_auditor_same_time_reception_boundary(self):
        rows=capture()
        for row in rows:
            if row['mono_ms']==200:row['mono_ms']=100
        rows.sort(key=lambda r:(r['mono_ms'],0 if r['kind']=='tick' else 1))
        self.assertGreater(self.audit_result(self.run_capture(rows),rows)['complete_cases'],0)
    def test_auditor_rejects_status_forgery(self):
        r=self.run_capture();r['summary']['status']='ECONOMICALLY_PROVEN'
        with self.assertRaises(AssertionError):self.audit_result(r)
    def test_auditor_rejects_failure_erasure_mismatch(self):
        r=self.run_capture();r['summary']['capture_failure']='GAP'
        with self.assertRaises(AssertionError):self.audit_result(r)
    def test_auditor_rejects_metadata_promotion(self):
        r=self.run_capture();r['metadata_semantics']['account_permission_verified']=True
        with self.assertRaises(AssertionError):self.audit_result(r)


if __name__=='__main__':unittest.main()
