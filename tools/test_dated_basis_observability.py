from decimal import Decimal as D
import json
import unittest

from analyze_dated_basis_quotes import Book
import dated_basis_observability as obs
from test_dated_basis_opportunity_replay import bar,fixture


def book(kind='future',quantity='100',received=100):
    b=Book(kind)
    b.snapshot(dict(lastUpdateId=10,bids=[['104',quantity]],asks=[['105',quantity]]))
    event=dict(U=11 if kind=='spot' else 10,u=11 if kind=='spot' else 10,pu=9,
               E=1000,b=[],a=[])
    b.update(dict(mono_ms=received,wall_ms=1000,payload=dict(raw=json.dumps(event))))
    return b


class ObservationTests(unittest.TestCase):
    def test_zero_trades_distinct_from_missing(self):
        quiet=obs.candle(bar(0,n=0,volume=0),0,300000)
        missing=obs.candle(None,0,300000)
        self.assertEqual(quiet['status'],'COMPLETE_QUIET_CANDLE')
        self.assertEqual(missing['status'],'MISSING')
        self.assertFalse(quiet['price_range_observed'])

    def test_positive_trades_not_quote_proof(self):
        self.assertEqual(obs.candle(bar(0),0,300000)['trades'],'POSITIVE_RECORDED')
        self.assertIsNone(obs.quote(None,'ask',25,100)['displayed_depth_covers_request'])

    def test_mark_zero_volume_is_not_zero_liquidity(self):
        mark=obs.candle(bar(0,n=0,volume=0),0,300000,mark=True)
        self.assertEqual(mark['status'],'MARK_RANGE_OBSERVED')
        self.assertEqual(mark['trades'],'NOT_APPLICABLE')

    def test_unclosed_candle_cannot_reveal_activity(self):
        self.assertEqual(obs.candle(bar(0),0,299999)['status'],'NOT_YET_CLOSED')

    def test_incomplete_not_quiet(self):
        row=bar(0,n=0,volume=0); row['complete']=False
        self.assertEqual(obs.candle(row,0,300000)['status'],'INCOMPLETE')

    def test_counts_inconsistent_rejected(self):
        for row in (bar(0,n=1,volume=0),bar(0,n=0,volume=1),bar(0,n=True),bar(0,n=-1)):
            self.assertEqual(obs.candle(row,0,300000)['status'],'INVALID_TRADE_COUNTS')

    def test_bad_price_does_not_become_valuation(self):
        for value in ('NaN','Infinity','0','-1'):
            row=bar(0); row['c']=value
            self.assertEqual(obs.candle(row,0,300000,mark=True)['status'],'INVALID_PRICE')

    def test_bar_clock_validation(self):
        with self.assertRaises(ValueError): obs.candle(bar(0),1,300000)

    def test_quiet_candle_compatible_with_deep_or_thin_book(self):
        quiet=bar(0,n=0,volume=0)
        deep=obs.quote(book(quantity='100'),'ask',25,200)
        thin=obs.quote(book(quantity='1'),'ask',25,200)
        self.assertEqual(obs.candle(quiet,0,300000),obs.candle(quiet,0,300000))
        self.assertTrue(deep['displayed_depth_covers_request'])
        self.assertFalse(thin['displayed_depth_covers_request'])

    def test_active_candle_compatible_with_stale_book(self):
        self.assertEqual(obs.candle(bar(0),0,300000)['trades'],'POSITIVE_RECORDED')
        self.assertEqual(obs.quote(book(),'ask',25,1101)['status'],'STALE_LOCAL_RECEIPT')

    def test_age_boundary(self):
        b=book()
        self.assertTrue(obs.quote(b,'ask',25,1100)['displayed_depth_covers_request'])
        self.assertFalse(obs.quote(b,'ask',25,1101)['actual_execution_qualified'])

    def test_future_receipt_rejected(self):
        self.assertEqual(obs.quote(book(),'ask',25,99)['status'],'FUTURE_RECEIPT')

    def test_unbridged_snapshot_does_not_certify(self):
        b=Book('future'); b.snapshot(dict(lastUpdateId=10,bids=[['104','100']],asks=[['105','100']]))
        self.assertEqual(obs.quote(b,'ask',25,100)['status'],'UNBRIDGED')

    def test_missing_book_distinct_from_thin(self):
        self.assertEqual(obs.quote(None,'ask',25,100)['status'],'NOT_CAPTURED')
        self.assertEqual(obs.quote(book(quantity='1'),'ask',25,100)['status'],'INSUFFICIENT_CAPTURED_DEPTH')

    def test_unknown_snapshot_tail_not_used(self):
        b=book(quantity='1'); b.asks[D('106')]=D('100')
        self.assertEqual(obs.quote(b,'ask',25,100)['status'],'INSUFFICIENT_CAPTURED_DEPTH')

    def test_negative_zero_and_nonfinite_size_rejected(self):
        for value in ('-1','0','NaN','Infinity'):
            with self.assertRaises(ValueError): obs.quote(book(),'ask',value,100)

    def test_inverse_contract_vwap(self):
        self.assertEqual(D(obs.quote(book(),'ask',25,100)['vwap']),105)

    def test_crossed_book(self):
        b=book(); b.bids[D(106)]=D(100)
        self.assertEqual(obs.quote(b,'ask',25,100)['status'],'EMPTY_OR_CROSSED_BOOK')

    def test_local_freshness_not_exchange_clock_certification(self):
        self.assertEqual(obs.quote(book(),'ask',25,100)['exchange_clock_alignment'],'UNKNOWN')

    def test_entry_and_exit_different_sides(self):
        s,f=book('spot'),book()
        entry=obs.pair(s,f,100,1,direction='entry')
        exit=obs.pair(s,f,100,1,direction='exit')
        self.assertEqual(D(entry['future']['vwap']),104)
        self.assertEqual(D(exit['future']['vwap']),105)
        self.assertEqual(D(entry['spot']['vwap']),105)
        self.assertEqual(D(exit['spot']['vwap']),104)

    def test_cross_leg_skew_not_atomic(self):
        s,f=book('spot'),book(); f.event['E']=1501
        p=obs.pair(s,f,100,1,direction='exit')
        self.assertFalse(p['displayed_pair_observed'])
        self.assertFalse(p['same_time_atomic_execution'])

    def test_no_permission_even_when_quotes_cover_size(self):
        p=obs.pair(book('spot'),book(),100,1,direction='exit')
        self.assertTrue(p['displayed_pair_observed'])
        self.assertFalse(p['orders_authorized'])
        self.assertFalse(p['recovery_authorized'])
        self.assertFalse(p['actual_execution_qualified'])

    def test_wrong_instrument_kind_rejected(self):
        with self.assertRaises(ValueError): obs.pair(book(),book('spot'),100,1,direction='exit')

    def test_historical_quotes_still_missing_on_active_bar(self):
        q,i=fixture(); t=i['earliest_entry_ms']; p=obs.historical_bar_evidence(q,t)
        self.assertEqual(p['future_trades']['status'],'TRADE_RANGE_OBSERVED')
        self.assertEqual(p['executable_quotes']['status'],'NOT_CAPTURED')

    def test_historical_quiet_with_mark_observed(self):
        q,i=fixture(); t=i['earliest_entry_ms']; q['series']['future'][str(t)]=bar(t,n=0,volume=0)
        p=obs.historical_bar_evidence(q,t)
        self.assertEqual(p['future_trades']['status'],'COMPLETE_QUIET_CANDLE')
        self.assertTrue(p['future_mark_range']['price_range_observed'])
        self.assertFalse(p['recovery_authorized'])

    def test_depth_recovery_not_permission_recovery(self):
        b=book(quantity='1'); before=obs.quote(b,'ask',25,100)
        b.asks[D(105)]=D(100); after=obs.quote(b,'ask',25,100)
        self.assertFalse(before['displayed_depth_covers_request'])
        self.assertTrue(after['displayed_depth_covers_request'])
        self.assertFalse(after['recovery_authorized'])


if __name__=='__main__': unittest.main()
