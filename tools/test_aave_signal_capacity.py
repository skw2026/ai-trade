import unittest

import aave_signal_capacity as c
import audit_aave_signal_capacity as audit


def block(offset, values, tag=''):
    return dict(time=c.START+offset, transactions=[dict(hash=tag+str(i), amount=value,
        status='WETH_BOUNDARY_UNKNOWN' if value is None else 'WETH_BOUNDARY_VERIFIED')
        for i, value in enumerate(values)])


class CausalCapacityTests(unittest.TestCase):
    def test_first_boundary_consumes_opportunity(self):
        rows = c.opportunities([block(3599, [1]), block(3601, [2], 'later')])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['exclusion'], 'LEFT_YEAR_BOUNDARY')
        self.assertFalse(rows[0]['candidate_opportunity'])

    def test_exact_left_boundary_allowed(self):
        self.assertTrue(c.opportunities([block(3600, [1])])[0]['candidate_opportunity'])

    def test_strict_observation_clock(self):
        row = c.opportunities([block(3600, [2]), block(3648, [-1], 'b'), block(3660, [-99], 'c')])[0]
        self.assertEqual(row['entry'], c.START+3720)
        self.assertEqual(row['bundle_net_sale_wei'], 1)
        self.assertEqual(len(row['bundle_transactions']), 2)

    def test_unknown_not_zero(self):
        row = c.opportunities([block(3600, [100, None])])[0]
        self.assertIsNone(row['bundle_net_sale_wei'])
        self.assertEqual(row['signal'], 'UNKNOWN')
        self.assertFalse(row['candidate_opportunity'])

    def test_future_unknown_does_not_revise_signal(self):
        first = c.opportunities([block(3600, [1])])[0]
        later = c.opportunities([block(3600, [1]), block(3700, [None], 'b')])[0]
        for key in ('entry', 'exit', 'signal', 'candidate_opportunity', 'bundle_net_sale_wei', 'bundle_transactions'):
            self.assertEqual(first[key], later[key])

    def test_adjacent_gap_not_first_gap_defines_cluster(self):
        rows = c.opportunities([block(3600, [0]), block(7200, [1], 'b'), block(10800, [1], 'c'), block(14401, [1], 'd')])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['cluster_blocks'], 3)
        self.assertFalse(rows[0]['candidate_opportunity'])
        self.assertTrue(rows[1]['candidate_opportunity'])

    def test_right_boundary_strict(self):
        offset = c.END-c.START-420
        self.assertEqual(c.opportunities([block(offset, [1])])[0]['exclusion'], 'RIGHT_YEAR_BOUNDARY')
        self.assertIsNone(c.opportunities([block(offset-1, [1])])[0]['exclusion'])

    def test_zero_and_negative_are_known_no_candidate(self):
        for values, sign in (([1, -1], 'ZERO'), ([-2, 1], 'NEGATIVE')):
            row = c.opportunities([block(3600, values)])[0]
            self.assertEqual(row['signal'], sign)
            self.assertFalse(row['candidate_opportunity'])

    def test_duplicate_and_missing_transaction_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            c.opportunities([block(3600, [1]), block(4000, [1])])
        with self.assertRaisesRegex(ValueError, 'EMPTY'):
            c.opportunities([block(3600, [])])

    def test_order_year_and_bool_amount_rejected(self):
        for blocks in ([block(-1, [1])], [block(4000, [1]), block(3600, [1], 'b')], [block(3600, [True])]):
            with self.assertRaises(ValueError): c.opportunities(blocks)

    def test_unknown_status_cannot_have_number(self):
        value = block(3600, [1])
        value['transactions'][0]['status'] = 'WETH_BOUNDARY_UNKNOWN'
        with self.assertRaisesRegex(ValueError, 'SEMANTICS'): c.opportunities([value])

    def test_both_half_years_required_but_not_profitability(self):
        blocks = [block(3600, [1]), block(c.MID-c.START, [1], 'b')]
        result = c.summarize(blocks, c.opportunities(blocks))
        self.assertTrue(result['proposal_minimum_capacity'])
        self.assertEqual(result['candidate_half_years'], {'H1': 1, 'H2': 1})
        self.assertEqual(result['profitability'], 'NOT_EVALUATED')
        self.assertEqual(result['economic_attempts'], 0)
        self.assertFalse(c.summarize(blocks[:1], c.opportunities(blocks[:1]))['proposal_minimum_capacity'])

    def test_independent_oracle_matches_causal_boundaries(self):
        blocks = [block(3599, [1]), block(3601, [None], 'b'), block(8000, [2], 'c'),
                  block(8052, [-1], 'd'), block(8112, [None], 'e'),
                  block(c.END-c.START-420, [1], 'f')]
        events = [dict(timeStamp=hex(b['time']), transactionHash=t['hash'])
                  for b in blocks for t in b['transactions']]
        flows = {t['hash']: dict(weth_dex_net_sale_wei=t['amount'])
                 for b in blocks for t in b['transactions']}
        primary = c.opportunities(blocks)
        independent = audit.reconstruct(events, flows)
        self.assertEqual(len(primary), len(independent))
        for left, right in zip(primary, independent):
            for key in right:
                self.assertEqual(sorted(left[key]) if key == 'bundle_transactions' else left[key], right[key])


if __name__ == '__main__': unittest.main()
