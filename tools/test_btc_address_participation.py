import csv
import datetime as dt
import io
import math
import unittest

import btc_address_participation as m
import audit_btc_address_participation as audit


def raw(rows, fields=('time','AdrActCnt','AssetEODCompletionTime','PriceUSD')):
    out=io.StringIO();w=csv.writer(out);w.writerow(fields);w.writerows(rows)
    return out.getvalue().encode()


class InputTests(unittest.TestCase):
    def one(self, v='100', completion='1609545600', price='30000'):
        return raw([['2021-01-01',v,completion,price]])
    def test_log_integer(self):
        a=m.inspect_input(self.one(),['2021-01-01'])
        self.assertEqual(m.log_values(a),{'2021-01-01':math.log(100)})
        self.assertFalse(a['point_in_time_certified'])
        self.assertEqual(a['second_input_status'],'MISSING_COLUMN')
    def test_required_790_days(self): self.assertEqual(len(set(m.required_dates())),790)
    def test_invalid_values(self):
        for x in ('','NaN','Infinity','0','-1','2.5'):
            with self.subTest(x=x),self.assertRaises(ValueError): m.inspect_input(self.one(x),['2021-01-01'])
    def test_missing_day(self):
        with self.assertRaisesRegex(ValueError,'MISSING_DAY'):m.inspect_input(self.one(),['2021-01-02'])
    def test_duplicate_day(self):
        x=self.one();x+=x.splitlines()[1]+b'\n'
        with self.assertRaisesRegex(ValueError,'DATE_ORDER'):m.inspect_input(x,['2021-01-01'])
    def test_duplicate_header(self):
        with self.assertRaisesRegex(ValueError,'SCHEMA'):
            m.inspect_input(raw([['2021-01-01','1','2']],['time','AdrActCnt','AdrActCnt']),['2021-01-01'])
    def test_row_width(self):
        with self.assertRaisesRegex(ValueError,'ROW_WIDTH'):
            m.inspect_input(raw([['2021-01-01','1']]),['2021-01-01'])
    def test_future_completion_never_certifies_pit(self):
        a=m.inspect_input(self.one(completion='1900000000'),['2021-01-01'])
        self.assertEqual(a['completion_after_assumed_clock_days'],['2021-01-01'])
        self.assertFalse(a['point_in_time_certified'])
    def test_missing_completion_not_zero(self):
        a=m.inspect_input(self.one(completion=''),['2021-01-01'])
        self.assertEqual(a['missing_completion_days'],['2021-01-01'])
    def test_unused_price_cannot_change_signal(self):
        a=m.inspect_input(self.one(),['2021-01-01']);b=m.inspect_input(self.one(price='999999'),['2021-01-01'])
        self.assertEqual(m.log_values(a),m.log_values(b))
    def test_second_metric_does_not_select_by_value(self):
        a=m.inspect_input(raw([['2021-01-01','100','5']],['time','AdrActCnt','TxTfrValAdjNtv']),['2021-01-01'])
        self.assertEqual(a['selected_mechanism'],'ACTIVE_ADDRESS_PARTICIPATION')
        self.assertEqual(a['second_input_status'],'PRESENT_NOT_ADMITTED')


class FeatureTests(unittest.TestCase):
    def fixture(self):
        start=m.execution.millis('2021-01-11');end=start+m.execution.DAY
        rows=[dict(timestamp=t,price=10000+i) for i,t in enumerate(range(start-8*m.execution.DAY,end,m.execution.STEP))]
        records=[[str(m.execution.day(start)+dt.timedelta(days=i)),str(100+i*i),str(1609545600),'10'] for i in range(-40,10)]
        source=raw(records);a=m.inspect_input(source,[r[0] for r in records])
        self.completion=a['completion_times']
        return source,rows,m.log_values(a),start,end
    def features(self,rows,logs,a,b,lag=3):
        return m.features(rows,logs,a,b,lag,completion_times=self.completion)
    def test_decimal_feature_audit_and_mutation(self):
        source,rows,logs,a,b=self.fixture();f=self.features(rows,logs,a,b)
        audit.features(source,rows,f,a,b,3);f[a]['z']+=.01
        with self.assertRaisesRegex(ValueError,'AUDIT_VALUE'):audit.features(source,rows,f,a,b,3)
    def test_future_data_does_not_change_features(self):
        _,rows,logs,a,b=self.fixture();f=self.features(rows,logs,a,b)
        for r in rows:
            if r['timestamp']>=a:r['price']*=3
        for d in logs:
            if d>='2021-01-09':logs[d]+=99
        self.assertEqual(self.features(rows,logs,a,b),f)
    def test_source_day_excluded_from_scaling(self):
        _,rows,logs,a,b=self.fixture();d=dt.date(2021,1,8)
        past=[logs[str(d-dt.timedelta(days=i))] for i in range(1,29)]
        mu=sum(past)/28;sd=(sum((v-mu)**2 for v in past)/28)**.5
        weekday=sum(logs[str(d-dt.timedelta(days=i))] for i in (7,14,21,28))/4
        logs[str(d)]=weekday+sd
        self.assertAlmostEqual(self.features(rows,logs,a,b)[a]['z'],1.)
    def test_delayed_clock_and_forbidden_lag(self):
        source,rows,logs,a,b=self.fixture();f=self.features(rows,logs,a,b,4)
        self.assertEqual(f[a]['source_day'],'2021-01-07');audit.features(source,rows,f,a,b,4)
        with self.assertRaisesRegex(ValueError,'FIXED_LAG'):self.features(rows,logs,a,b,1)
    def test_constant_input_disables_mechanism(self):
        _,rows,logs,a,b=self.fixture();logs={d:1. for d in logs}
        f=self.features(rows,logs,a,b)[a];self.assertIsNone(f['z'])
        self.assertEqual(m.execution.signal(f,(1,0,1)),0)
    def test_missing_warmup_not_filled(self):
        _,rows,logs,a,b=self.fixture();del logs['2020-12-20']
        with self.assertRaises(KeyError):self.features(rows,logs,a,b)
    def test_late_completion_blocks_mechanism_not_price(self):
        _,rows,logs,a,b=self.fixture();self.completion['2021-01-08']=a+1
        f=self.features(rows,logs,a,b)[a]
        self.assertFalse(f['completion_ready']);self.assertIsNone(f['z'])
        f['r']=2.;self.assertEqual(m.execution.signal(f,(0,1,1)),1)
    def test_pure_weekday_pattern_removed(self):
        _,rows,logs,a,b=self.fixture()
        logs={d:float(dt.date.fromisoformat(d).weekday()+1) for d in logs}
        self.assertAlmostEqual(self.features(rows,logs,a,b)[a]['z'],0.)
    def test_missing_completion_blocks_not_assumed_known(self):
        _,rows,logs,a,b=self.fixture();del self.completion['2021-01-01']
        f=self.features(rows,logs,a,b)[a]
        self.assertIsNone(f['z']);self.assertIsNone(f['latest_completion_ms'])


if __name__=='__main__':unittest.main()
