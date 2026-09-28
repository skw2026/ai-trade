import copy
import datetime as dt
import json
import unittest
from unittest.mock import patch

import attention_capacity as a


class CapacityTests(unittest.TestCase):
    def sample(self):
        s = dict(start=a.ms('2024-01-01T00:00:00Z'), end=a.ms('2024-01-02T00:00:00Z')-1)
        r = dict(project='en.wikipedia',article='Bitcoin',access='all-access',agent='user',
                 granularity='daily',timestamp='2024010100',views=7)
        return s,r

    def test_complete_views(self):
        s,r = self.sample()
        self.assertEqual(a.views(json.dumps({'items':[r]}),s), {s['start']:7})

    def test_duplicate_views(self):
        s,r = self.sample()
        with self.assertRaisesRegex(ValueError,'BAD_VIEWS'):
            a.views(json.dumps({'items':[r,r]}),s)

    def test_wrong_agent(self):
        s,r = self.sample()
        r['agent'] = 'all-agents'
        with self.assertRaisesRegex(ValueError,'IDENTITY'):
            a.views(json.dumps({'items':[r]}),s)

    def test_boolean_views(self):
        s,r = self.sample()
        r['views'] = True
        with self.assertRaisesRegex(ValueError,'BAD_VIEWS'):
            a.views(json.dumps({'items':[r]}),s)

    def test_missing_day(self):
        s,r = self.sample()
        s['end'] += a.DAY
        with self.assertRaisesRegex(ValueError,'COVERAGE'):
            a.views(json.dumps({'items':[r]}),s)

    def test_no_endpoint_actuation(self):
        specs = a.specs(a.config())
        self.assertLessEqual(len(specs)+23, 200)
        self.assertEqual(set(s['kind'] for s in specs), {'views','trade','mark','funding'})
        self.assertTrue(all('/metrics/pageviews/' in s['url'] or '/v5/market/' in s['url'] for s in specs))

    def test_exact_grid(self):
        for kind in ('trade','mark','funding','views'):
            selected = [s for s in a.specs(a.config()) if s['kind']==kind]
            self.assertTrue(all(x['end']+1 == y['start'] for x,y in zip(selected,selected[1:])))

    def test_signals_only_use_past(self):
        c = copy.deepcopy(a.config())
        c['calendar'].update(first_entry='2024-01-04T12:00:00Z',end_exclusive='2024-01-05T12:00:00Z')
        d = a.ms('2024-01-04T00:00:00Z')
        # Deliberately supply NO holding-period prices: capacity must still work.
        v = {d-2*a.DAY:20,d-3*a.DAY:10}
        p = {d-a.DAY:['110'],d-2*a.DAY:['100']}
        rows,exclude = a.signals(v,p,c)
        self.assertFalse(exclude)
        self.assertEqual((rows[0]['side'], rows[0]['control']), (-1,-1))
        self.assertEqual(rows[0]['available_lag_hours'],36)

    def test_attention_not_price_momentum(self):
        c = copy.deepcopy(a.config())
        c['calendar'].update(first_entry='2024-01-04T12:00:00Z',end_exclusive='2024-01-05T12:00:00Z')
        d = a.ms('2024-01-04T00:00:00Z')
        rows,_ = a.signals({d-2*a.DAY:10,d-3*a.DAY:20}, {d-a.DAY:['110'],d-2*a.DAY:['100']},c)
        self.assertEqual((rows[0]['side'],rows[0]['control']),(1,-1))

    def test_incident_excluded_before_price_read(self):
        c = copy.deepcopy(a.config())
        c['calendar'].update(first_entry='2022-01-29T12:00:00Z',end_exclusive='2022-01-30T12:00:00Z')
        d = a.ms('2022-01-29T00:00:00Z')
        rows,excluded = a.signals({d-2*a.DAY:10,d-3*a.DAY:20},{},c)
        self.assertFalse(rows)
        self.assertEqual(excluded[0]['reason'],'KNOWN_INPUT_LOSS')

    def test_tie_excluded(self):
        c = copy.deepcopy(a.config())
        c['calendar'].update(first_entry='2024-01-04T12:00:00Z',end_exclusive='2024-01-05T12:00:00Z')
        d = a.ms('2024-01-04T00:00:00Z')
        rows,excluded = a.signals({d-2*a.DAY:10,d-3*a.DAY:10},{d-a.DAY:['110'],d-2*a.DAY:['100']},c)
        self.assertFalse(rows)
        self.assertEqual(excluded[0]['reason'],'SIGNAL_TIE')


if __name__ == '__main__':
    unittest.main()
