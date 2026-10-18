from copy import deepcopy
from decimal import Decimal as D, localcontext
import unittest

from dated_basis_fee_match import target_from_confirmed_fills, gross_spot_target, synthetic_path, batch
from dated_basis_execution_bridge import AssetLedger
from test_dated_basis_execution_bridge import instruments, metadata


def fills():
    return [dict(id='a',ms=10,confirmed=True,contracts='25',price='50000',fee_asset='BTC',fee_btc='.00005')]


class FeeMatchTests(unittest.TestCase):
    def test_single_fill_identity(self):
        r=target_from_confirmed_fills(fills(),10);self.assertEqual(r['target_net_btc'],D('.05005'))
    def test_multiple_inverse_prices(self):
        f=fills();f[0].update(contracts='10',price='40000',fee_btc='.000025')
        f.append(dict(f[0],id='b',contracts='15',price='60000'))
        r=target_from_confirmed_fills(f,10);self.assertEqual(r['target_net_btc'],D('.05005'))
        self.assertNotEqual(r['inverse_basis_btc'],D(2500)/52000)
    def test_future_fill_not_known_at_first_leg(self):
        with self.assertRaises(ValueError):target_from_confirmed_fills(fills(),9)
    def test_unconfirmed_rejected(self):
        f=fills();f[0]['confirmed']=False
        with self.assertRaises(ValueError):target_from_confirmed_fills(f,10)
    def test_empty_second_leg_not_hedged(self):
        with self.assertRaises(ValueError):target_from_confirmed_fills([],10)
    def test_unknown_fee_rejected(self):
        f=fills();f[0]['fee_btc']=None
        with self.assertRaises(ValueError):target_from_confirmed_fills(f,10)
    def test_non_btc_fee_rejected(self):
        f=fills();f[0]['fee_asset']='BNB'
        with self.assertRaises(ValueError):target_from_confirmed_fills(f,10)
    def test_duplicate_fill_not_double_counted(self):
        f=fills()
        with self.assertRaises(ValueError):target_from_confirmed_fills(f+f,10)
    def test_conflicting_duplicate_rejected(self):
        f=fills();g=deepcopy(f[0]);g['price']='49000'
        with self.assertRaises(ValueError):target_from_confirmed_fills(f+[g],10)
    def test_partial_fill_only_actual_face(self):
        f=fills();f[0].update(contracts='10',fee_btc='.00002')
        r=target_from_confirmed_fills(f,10);self.assertEqual(r['face_usd'],1000)
        self.assertEqual(r['target_net_btc'],D('.02002'))
    def test_negative_fee_invalid(self):
        f=fills();f[0]['fee_btc']='-.001'
        with self.assertRaises(ValueError):target_from_confirmed_fills(f,10)
    def test_fractional_contract_rejected(self):
        f=fills();f[0]['contracts']='.05'
        with self.assertRaises(ValueError):target_from_confirmed_fills(f,10)
    def test_nonfinite_price_rejected(self):
        f=fills();f[0]['price']='NaN'
        with self.assertRaises(ValueError):target_from_confirmed_fills(f,10)
    def test_no_order_authority(self):
        self.assertFalse(target_from_confirmed_fills(fills(),10)['orders_authorized'])
    def test_fee_estimate_error_is_residual(self):
        estimated=target_from_confirmed_fills(fills(),10)['target_net_btc']
        actual=fills();actual[0]['fee_btc']='.00006'
        self.assertEqual(estimated-target_from_confirmed_fills(actual,10)['target_net_btc'],D('-.00001'))
    def test_btc_fee_gross_and_rounding(self):
        r=gross_spot_target(instruments()[0],D('.05005'),'BTC')
        self.assertEqual(r['gross'],D('.05016'));self.assertGreaterEqual(r['net'],D('.05005'))
        self.assertLess(r['net']-D('.05005'),D('.00001'))
    def test_usdt_fee_does_not_consume_btc(self):
        r=gross_spot_target(instruments()[0],D('.05005'),'USDT')
        self.assertEqual(r['gross'],r['net'])
    def test_terminal_wallet_independent_formula(self):
        r=synthetic_path(*instruments(),[(25,50000)],'USDT','FEE_MATCHED',25000)
        self.assertEqual(D(r['terminal_wallet_btc']),D('2497.5')/25000)
    def test_btc_rounding_not_strict_zero(self):
        r=synthetic_path(*instruments(),[(25,50000)],'BTC','FEE_MATCHED',50000)
        self.assertGreater(D(r['residual_btc']),0)
    def test_downside_counterexample(self):
        a=synthetic_path(*instruments(),[(25,50000)],'USDT','ORIGINAL',25000)
        b=synthetic_path(*instruments(),[(25,50000)],'USDT','FEE_MATCHED',25000)
        self.assertLess(D(b['cash_plus_marked_dust_usdt']),D(a['cash_plus_marked_dust_usdt']))
    def test_upside_difference(self):
        a=synthetic_path(*instruments(),[(25,50000)],'USDT','ORIGINAL',100000)
        b=synthetic_path(*instruments(),[(25,50000)],'USDT','FEE_MATCHED',100000)
        self.assertGreater(D(b['cash_plus_marked_dust_usdt']),D(a['cash_plus_marked_dust_usdt']))
    def test_plan_not_causal(self):
        r=synthetic_path(*instruments(),[(25,50000)],'USDT','FEE_MATCHED',50000)
        self.assertFalse(r['causal_strategy']);self.assertFalse(r['actual_execution_qualified'])
    def test_second_leg_failure_keeps_spot(self):
        ledger=AssetLedger(*instruments())
        ledger.apply(dict(id='buy',ms=1,kind='SPOT_BUY',qty='.05016',price='49000',fee_asset='BTC',fee_rate='.002'))
        ledger.apply(dict(id='reject',ms=2,kind='SECOND_LEG_REJECTED'))
        self.assertGreater(ledger.state['spot_btc'],0);self.assertEqual(ledger.state['contracts'],0)
        self.assertTrue(ledger.state['uncertain'])
    def test_all_fixed_scenarios(self):
        r=batch(metadata('spot'),metadata('future'))
        self.assertEqual(r['summary']['synthetic_paths'],24)
        self.assertFalse(r['summary']['unconditional_profit_improvement'])


if __name__=='__main__':unittest.main()
