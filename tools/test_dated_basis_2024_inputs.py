import datetime as dt
import hashlib
import io
import unittest
import zipfile
import dated_basis_2024_inputs as data


class FrozenInputTests(unittest.TestCase):
    def fixture(self):
        r=data.requests()[0]
        t=int(dt.datetime.strptime(r['month'],'%Y-%m').replace(tzinfo=dt.timezone.utc).timestamp()*1000)
        return r,t,[str(t),'100','101','99','100','2',str(t+299999),'200','1','1','100','0']
    def parse(self,r,rows,member=None,checksum=None):
        f=io.BytesIO()
        with zipfile.ZipFile(f,'w') as z:z.writestr(member or r['filename'][:-4]+'.csv','\n'.join(','.join(row) for row in rows))
        raw=f.getvalue(); check=checksum or (hashlib.sha256(raw).hexdigest()+'  '+r['filename']).encode()
        return data.parse_archive(raw,check,r)
    def test_fixed_month_union(self):
        rows=data.requests();self.assertEqual(len(rows),45)
        self.assertEqual(len({r['url'] for r in rows}),45)
        self.assertEqual({r['symbol'] for r in rows if r['kind']!='spot'},set(data.TARGETS))
    def test_targets_and_leap_year(self):
        dates=[dt.datetime.fromtimestamp(t/1000,dt.timezone.utc).isoformat() for t in data.TARGETS.values()]
        self.assertEqual(dates,['2024-03-29T08:00:00+00:00','2024-06-28T08:00:00+00:00','2024-09-27T08:00:00+00:00','2024-12-27T08:00:00+00:00'])
    def test_no_unapproved_host_or_year(self):
        self.assertTrue(all(r['url'].startswith('https://data.binance.vision/data/') and r['month']<='2024-12' for r in data.requests()))
        r,t,row=self.fixture();r=dict(r,url='https://example.com/x')
        with self.assertRaisesRegex(ValueError,'REQUEST_IDENTITY'):self.parse(r,[row])
    def test_valid_row(self):
        r,t,row=self.fixture();out=self.parse(r,[row]);self.assertTrue(out[t]['complete'])
    def test_checksum_mismatch(self):
        r,t,row=self.fixture()
        with self.assertRaisesRegex(ValueError,'CHECKSUM_IDENTITY'):self.parse(r,[row],checksum=b'bad name')
    def test_member_identity(self):
        r,t,row=self.fixture()
        with self.assertRaisesRegex(ValueError,'CSV_IDENTITY'):self.parse(r,[row],member='other.csv')
    def test_duplicate(self):
        r,t,row=self.fixture()
        with self.assertRaisesRegex(ValueError,'TIME_GRID_ORDER'):self.parse(r,[row,row])
    def test_reversed(self):
        r,t,row=self.fixture();later=list(row);later[0]=str(t+300000);later[6]=str(t+599999)
        with self.assertRaisesRegex(ValueError,'TIME_GRID_ORDER'):self.parse(r,[later,row])
    def test_wrong_month(self):
        r,t,row=self.fixture();row[0]=str(t-300000)
        with self.assertRaisesRegex(ValueError,'TIME_GRID_ORDER'):self.parse(r,[row])
    def test_bad_bounds(self):
        r,t,row=self.fixture();row[2]='98'
        with self.assertRaisesRegex(ValueError,'OHLC_ORDER'):self.parse(r,[row])
    def test_nonfinite(self):
        r,t,row=self.fixture();row[1]='NaN'
        with self.assertRaisesRegex(ValueError,'FINITE_VALUE'):self.parse(r,[row])
    def test_trade_count_type(self):
        r,t,row=self.fixture();row[8]='1.5'
        with self.assertRaisesRegex(ValueError,'TRADES_TYPE'):self.parse(r,[row])
    def test_partial_bar_not_padded(self):
        r,t,row=self.fixture();row[6]=str(t+100)
        self.assertFalse(self.parse(r,[row])[t]['complete'])
    def test_zero_volume_preserved_not_fabricated(self):
        r,t,row=self.fixture();row[5]='0';row[8]='0'
        b=self.parse(r,[row])[t];self.assertEqual(b['v'],'0');self.assertEqual(b['n'],0)


if __name__=='__main__':unittest.main()
