import datetime as dt
import unittest
import btc_old_output_input as m

class InputTests(unittest.TestCase):
    def raw(self,kind=0,value='1',date='2021-01-01',version='OBM v0.1.0',unit='BTC'):
        return (','.join(m.FIELDS)+'\n'+','.join([date,m.SERIES[kind],value,unit,'daily',version])+'\n').encode()
    def check(self,old=None,total=None):
        return m.admit(old or self.raw(),total or self.raw(1,'2'),[dt.date(2021,1,1)])
    def test_ratio_and_no_pit_claim(self):
        x=self.check();self.assertEqual(x['ratios']['2021-01-01'],'0.5')
        self.assertFalse(x['first_observed_at_certified'])
    def test_zero_old_is_not_zero_denominator(self):
        self.assertEqual(self.check(self.raw(value='0'))['zero_old_days'],1)
        with self.assertRaisesRegex(ValueError,'ZERO_OR_SUBSET'):self.check(total=self.raw(1,'0'))
    def test_old_subset(self):
        with self.assertRaisesRegex(ValueError,'ZERO_OR_SUBSET'):self.check(self.raw(value='3'))
    def test_nan_negative_and_infinity(self):
        for v in ('NaN','Infinity','-1'):
            with self.subTest(v=v),self.assertRaisesRegex(ValueError,'VALUE'):self.check(self.raw(value=v))
    def test_missing_date(self):
        with self.assertRaisesRegex(ValueError,'DATE_MISSING'):self.check(self.raw(date='2021-01-02'))
    def test_duplicate(self):
        r=self.raw();r+=r.splitlines()[1]+b'\n'
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.check(r)
    def test_identity(self):
        with self.assertRaisesRegex(ValueError,'IDENTITY'):self.check(self.raw(unit='BTC-days'))
    def test_release(self):
        with self.assertRaisesRegex(ValueError,'VERSION_MISMATCH'):self.check(total=self.raw(1,'2',version='v2'))
    def test_required_calendar(self):
        self.assertEqual(len(m.required_dates()),790)
        self.assertEqual(len(set(m.required_dates())),790)

if __name__=='__main__':unittest.main()
