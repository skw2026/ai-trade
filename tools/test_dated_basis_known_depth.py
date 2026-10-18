from decimal import Decimal as D
import json
import unittest

from dated_basis_known_depth import KnownDepth


def update(kind='spot',u=11,pu=10,received=100,bids=None,asks=None):
    return dict(mono_ms=received,wall_ms=1000,payload=dict(raw=json.dumps(
        dict(U=u,u=u,pu=pu,E=1000,b=[] if bids is None else bids,a=[] if asks is None else asks))))


def build(kind='spot'):
    b=KnownDepth(kind)
    b.snapshot(dict(lastUpdateId=10,bids=[['100','1']],asks=[['101','1']]))
    b.update(update(kind,u=11 if kind=='spot' else 10,pu=9))
    return b


class KnownDepthTests(unittest.TestCase):
    def test_snapshot_and_bridge(self):
        self.assertTrue(build().bound('bid',1,100)['displayed_depth_covers_request'])

    def test_explicit_outside_bid_is_known(self):
        b=build();b.update(update(u=12,bids=[['100','0'],['99','2']]))
        r=b.bound('bid',1,100)
        self.assertEqual(D(r['price_bound']),99)
        self.assertTrue(r['used_levels'][0]['outside_initial_region'])
        self.assertEqual(r['used_levels'][0]['origin'],'ABSOLUTE_SEQUENCE_UPDATE')

    def test_explicit_outside_ask_is_known(self):
        b=build();b.update(update(u=12,asks=[['101','0'],['102','2']]))
        self.assertEqual(D(b.bound('ask',1,100)['price_bound']),102)

    def test_absent_outside_depth_not_fabricated(self):
        self.assertFalse(build().bound('bid',2,100)['displayed_depth_covers_request'])

    def test_zero_update_removes_provenance(self):
        b=build();b.update(update(u=12,bids=[['99','2']]))
        b.update(update(u=13,bids=[['99','0']]))
        self.assertFalse(b.bound('bid',2,100)['displayed_depth_covers_request'])

    def test_absolute_quantity_not_increment(self):
        b=build();b.update(update(u=12,bids=[['100','1.5']]))
        self.assertFalse(b.bound('bid',2,100)['displayed_depth_covers_request'])

    def test_old_update_cannot_create_known_level(self):
        b=build();b.update(update(u=9,bids=[['99','100']]))
        self.assertFalse(b.bound('bid',2,100)['displayed_depth_covers_request'])

    def test_gap_taints_and_cannot_recover(self):
        b=build()
        with self.assertRaises(ValueError): b.update(update(u=13,bids=[['99','100']]))
        self.assertEqual(b.bound('bid',1,100)['status'],'TAINTED_BOOK')
        with self.assertRaisesRegex(ValueError,'TAINTED'): b.update(update(u=12))

    def test_future_sequence_taints(self):
        b=build('future')
        with self.assertRaises(ValueError): b.update(update('future',u=11,pu=8))
        self.assertEqual(b.bound('ask',1,100)['status'],'TAINTED_BOOK')

    def test_malformed_update_taints_after_partial_mutation(self):
        b=build()
        with self.assertRaises(ValueError): b.update(update(u=12,bids=[['99','2']],asks=[['-1','2']]))
        self.assertEqual(b.bound('bid',2,100)['status'],'TAINTED_BOOK')

    def test_stale_cannot_certify(self):
        self.assertIsNone(build().bound('bid',1,1101)['displayed_depth_covers_request'])

    def test_future_receipt_cannot_certify(self):
        self.assertIsNone(build().bound('bid',1,99)['displayed_depth_covers_request'])

    def test_unbridged_cannot_certify(self):
        b=KnownDepth('spot');b.snapshot(dict(lastUpdateId=10,bids=[['100','1']],asks=[['101','1']]))
        self.assertEqual(b.bound('bid',1,100)['status'],'UNBRIDGED')

    def test_pre_snapshot_buffer_provenance(self):
        b=KnownDepth('spot');b.update(update(u=11,bids=[['99','2']]))
        b.snapshot(dict(lastUpdateId=10,bids=[['100','1']],asks=[['101','1']]))
        self.assertTrue(b.bound('bid',2,100)['displayed_depth_covers_request'])

    def test_inverse_harmonic_bound(self):
        b=build('future');b.update(update('future',u=11,pu=10,bids=[['100','10'],['99','20']]))
        price=D(b.bound('bid',25,100)['price_bound'])
        self.assertLess(price,100);self.assertGreater(price,99)

    def test_unknown_better_liquidity_can_only_improve_sell(self):
        b=build();b.update(update(u=12,bids=[['99','10']]))
        bound=D(b.bound('bid',2,100)['price_bound'])
        self.assertLessEqual(bound,(D(100)*2+D('99.5'))/3)
        self.assertEqual(b.bound('bid',2,100)['bound_direction'],'LOWER_SELL_PRICE')

    def test_buy_bound_is_upper(self):
        self.assertEqual(build().bound('ask',1,100)['bound_direction'],'UPPER_BUY_PRICE')

    def test_no_permission_or_complete_book_claim(self):
        r=build().bound('bid',1,100)
        for key in ('exact_full_book_vwap','actual_execution_qualified','orders_authorized','recovery_authorized'):
            self.assertFalse(r[key])

    def test_unauthorized_level_provenance_not_accepted(self):
        b=build();b._book.bids[D(99)]=D(100)
        self.assertEqual(b.bound('bid',2,100)['status'],'UNBOUND_LEVEL_PROVENANCE')

    def test_size_validation(self):
        for size in (0,-1,'NaN','Infinity'):
            with self.assertRaises(ValueError): build().bound('bid',size,100)

    def test_second_snapshot_cannot_reset(self):
        b=build()
        with self.assertRaises(ValueError): b.snapshot(dict(lastUpdateId=10,bids=[['100','1']],asks=[['101','1']]))
        self.assertEqual(b.bound('bid',1,100)['status'],'TAINTED_BOOK')


if __name__=='__main__': unittest.main()
