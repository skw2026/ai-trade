import copy
import datetime as dt
import io
import unittest
import zipfile
import dated_basis_gap_probe as probe


def fixture(change=None):
    r=probe.requests()[0]
    start=int(dt.datetime.fromisoformat(r['day']).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
    rows=[[str(t),'100','101','99','100','2',str(t+299999),'200','3','1','100','0']
          for t in range(start,start+86400000,300000)]
    if change:change(rows)
    f=io.BytesIO()
    with zipfile.ZipFile(f,'w') as z:z.writestr(r['filename'][:-4]+'.csv','\n'.join(','.join(row) for row in rows))
    raw=f.getvalue();return raw,(probe.digest(raw)+'  '+r['filename']).encode(),r


class GapTests(unittest.TestCase):
    def test_fixed_twenty_get_limit(self):
        self.assertEqual(len(probe.requests()),10)
        self.assertEqual({r['day'] for r in probe.requests()},set(probe.DAYS))

    def test_full_day_checksum(self):self.assertEqual(len(probe.parse(*fixture())),288)

    def test_bad_hash_and_wrong_scope_fail(self):
        raw,checksum,r=fixture()
        with self.assertRaises(ValueError):probe.parse(raw+b'x',checksum,r)
        with self.assertRaises(ValueError):probe.parse(raw,checksum,dict(r,day='2023-08-26'))

    def test_missing_duplicate_and_nonfinite_fail(self):
        for edit in (lambda rows:rows.pop(),lambda rows:rows.__setitem__(1,rows[0]),
                     lambda rows:rows[0].__setitem__(1,'NaN')):
            with self.assertRaises(ValueError):probe.parse(*fixture(edit))

    def test_add_does_not_mutate_original(self):
        original={1:dict(c='100')};saved=copy.deepcopy(original)
        result,count=probe.reconcile(original,[{1:dict(c='100'),2:dict(c='101')}])
        self.assertEqual(original,saved);self.assertEqual(count,dict(overlap=1,added=1))
        self.assertEqual(len(result),2)

    def test_conflicting_overlap_rejected(self):
        with self.assertRaises(ValueError):probe.reconcile({1:dict(c='100')},[{1:dict(c='101')}])

    def test_zero_trade_not_fabricated(self):
        bars=probe.parse(*fixture(lambda rows:rows[0].__setitem__(8,'0')))
        self.assertEqual(bars[min(bars)]['n'],0)


if __name__=='__main__':unittest.main()
