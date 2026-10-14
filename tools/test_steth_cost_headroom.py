import copy
from decimal import Decimal
import unittest

import steth_cost_headroom as h


def reply(c, block):
    values = [dict(number=hex(block), hash='0x'+'a'*64, timestamp=hex(1710000000)), '0x6001',
              '0x'+format(int(c['coin0'], 16), '064x'), '0x'+format(int(c['coin1'], 16), '064x'),
              '0x'+format(18, '064x'), '0x'+format(10**18, '064x')]
    return [dict(jsonrpc='2.0', id=i, result=v) for i, v in enumerate(values, 1)]


class HeadroomTests(unittest.TestCase):
    def setUp(self):
        self.c = h.config()
        self.block = h.grid(self.c)[0]

    def test_fixed_whole_year_grid_no_price_selection(self):
        g = h.grid(self.c)
        self.assertEqual(len(g), 52)
        self.assertEqual(g, sorted(set(g)))
        self.assertGreater(g[0], self.c['first_block_inclusive'])
        self.assertLess(g[-1], self.c['last_block_exclusive'])
        self.assertEqual(1+len(g)*6, 313)

    def test_requests_read_only_historical_only(self):
        rs = h.requests(self.block, self.c)
        self.assertEqual(len(rs), 6)
        self.assertEqual([r['method'] for r in rs], ['eth_getBlockByNumber', 'eth_getCode']+['eth_call']*4)
        self.assertTrue(all(r['params'][-1] == hex(self.block) for r in rs[1:]))
        self.assertEqual(rs[-1]['params'][0]['data'][-64:], format(10**18, '064x'))

    def test_unordered_valid_results(self):
        r = h.decode(reply(self.c, self.block)[::-1], self.block, self.c)
        self.assertEqual(r['output_steth_wei'], 10**18)

    def test_wrong_chain_block_time_rejected_by_grid_decode(self):
        for key, val in [('number', hex(self.block+1)), ('hash', '0x'), ('timestamp', hex(1800000000))]:
            r = reply(self.c, self.block); r[0]['result'][key] = val
            with self.assertRaises(ValueError): h.decode(r, self.block, self.c)

    def test_empty_code_and_reverse_coins_rejected(self):
        for pos, value in [(1, '0x'), (2, '0x'+format(int(self.c['coin1'], 16), '064x')),
                           (4, '0x'+format(6, '064x')), (5, '0x')]:
            r = reply(self.c, self.block); r[pos]['result'] = value
            with self.assertRaises(ValueError): h.decode(r, self.block, self.c)

    def test_zero_or_oversized_quote_rejected(self):
        for n in (0, 2**128):
            r = reply(self.c, self.block); r[5]['result'] = '0x'+format(n, '064x')
            with self.assertRaisesRegex(ValueError, 'QUOTE_RANGE'): h.decode(r, self.block, self.c)

    def test_duplicate_missing_error_and_boolean_ids_rejected(self):
        good = reply(self.c, self.block)
        for r in (good[:-1], good+[good[0]], [good[0]]*6):
            with self.assertRaises(ValueError): h.envelope(r, 6)
        for field, val in [('id', True), ('error', {'code': -1})]:
            r = copy.deepcopy(good); r[0][field] = val
            with self.assertRaisesRegex(ValueError, 'BATCH_ENVELOPE'): h.envelope(r, 6)

    def test_parity_or_discounted_steth_does_not_cover_cost(self):
        r = h.measure([10**18, 10**18-100], self.c)
        self.assertEqual(r['decision'], 'REJECT_SAMPLED_REFERENCE_COST_COVERAGE')
        self.assertEqual(r['reference_cost_covered'], 0)

    def test_strict_break_even_not_pass(self):
        q = 10**18+31*10**14
        r = h.measure([q], self.c)
        self.assertEqual(Decimal(r['maximum_reference_headroom_bps']), 0)
        self.assertEqual(r['reference_cost_covered'], 0)

    def test_base_only_is_not_stress_pass(self):
        r = h.measure([10**18+40*10**14], self.c)
        self.assertEqual(r['decision'], 'INSUFFICIENT_STRESS_COST_HEADROOM')
        self.assertEqual(r['qualified_candidates'], 0)

    def test_positive_stress_cannot_create_profitability_or_g4(self):
        r = h.measure([10**18+100*10**14], self.c)
        self.assertEqual(r['decision'], 'COST_HEADROOM_PRESENT_EXIT_MODEL_NOT_ADMITTED')
        self.assertEqual(r['qualified_candidates'], 0)
        self.assertFalse(r['g4'])
        for key in ('full_strategy_pnl', 'sharpe', 'maximum_drawdown'):
            self.assertIsNone(r[key])

    def test_wei_precision_not_float_threshold(self):
        for bump, expected in [(0, 0), (1, 1)]:
            r = h.measure([10**18+31*10**14+bump], self.c)
            self.assertEqual(r['reference_cost_covered'], expected)

    def test_reference_costs_and_no_full_strategy_fixed(self):
        self.assertEqual(self.c['reference_fee_bps'], '31')
        self.assertEqual(self.c['stress_fee_bps'], '62')
        self.assertFalse(self.c['full_strategy_simulation_allowed'])
        self.assertEqual(self.c['economic_attempts_maximum'], 1)


if __name__ == '__main__':
    unittest.main()
