#!/usr/bin/env python3
import copy
import datetime as dt
import json
import unittest

import eth_net_supply_inputs as n


class SupplyInputTest(unittest.TestCase):
    def setUp(self):
        self.retrieved='2026-09-29T03:16:33Z'
        self.spec=dict(start='2022-12-24',end='2022-12-25')
        self.row=dict(asset='eth',time='2022-12-24T00:00:00.000000000Z',SplyCur='1000',
                      AssetCompletionTime='1671930000',AssetEODCompletionTime='1671930000')

    def parse(self,rows,**extra):
        return n.parse_supply(json.dumps(dict(data=rows,**extra)),self.spec,self.retrieved)

    def test_fixed_requests_cover_once(self):
        specs=n.specifications()
        self.assertEqual(len(specs),2)
        self.assertEqual(specs[0]['start'],'2022-12-24')
        self.assertEqual(specs[-1]['end'],'2025-12-27')
        self.assertEqual(dt.date.fromisoformat(specs[0]['end'])+dt.timedelta(days=1),
                         dt.date.fromisoformat(specs[1]['start']))
        for s in specs:
            self.assertTrue(s['url'].startswith(n.HOST))
            self.assertNotIn('api_key',s['url'])
            self.assertNotIn('Price',s['url'])

    def test_normalized_decimal_string(self):
        rows=self.parse([self.row])
        self.assertEqual(rows['2022-12-24']['SplyCur'],'1000')
        self.assertEqual(rows['2022-12-24']['frequency'],'1d')

    def test_null_and_missing_remain_missing(self):
        self.row['SplyCur']=None
        self.row.pop('AssetCompletionTime')
        parsed=self.parse([self.row])['2022-12-24']
        self.assertIsNone(parsed['SplyCur'])
        self.assertIsNone(parsed['AssetCompletionTime'])

    def test_duplicate_and_wrong_asset(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):
            self.parse([self.row,self.row])
        self.row['asset']='eth_cl'
        with self.assertRaisesRegex(ValueError,'WRONG_ASSET'):
            self.parse([self.row])

    def test_day_bounds(self):
        for time in ('2022-12-23T00:00:00Z','2022-12-24T01:00:00Z'):
            self.row['time']=time
            with self.subTest(time=time),self.assertRaises(ValueError):
                self.parse([self.row])

    def test_bad_decimal_and_clock(self):
        for field,bad in (('SplyCur','NaN'),('SplyCur',0),('AssetCompletionTime','0'),
                          ('AssetCompletionTime','9999999999')):
            row=copy.deepcopy(self.row)
            row[field]=bad
            with self.subTest(field=field,bad=bad),self.assertRaises(ValueError):
                self.parse([row])

    def test_revised_status_stops(self):
        self.row['SplyCur-status']='revised'
        with self.assertRaisesRegex(ValueError,'REVISION_STATUS'):
            self.parse([self.row])

    def test_pagination_not_silent_truncation(self):
        with self.assertRaisesRegex(ValueError,'PAGINATION'):
            self.parse([self.row],next_page_token='more')

    def synthetic_supply(self):
        values={}
        day=n.design.instant('2022-12-24T00:00:00Z')
        end=n.design.instant('2025-12-28T00:00:00Z')
        i=0
        while day<end:
            clock=str(int((day+n.design.DAY+dt.timedelta(hours=1)).timestamp()))
            key=day.date().isoformat()
            values[key]=dict(day=key,asset='eth',frequency='1d',SplyCur=str(100000+i),
                AssetCompletionTime=clock,AssetEODCompletionTime=clock)
            day+=n.design.DAY
            i+=1
        return values

    def test_upper_bound_never_actual_capacity_pass(self):
        r=n.clock_upper_bound(n.design.contract(),self.synthetic_supply(),self.retrieved)
        self.assertEqual(r['maximum_valid_weeks'],157)
        self.assertEqual(r['status'],'PRICE_CAPACITY_STILL_REQUIRED')
        self.assertEqual(r['price_cells'],'NOT_EVALUATED')
        self.assertTrue(all('price_direction' not in w for w in r['weeks']))

    def test_all_delayed_impossible(self):
        data=self.synthetic_supply()
        for row in data.values():
            row['AssetCompletionTime']=str(int(n.design.instant('2026-01-20T00:00:00Z').timestamp()))
        r=n.clock_upper_bound(n.design.contract(),data,self.retrieved)
        self.assertEqual(r['maximum_valid_weeks'],0)
        self.assertEqual(r['status'],'CAPACITY_INSUFFICIENT')

    def test_missing_and_tie_exclusions(self):
        data=self.synthetic_supply()
        data.pop('2022-12-24')
        data['2023-01-07']['SplyCur']=data['2022-12-31']['SplyCur']
        r=n.clock_upper_bound(n.design.contract(),data,self.retrieved)
        self.assertEqual(r['maximum_valid_weeks'],155)
        self.assertEqual(r['reasons'],{'MISSING_DAY':1,'SUPPLY_TIE':1})

    def test_json_duplicates_rejected(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):
            n.parse_supply('{"data":[],"data":[]}',self.spec,self.retrieved)


if __name__=='__main__':
    unittest.main()
