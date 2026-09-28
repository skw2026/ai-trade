#!/usr/bin/env python3
"""Synthetic fixtures only: never load real supply/price rows or run returns."""
import copy
import datetime as dt
import unittest

import eth_net_supply_contract as n


class NetSupplyContractTest(unittest.TestCase):
    def setUp(self):
        self.c = n.contract()
        self.entry = self.c['calendar']['first_entry']
        self.retrieved = '2026-09-28T15:46:03Z'
        old = n.instant('2022-12-24T00:00:00Z')
        self.rows = []
        for i in range(8):
            day = old + i*n.DAY
            clock = str(int((day + n.DAY + dt.timedelta(hours=1)).timestamp()))
            self.rows.append(dict(asset='eth', frequency='1d', day=day.date().isoformat(),
                SplyCur=str(1000-i), AssetCompletionTime=clock, AssetEODCompletionTime=clock))
        self.prices = {'2022-12-25T00:00:00Z': '100', '2023-01-01T00:00:00Z': '110'}

    def observe(self):
        return n.observe(self.c, self.entry, self.rows, self.prices, self.retrieved)

    def synthetic_calendar(self):
        result = []
        for i, entry in enumerate(n.calendar(self.c)):
            result.append(dict(entry=entry, valid=True, reasons=[],
                scope='RECONSTRUCTED_SNAPSHOT_ONLY', historical_vintage_proven=False,
                candidate_qualified=False, supply_direction=(-1, 1)[i%2],
                price_direction=(-1, 1)[(i//2)%2]))
        return result

    def test_design_scope_and_frozen_parameters(self):
        c = self.c
        self.assertEqual(c['stage'], 'DESIGN_ONLY')
        self.assertTrue(all(v is False for v in c['authority'].values()))
        self.assertFalse(c['candidate_qualified'])
        self.assertEqual(c['supply']['completion_fields'], ['AssetCompletionTime', 'AssetEODCompletionTime'])
        self.assertEqual(c['capacity']['minimum_valid_weeks'], 144)
        self.assertEqual(c['capacity']['minimum_each_supply_price_sign_cell'], 20)
        self.assertEqual(c['capacity']['minimum_each_calendar_half'], 64)
        self.assertEqual(c['economy']['allocation'], .25)
        self.assertEqual(c['economy']['reference_drawdown_stop'], .08)
        self.assertEqual([c['cost'][k] for k in ('fee_bps_per_side','slippage_bps_per_side')], [6,5])
        self.assertEqual(c['future_batch_proposal_not_authorized']['economic_attempts_max'], 1)

    def test_complete_calendar(self):
        entries = n.calendar(self.c)
        self.assertEqual(len(entries), 157)
        self.assertEqual(entries[-1], '2025-12-29T12:00:00Z')
        self.assertEqual(sum(n.instant(e) < n.instant(self.c['calendar']['split']) for e in entries), 78)

    def test_contraction_long_not_burn_or_pit(self):
        o = self.observe()
        self.assertTrue(o['valid'])
        self.assertEqual((o['supply_direction'], o['price_direction']), (1,1))
        self.assertFalse(o['historical_vintage_proven'])
        self.assertNotIn('fee_burn', o)

    def test_expansion_short_and_price_down(self):
        self.rows[-1]['SplyCur'] = '1010'
        self.prices['2023-01-01T00:00:00Z'] = '90'
        o = self.observe()
        self.assertEqual((o['supply_direction'], o['price_direction']), (-1,-1))

    def test_decimal_precision_not_float_tie(self):
        self.rows[0]['SplyCur'] = '120000000.000000000000000001'
        self.rows[-1]['SplyCur'] = '120000000.000000000000000000'
        self.assertEqual(self.observe()['supply_direction'], 1)

    def test_ties_excluded_not_forced(self):
        self.rows[-1]['SplyCur'] = self.rows[0]['SplyCur']
        self.prices['2023-01-01T00:00:00Z'] = '100'
        self.assertEqual(self.observe()['reasons'], ['PRICE_TIE','SUPPLY_TIE'])

    def test_late_completion_any_day_or_field(self):
        for i in (0,3,7):
            for field in self.c['supply']['completion_fields']:
                with self.subTest(i=i,field=field):
                    rows = copy.deepcopy(self.rows)
                    rows[i][field] = str(int((n.instant(self.entry)+21*n.DAY).timestamp()))
                    o = n.observe(self.c,self.entry,rows,self.prices,self.retrieved)
                    self.assertEqual(o['reasons'], ['COMPLETION_AFTER_DECISION'])
                    self.assertEqual(o['entry'], self.entry)

    def test_completion_exact_boundary_allowed(self):
        self.rows[-1]['AssetEODCompletionTime'] = str(int(n.instant(self.entry).timestamp()))
        self.assertTrue(self.observe()['valid'])

    def test_missing_intermediate_day_excludes(self):
        self.rows.pop(3)
        self.assertEqual(self.observe()['reasons'], ['MISSING_DAY'])

    def test_missing_values_excluded(self):
        self.rows[2]['SplyCur'] = None
        self.rows[3].pop('AssetCompletionTime')
        self.assertEqual(self.observe()['reasons'], ['MISSING_COMPLETION','MISSING_SUPPLY'])

    def test_malformed_supply_is_not_missing(self):
        for bad in ('NaN','Infinity','0','-1','abc',True,100):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.rows[0]['SplyCur'] = bad
                self.observe()

    def test_bad_clock_is_technical_stop(self):
        for bad in ('0', '9999999999', '-1', '1.5', True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.rows[0]['AssetCompletionTime'] = bad
                self.observe()

    def test_duplicate_and_wrong_identity(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        with self.assertRaisesRegex(ValueError, 'DUPLICATE'):
            self.observe()
        self.rows.pop()
        self.rows[0]['asset'] = 'eth_cl'
        with self.assertRaisesRegex(ValueError, 'IDENTITY'):
            self.observe()

    def test_known_revision_blocks_not_silent_exclusion(self):
        self.rows[2]['known_revision'] = True
        with self.assertRaisesRegex(ValueError, 'KNOWN_REVISION'):
            self.observe()

    def test_no_entry_or_price_time_fallback(self):
        self.entry = '2023-01-03T12:00:00Z'
        with self.assertRaisesRegex(ValueError, 'OFF_CALENDAR'):
            self.observe()
        self.entry = self.c['calendar']['first_entry']
        self.prices[self.entry] = self.prices.pop('2023-01-01T00:00:00Z')
        with self.assertRaisesRegex(ValueError, 'PRICE_TIMESTAMPS'):
            self.observe()

    def test_capacity_pass_not_authority(self):
        c = n.capacity(self.c,self.synthetic_calendar())
        self.assertEqual(c['status'], 'CAPACITY_PASS_ONLY')
        self.assertEqual(c['halves'], [78,79])
        self.assertFalse(c['experiment_authorized'])
        self.assertFalse(c['candidate_qualified'])
        self.assertEqual(c['economic_effect'], 'NOT_EVALUATED')

    def test_capacity_overall_floor_boundary(self):
        rows = self.synthetic_calendar()
        for i in range(13):
            rows[i].update(valid=False,reasons=['MISSING_DAY'])
        self.assertEqual(n.capacity(self.c,rows)['status'], 'CAPACITY_PASS_ONLY')
        rows[13].update(valid=False,reasons=['MISSING_DAY'])
        self.assertEqual(n.capacity(self.c,rows)['status'], 'CAPACITY_INSUFFICIENT')

    def test_capacity_cell_and_half_floors(self):
        rows = self.synthetic_calendar()
        for o in rows:
            o['price_direction'] = 1
        self.assertEqual(n.capacity(self.c,rows)['status'], 'CAPACITY_INSUFFICIENT')
        rows = self.synthetic_calendar()
        for o in rows[:15]:
            o.update(valid=False,reasons=['MISSING_DAY'])
        relaxed_total = copy.deepcopy(self.c)
        relaxed_total['capacity']['minimum_valid_weeks'] = 0
        self.assertEqual(n.capacity(relaxed_total,rows)['status'], 'CAPACITY_INSUFFICIENT')

    def test_calendar_and_authority_forgery_rejected(self):
        rows = self.synthetic_calendar()
        with self.assertRaisesRegex(ValueError, 'CALENDAR'):
            n.capacity(self.c,rows[:-1])
        rows[0]['candidate_qualified'] = True
        with self.assertRaisesRegex(ValueError, 'QUALIFICATION'):
            n.capacity(self.c,rows)

    def test_strict_json(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
            with self.assertRaises(ValueError):
                n.strict(raw)


if __name__ == '__main__':
    unittest.main()
