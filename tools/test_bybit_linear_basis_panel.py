"""Synthetic cases only: no network and no historical market sampling."""
import copy
from decimal import Decimal as D, localcontext
import unittest
import bybit_linear_basis_panel as panel


def payload(symbol='BTCUSDT', bid='99999', ask='100000', cts=1000):
    return dict(retCode=0, time=cts+10, result=dict(s=symbol, cts=cts, ts=cts+5,
                u=1, seq=2, b=[[bid, '1']], a=[[ask, '1']]))


def instrument(symbol=panel.SYMBOLS[0]):
    return dict(symbol=symbol, contractType='LinearFutures', baseCoin='BTC',
                quoteCoin='USDT', settleCoin='USDT', status='Trading',
                fundingInterval=0, deliveryTime=str(1000+30*86400000),
                deliveryFeeRate='0', lotSizeFilter=dict(qtyStep='.001',
                minOrderQty='.001', maxMktOrderQty='10', maxOrderQty='10', minNotionalValue='5'))


def pair(future_bid='101000'):
    return (panel.book(payload(), 'BTCUSDT', 1020),
            panel.book(payload(panel.SYMBOLS[0], future_bid, str(D(future_bid)+1)),
                       panel.SYMBOLS[0], 1020))


class PanelTests(unittest.TestCase):
    def test_valid_metadata_mixed_outer_category(self):
        p = dict(retCode=0, result=dict(category='inverse', list=[instrument()]))
        self.assertEqual(panel.metadata(p, panel.SYMBOLS[:1])[panel.SYMBOLS[0]], instrument())

    def test_wrong_metadata_types(self):
        for key, value in (('contractType', 'InverseFutures'), ('settleCoin', 'BTC'),
                           ('quoteCoin', 'USD'), ('baseCoin', 'ETH'), ('status', 'Settled'),
                           ('fundingInterval', 480), ('deliveryFeeRate', '.001')):
            with self.subTest(key=key), self.assertRaises(ValueError):
                r = instrument(); r[key] = value
                panel.metadata(dict(retCode=0, result=dict(list=[r])), panel.SYMBOLS[:1])

    def test_missing_and_duplicate_metadata(self):
        for rows in ([], [instrument(), instrument()]):
            with self.assertRaisesRegex(ValueError, 'IDENTITY'):
                panel.metadata(dict(retCode=0, result=dict(list=rows)), panel.SYMBOLS[:1])

    def test_wrong_quantity_grid(self):
        r = instrument(); r['lotSizeFilter']['qtyStep'] = '1'
        with self.assertRaisesRegex(ValueError, 'GRID'):
            panel.metadata(dict(retCode=0, result=dict(list=[r])), panel.SYMBOLS[:1])

    def test_book_requires_exact_symbol(self):
        with self.assertRaisesRegex(ValueError, 'IDENTITY'):
            panel.book(payload(), panel.SYMBOLS[0], 1020)

    def test_retcode_type_and_nonzero(self):
        for code in (False, '0', -1):
            p = payload(); p['retCode'] = code
            with self.assertRaises(ValueError): panel.book(p, 'BTCUSDT', 1020)

    def test_invalid_prices_quantities(self):
        for v in ('NaN', 'Infinity', '-1', '0', 1.1, True):
            for column in (0, 1):
                p = payload(); p['result']['b'][0][column] = v
                with self.subTest(v=v, column=column), self.assertRaises(ValueError):
                    panel.book(p, 'BTCUSDT', 1020)

    def test_duplicate_wrong_order_crossed(self):
        for rows in ([['99999','1'],['99999','2']], [['99998','1'],['99999','2']], [['100000','1']]):
            p = payload(); p['result']['b'] = rows
            with self.assertRaises(ValueError): panel.book(p, 'BTCUSDT', 1020)

    def test_empty_is_insufficient_not_crash(self):
        s, f = pair(); s['a'] = []
        r = panel.analyze_pair(s, f, instrument())
        self.assertEqual(r['status'], 'INPUT_INSUFFICIENT')
        self.assertEqual(r['cases'], [])

    def test_clock_order(self):
        p = payload(); p['result']['ts'] = 999
        with self.assertRaisesRegex(ValueError, 'CLOCK_ORDER'):
            panel.book(p, 'BTCUSDT', 1020)

    def test_freshness_boundaries(self):
        s, f = pair(); f['cts'] = 1500; f['server_ms'] = 1500; f['received_ms'] = 2500
        s['received_ms'] = 1500
        self.assertEqual(panel.pair_reasons(s, f), [])
        f['cts'] = 1501
        self.assertIn('EVENT_SKEW', panel.pair_reasons(s, f))

    def test_stale_server_and_receive(self):
        s, f = pair(); s['server_ms'] = 2001; f['received_ms'] = 999
        self.assertIn('spot:STALE_SERVER', panel.pair_reasons(s, f))
        self.assertIn('future:STALE_RECEIVE', panel.pair_reasons(s, f))

    def test_depth_sweep(self):
        self.assertEqual(panel.sweep([(D(100), D(1)), (D(101), D(2))], D(2)), D(201))
        with self.assertRaisesRegex(ValueError, 'DEPTH'):
            panel.sweep([(D(100), D(1))], D(2))

    def test_pressure_quantity_checked_separately(self):
        s, f = pair(); s['a'][0] = (D(100000), D('.02504'))
        r = panel.analyze_pair(s, f, instrument())
        self.assertEqual(r['status'], 'INPUT_INSUFFICIENT')
        self.assertEqual(len(r['cases']), 18)
        self.assertEqual(r['reasons'], ['stress:DEPTH_INSUFFICIENT'])

    def test_net_quantity_and_dust(self):
        for fee in (D('.001'), D('.002')):
            q, g, net = panel.quantities(D(100000), fee)
            self.assertEqual(q, D('.025'))
            self.assertGreaterEqual(net, q)
            self.assertLess(net-q, D('.000001'))
            self.assertEqual(g % D('.000001'), 0)

    def test_fee_free_identity(self):
        c = panel.cash_case(D(1), D(1), D(1), D(100), D(110), D(0), D(0), D(0),
                            D(100), D(30), D(2), D(0), D(0))
        self.assertEqual(D(c['profit']), D(10))

    def test_cash_ledger_and_affine_independent(self):
        s, f = pair(); r = panel.analyze_pair(s, f, instrument())
        self.assertEqual(len(r['cases']), 36)
        for c in r['cases']:
            with localcontext() as ctx:
                ctx.prec = 50
                d = {k:D(v) for k,v in c.items() if k!='cost'}
                sf, ff, slip = map(D, panel.COSTS[c['cost']])
                terminal = D(10000)-d['occupied_capital']+d['fee_buffer']
                terminal += d['initial_margin_reference']+d['future_pnl']-d['delivery_fee']
                terminal += d['spot_exit_net']
                expanded = d['q']*d['future_price']*(1-ff)-d['spot_spend']
                expanded += d['index']*(d['sold_btc']*(1+d['deviation'])*(1-slip)*(1-sf)-d['q']*(1+d['delivery_rate']))
                self.assertLess(abs(terminal-10000-d['profit']), D('1e-30'))
                self.assertLess(abs(expanded-d['profit']), D('1e-30'))

    def test_delivery_fee_and_terminal_price_exposure(self):
        r = panel.analyze_pair(*pair(), instrument())
        cases = [c for c in r['cases'] if c['cost']=='stress' and c['deviation']=='0' and c['index_multiple']=='2']
        self.assertEqual(len(cases), 2)
        self.assertAlmostEqual(D(cases[0]['profit'])-D(cases[1]['profit']),
                               D(cases[0]['q'])*D(cases[0]['index'])*D('.0005'), places=20)

    def test_margin_not_certified_by_positive_combined_cash(self):
        r = panel.analyze_pair(*pair('102000'), instrument())
        self.assertEqual(r['status'], 'CONDITIONAL_CASH_MARGIN')
        self.assertGreater(r['terminal_margin_failure_cases'], 0)
        self.assertFalse(r['qualified']); self.assertFalse(r['path_validated'])

    def test_all_nonpositive_classification(self):
        r = panel.analyze_pair(*pair('98000'), instrument())
        self.assertEqual(r['status'], 'NO_CURRENT_CASH_MARGIN')

    def test_opportunity_cost_separate_from_cash(self):
        c = panel.analyze_pair(*pair(), instrument())['cases'][0]
        self.assertLess(D(c['excess_occupied_6']), D(c['profit']))
        self.assertLess(D(c['excess_total_6']), D(c['excess_occupied_6']))
        self.assertLess(D(c['excess_occupied_12']), D(c['excess_occupied_6']))

    def test_metadata_expiry_checked_before_calculation(self):
        m = instrument(); m['deliveryTime'] = '999'
        with self.assertRaisesRegex(ValueError, 'EXPIRED'):
            panel.analyze_pair(*pair(), m)

    def test_all_eight_fixed_symbols(self):
        self.assertEqual(len(set(panel.SYMBOLS)), 8)
        self.assertEqual(panel.SYMBOLS[0], 'BTCUSDT-23OCT26')
        self.assertEqual(panel.SYMBOLS[-1], 'BTCUSDT-24SEP27')

    def test_valid_book_wrong_expiry_metadata_rejected(self):
        with self.assertRaisesRegex(ValueError, 'PAIR_IDENTITY'):
            panel.analyze_pair(*pair(), instrument(panel.SYMBOLS[1]))
        s,f=pair(); s['symbol']='ETHUSDT'
        with self.assertRaisesRegex(ValueError, 'PAIR_IDENTITY'):
            panel.analyze_pair(s,f,instrument())

    def test_dust_cannot_turn_negative_cash_into_positive(self):
        q,g,net=panel.quantities(D(100000),D('.001'))
        c=panel.cash_case(q,g,net,g*100000,D(100202),D(0),D('.001'),D(0),
                          D(100000),D(30),D(1),D(0),D(0))
        self.assertEqual(D(c['sold_btc']),q)
        self.assertGreater(D(c['remaining_btc']),0)
        self.assertLess(D(c['profit']),0)
        # Old full-net liquidation would falsely call this cash-positive.
        old_cash=D(c['profit'])+D(c['remaining_btc'])*100000*D('.999')
        self.assertGreater(old_cash,0)

    def test_terminal_cash_excludes_untradeable_residual(self):
        r=panel.analyze_pair(*pair(),instrument())
        for c in r['cases']:
            self.assertEqual(D(c['sold_btc'])+D(c['remaining_btc']),D(c['net_btc']))
            self.assertEqual(D(c['sold_btc'])%D('.000001'),0)
            self.assertLess(D(c['remaining_btc']),D('.000001'))


if __name__ == '__main__':
    unittest.main()
