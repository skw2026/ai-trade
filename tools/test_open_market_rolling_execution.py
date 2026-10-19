import copy
import unittest

import audit_open_market_execution as audit
import open_market_execution as fixed
import open_market_rolling_execution as rolling

START = 1736985600000  # 2025-01-16 00:00 UTC, Thursday.
DAY, STEP = fixed.DAY, fixed.STEP


def model(direction, cutoff=START):
    return dict(arm='PV', ridge=.1, label_cutoff=cutoff,
                center=[0.] * 11, scale=[1.] * 11,
                intercept=[.01 if direction == 1 else -.01,
                           .01 if direction == -1 else -.01],
                coefficients=[[0.] * 11, [0.] * 11])


def fixture(days=4, direction=1):
    rows = [dict(timestamp=t, open=10000., high=10000., low=10000., price=10000.,
                 volume=100., mark_open=10000., mark_close=10000., mark_high=10000.,
                 mark_low=10000., funding_rate_per_interval=0.)
            for t in range(START - STEP, START + days * DAY, STEP)]
    fs = {t: dict(price=10000., latest_price_closed_at=t,
                  x={'PV': [0.] * 11}) for t in range(START, START + days * DAY, DAY)}
    schedule = [dict(t=START, model_id='initial', model=model(direction))]
    return rows, fs, schedule


class RollingExecutionTests(unittest.TestCase):
    def check_path(self, result, rows, fs):
        decisions = {p['t']: p['direction'] for p in result['predictions']}
        audit.audit(result['path'], rows, fs, decisions)
        expected = fixed.simulate(rows, fs, decisions, result['path']['start'],
                                  result['path']['end'], result['path']['latency'])
        self.assertEqual(result['path'], expected)

    def test_constant_schedule_is_exact_frozen_execution(self):
        for latency in (1, 2):
            for direction in (-1, 0, 1):
                with self.subTest(latency=latency, direction=direction):
                    rows, fs, schedule = fixture(direction=direction)
                    result = rolling.simulate(rows, fs, schedule, START, START+4*DAY, latency)
                    self.check_path(result, rows, fs)
                    self.assertIs(result['rolling'], True)
                    self.assertEqual(len(result['predictions']), 4)
                    self.assertEqual(result['governance']['applied_updates'],
                                     [dict(t=START, model_id='initial', reason='INITIAL')])

    def test_update_changes_model_without_resetting_account(self):
        rows, fs, schedule = fixture()
        schedule.append(dict(t=START+DAY, model_id='short', model=model(-1, START+DAY)))
        result = rolling.simulate(rows, fs, schedule, START, START+4*DAY)
        self.check_path(result, rows, fs)
        self.assertEqual([p['direction'] for p in result['predictions']], [1, -1, -1, -1])
        self.assertEqual(result['governance']['applied_updates'][1],
                         dict(t=START+DAY, model_id='short', reason='SCHEDULED'))
        for book in result['path']['books']:
            self.assertEqual(len(book['cycles']), 4)
            self.assertLess(book['days'][1]['equity'], book['days'][0]['equity'])

    def test_static_reference_blocks_every_candidate(self):
        rows, fs, schedule = fixture()
        schedule.append(dict(t=START+DAY, model_id='short', model=model(-1, START+DAY)))
        result = rolling.simulate(rows, fs, schedule, START, START+4*DAY, rolling=False)
        self.check_path(result, rows, fs)
        self.assertIs(result['rolling'], False)
        self.assertEqual([p['model_id'] for p in result['predictions']], ['initial']*4)
        self.assertEqual(result['governance']['blocked_updates'],
                         [dict(t=START+DAY, model_id='short', reason='STATIC_REFERENCE')])

    def test_performance_alert_blocks_same_clock_update_and_latches(self):
        rows, fs, schedule = fixture(days=22)
        # Two negative Monday closes: Jan 20 then Jan 27 (START + 11 days).
        for offset in (2, 11, 18):
            schedule.append(dict(t=START+offset*DAY, model_id='m'+str(offset),
                                 model=model(-1 if offset == 2 else 0, START+offset*DAY)))
        result = rolling.simulate(rows, fs, schedule, START, START+22*DAY)
        self.check_path(result, rows, fs)
        g = result['governance']
        self.assertEqual(g['performance_freeze_time'], START+11*DAY)
        self.assertEqual([e['model_id'] for e in g['applied_updates']], ['initial', 'm2'])
        self.assertEqual([e['reason'] for e in g['blocked_updates']], ['PERFORMANCE_FREEZE']*2)
        self.assertEqual(result['predictions'][-1]['model_id'], 'm2')
        self.assertTrue(all(b['stopped'] is None for b in result['path']['books']))

    def test_static_reference_still_records_performance_freeze(self):
        rows, fs, schedule = fixture(days=15)
        schedule.append(dict(t=START+12*DAY, model_id='cash', model=model(0, START+12*DAY)))
        result = rolling.simulate(rows, fs, schedule, START, START+15*DAY, rolling=False)
        self.check_path(result, rows, fs)
        self.assertEqual(result['governance']['performance_freeze_time'], START+11*DAY)
        self.assertEqual(result['governance']['blocked_updates'][0]['reason'], 'STATIC_REFERENCE')

    def test_hard_stop_blocks_updates_but_predictions_and_calendar_continue(self):
        rows, fs, schedule = fixture(days=4)
        rows[3]['mark_low'] = 6000.
        schedule.append(dict(t=START+DAY, model_id='short', model=model(-1, START+DAY)))
        result = rolling.simulate(rows, fs, schedule, START, START+4*DAY)
        self.check_path(result, rows, fs)
        self.assertEqual(len(result['predictions']), 4)
        self.assertEqual(result['path']['bars'], 4*DAY//STEP)
        self.assertEqual(result['governance']['blocked_updates'][0]['reason'], 'HARD_STOP')
        self.assertEqual(result['governance']['performance_freeze_time'], None)
        for book in result['path']['books']:
            self.assertEqual(sum(e['kind'] == 'entry_request' for e in book['events']), 1)

    def test_insufficient_exit_keeps_only_prediction_calendar(self):
        rows, fs, schedule = fixture(days=4)
        for row in rows:
            if row['timestamp'] >= START+DAY-3*STEP:
                row['volume'] = 0.
        schedule.append(dict(t=START+2*DAY, model_id='short', model=model(-1, START+2*DAY)))
        result = rolling.simulate(rows, fs, schedule, START, START+4*DAY)
        self.check_path(result, rows, fs)
        self.assertEqual(len(result['predictions']), 4)
        self.assertLess(result['path']['bars'], 4*DAY//STEP)
        self.assertEqual(result['governance']['blocked_updates'][0]['reason'], 'EXECUTION_INSUFFICIENT')

    def test_schedule_and_model_future_information_rejected(self):
        rows, fs, schedule = fixture()
        cases = [[], [dict(schedule[0], t=START+DAY)],
                 schedule+[copy.deepcopy(schedule[0])],
                 schedule+[dict(t=START+DAY, model_id='initial', model=model(1, START+DAY))],
                 [dict(schedule[0], model=model(1, START+STEP))],
                 [dict(schedule[0], model=dict(model(1), arm='P'))]]
        for bad in cases:
            with self.subTest(schedule=bad):
                with self.assertRaises(ValueError):
                    rolling.simulate(rows, fs, bad, START, START+4*DAY)


if __name__ == '__main__':
    unittest.main()
