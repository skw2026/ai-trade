#!/usr/bin/env python3
import copy
import datetime as dt
import unittest

import audit_eth_supply_clock as a


class IndependentClockTest(unittest.TestCase):
    def setUp(self):
        self.c={'calendar':{'first_entry':'2023-01-02T12:00:00Z','last_entry':'2023-01-09T12:00:00Z',
                           'split':'2023-01-09T12:00:00Z'},
                'capacity':{'minimum_valid_weeks':2,'minimum_each_calendar_half':1}}
        self.rows=[]
        for i in range(15):
            day=dt.datetime(2022,12,24,tzinfo=dt.timezone.utc)+dt.timedelta(days=i)
            clock=str(int((day+dt.timedelta(days=1,hours=1)).timestamp()))
            self.rows.append(dict(asset='eth',time=a.iso(day),SplyCur=str(1000+i),
                                  AssetCompletionTime=clock,AssetEODCompletionTime=clock))
        self.retrieved='2026-09-29T03:16:33Z'

    def run_audit(self):
        return a.reconstruct(self.c,self.rows,self.retrieved)

    def test_full_upper_bound_not_experiment(self):
        r=self.run_audit()
        self.assertEqual(r['status'],'PRICE_CAPACITY_STILL_REQUIRED')
        self.assertEqual(r['maximum_half_counts'],[1,1])

    def test_shared_late_day_invalidates_two_weeks(self):
        # Saturday 2022-12-31 belongs to both fixed eight-day input windows.
        self.rows[7]['AssetCompletionTime']=str(int(a.ts('2023-02-01T00:00:00Z').timestamp()))
        r=self.run_audit()
        self.assertEqual(r['maximum_valid_weeks'],0)
        self.assertEqual(r['unique_decision_violating_days'],1)
        self.assertEqual(r['reasons'],{'COMPLETION_AFTER_DECISION':2})

    def test_exact_deadline_and_one_second_late(self):
        clock=int(a.ts('2023-01-02T12:00:00Z').timestamp())
        self.rows[0]['AssetCompletionTime']=str(clock)
        self.assertEqual(self.run_audit()['maximum_valid_weeks'],2)
        self.rows[0]['AssetCompletionTime']=str(clock+1)
        self.assertEqual(self.run_audit()['maximum_valid_weeks'],1)

    def test_missing_and_null(self):
        self.rows.pop(0)
        self.rows[-1]['SplyCur']=None
        r=self.run_audit()
        self.assertEqual(r['maximum_valid_weeks'],0)
        self.assertEqual(r['reasons'],{'MISSING_DAY':1,'MISSING_SUPPLY':1})

    def test_endpoint_tie(self):
        self.rows[7]['SplyCur']=self.rows[0]['SplyCur']
        self.assertEqual(self.run_audit()['reasons'],{'SUPPLY_TIE':1})

    def test_duplicate_raw_rejected(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):
            self.run_audit()

    def test_wrong_asset_and_bad_clock_rejected(self):
        original=copy.deepcopy(self.rows)
        self.rows[0]['asset']='btc'
        with self.assertRaisesRegex(ValueError,'IDENTITY'):
            self.run_audit()
        self.rows=original
        self.rows[0]['AssetCompletionTime']='0'
        with self.assertRaisesRegex(ValueError,'CLOCK_RANGE'):
            self.run_audit()

    def test_nonfinite_not_exclusion(self):
        self.rows[0]['SplyCur']='NaN'
        with self.assertRaisesRegex(ValueError,'FINITE'):
            self.run_audit()


if __name__=='__main__':
    unittest.main()
