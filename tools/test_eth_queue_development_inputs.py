"""Synthetic input-only tests. No market return calculation or network."""
import copy
import json
import unittest

import eth_queue_development_inputs as q


def fixture():
    return dict(publication_guard_seconds=60, maximum_age_days=2, lookback_days=7,
        decision_start_utc='2024-01-08T05:00:00Z', last_entry_utc='2024-01-10T05:00:00Z',
        terminal_utc='2024-01-11T05:00:00Z', split_utc='2024-01-10T05:00:00Z',
        group_empty_gap_days=7, minimum_usable_fraction=0.9, minimum_signal_days=1,
        minimum_separated_groups=1, minimum_signal_days_per_subperiod=1)


def history():
    return [dict(sha=str(d), available_ms=q.ms('2024-01-%02dT04:00:00Z' % d),
                 date='2024-01-%02d' % d, exit_queue=d) for d in range(1, 11)]


class QueueInputs(unittest.TestCase):
    def test_causal_asof_and_groups(self):
        result = q.fixed_schedule(history(), fixture())
        self.assertTrue(result['capacity_admitted'])
        self.assertEqual(result['signal_days'], 3)
        self.assertEqual(result['separated_groups'], 1)
        self.assertEqual(result['rows'][0]['past']['exit_queue'], 1)
        self.assertEqual(result['rows'][0]['current']['exit_queue'], 8)
        self.assertFalse(result['independent_confirmation_admitted'])

    def test_future_revision_never_backfills_old_clock(self):
        data = history()
        data[-1]['exit_queue'] = 9999
        result = q.fixed_schedule(data, fixture())
        self.assertEqual(result['rows'][0]['current']['exit_queue'], 8)
        self.assertEqual(result['rows'][1]['past']['exit_queue'], 2)

    def test_publication_guard(self):
        data = history()
        data[7]['available_ms'] = q.ms('2024-01-08T04:59:01Z')
        self.assertEqual(q.fixed_schedule(data, fixture())['rows'][0]['current']['exit_queue'], 7)
        data[7]['available_ms'] -= 1000
        self.assertEqual(q.fixed_schedule(data, fixture())['rows'][0]['current']['exit_queue'], 8)

    def test_stale_and_missing_do_not_become_zero(self):
        result = q.fixed_schedule(history()[:1], fixture())
        self.assertEqual(result['signal_days'], 0)
        self.assertFalse(result['capacity_admitted'])
        self.assertIsNone(result['rows'][0]['current'])

    def test_equal_exit_count_is_flat(self):
        data = history()
        for row in data: row['exit_queue'] = 10
        self.assertEqual(q.fixed_schedule(data, fixture())['signal_days'], 0)

    def test_decreasing_count_does_not_reverse_direction(self):
        data = history()
        for row in data: row['exit_queue'] = 100-row['exit_queue']
        self.assertEqual(q.fixed_schedule(data, fixture())['signal_days'], 0)

    def test_group_gap_boundary(self):
        c = fixture(); c['last_entry_utc'] = '2024-01-24T05:00:00Z'
        data = [dict(sha=str(d), available_ms=q.ms('2024-01-%02dT04:00:00Z' % d),
                     date='2024-01-%02d' % d, exit_queue={8: 1, 16: 1, 23: 2}.get(d, 0))
                for d in range(1, 25)]
        result = q.fixed_schedule(data, c)
        self.assertEqual(result['signal_days'], 3)
        self.assertEqual(result['separated_groups'], 2)
        self.assertEqual([r['days'] for r in result['groups']], [1, 2])

    def test_capacity_subperiod_cannot_be_empty(self):
        c = fixture(); c['split_utc'] = '2024-01-11T05:00:00Z'
        self.assertFalse(q.fixed_schedule(history(), c)['capacity_admitted'])

    def test_queue_units_and_future_date(self):
        req = dict(kind='queue', available_ms=q.ms('2024-01-02T00:00:00Z'))
        rows = [dict(date='2024-01-01', entry_queue=1, exit_queue=2)]
        self.assertEqual(q.validate(json.dumps(rows).encode(), req), rows)
        for value in (True, -1, 0.5, None):
            bad = copy.deepcopy(rows); bad[0]['exit_queue'] = value
            with self.assertRaises((ValueError, TypeError)):
                q.validate(json.dumps(bad).encode(), req)
        rows[0]['date'] = '2024-01-03'
        with self.assertRaises(ValueError): q.validate(json.dumps(rows).encode(), req)

    def test_duplicate_json_key(self):
        with self.assertRaises(ValueError): q.strict_json(b'{"a":1,"a":2}')

    def test_market_plan_is_whitelisted_and_terminal_included(self):
        c = fixture(); requests = q.market_requests(c)
        self.assertTrue(all(r['url'].startswith('https://api.bybit.com/v5/market/') for r in requests))
        self.assertTrue(all('symbol=ETHUSDT' in r['url'] for r in requests))
        self.assertEqual({r['kind'] for r in requests}, {'trade', 'mark', 'funding'})
        for kind in ('trade', 'mark'):
            final = [r for r in requests if r['kind'] == kind][-1]
            self.assertEqual(final['end'], q.ms(c['terminal_utc'])+q.HOUR-1)

    def test_market_page_grid_and_symbol(self):
        req = dict(kind='trade', start=0, end=2*q.HOUR-1, step=q.HOUR, limit=1000)
        rows = [[str(t), '100', '110', '90', '101', '10', '1000'] for t in (q.HOUR, 0)]
        data = dict(retCode=0, result=dict(category='linear', symbol='ETHUSDT', list=rows))
        self.assertEqual(len(q.validate(json.dumps(data).encode(), req)), 2)
        data['result']['list'] = rows[:1]
        with self.assertRaises(ValueError): q.validate(json.dumps(data).encode(), req)
        data['result']['list'] = rows; data['result']['symbol'] = 'BTCUSDT'
        with self.assertRaises(ValueError): q.validate(json.dumps(data).encode(), req)

    def test_funding_page_and_api_failure(self):
        req = dict(kind='funding', start=0, end=8*q.HOUR-1, step=8*q.HOUR, limit=200)
        data = dict(retCode=0, result=dict(category='linear', list=[dict(symbol='ETHUSDT',
                    fundingRateTimestamp='0', fundingRate='-0.0001')]))
        self.assertEqual(len(q.validate(json.dumps(data).encode(), req)), 1)
        data['retCode'] = 10016
        with self.assertRaises(ValueError): q.validate(json.dumps(data).encode(), req)


if __name__ == '__main__': unittest.main()
