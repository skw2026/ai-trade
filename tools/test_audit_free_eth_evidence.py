import csv
import datetime as dt
import hashlib
import io
import unittest

from audit_free_eth_evidence import compare_vintage


class VintageAuditTest(unittest.TestCase):
    def fixture(self):
        rows, current = [], []
        for i in range(9, 1, -1):
            day = dt.date(2023, 9, 18)-dt.timedelta(days=i)
            clock = int(dt.datetime.combine(day+dt.timedelta(days=1), dt.time(1),
                                            tzinfo=dt.timezone.utc).timestamp())
            rows.append({'time':str(day), 'SplyCur':'100.000000000000000001',
                         'AssetEODCompletionTime':str(clock)})
            current.append({'asset':'eth', 'time':str(day)+'T00:00:00Z', 'SplyCur':'100',
                            'AssetEODCompletionTime':str(clock)})
        return rows, current

    def encoded(self, rows):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=['time','SplyCur','AssetEODCompletionTime'])
        writer.writeheader()
        writer.writerows(rows)
        raw = stream.getvalue().encode()
        return raw, hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()

    def test_exact_decimal_and_no_auto_admission(self):
        rows, current = self.fixture()
        result = compare_vintage(*self.encoded(rows), current)
        self.assertEqual(result['changed_supply_days'], 8)
        self.assertEqual(result['rows'][0]['vintage_minus_current'], '1E-18')
        self.assertFalse(result['original_contract_admitted'])
        self.assertFalse(result['old_completion_column_present'])

    def test_blank_clock_not_imputed(self):
        rows, current = self.fixture()
        rows[-1]['AssetEODCompletionTime'] = ''
        result = compare_vintage(*self.encoded(rows), current)
        self.assertEqual(result['old_eod_before_decision_days'], 7)
        self.assertEqual(result['old_eod_missing_days'], ['2023-09-16'])

    def test_forged_blob(self):
        rows, current = self.fixture()
        with self.assertRaisesRegex(ValueError, 'GIT_BLOB_IDENTITY'):
            compare_vintage(self.encoded(rows)[0], '0'*40, current)

    def test_duplicate_old_day(self):
        rows, current = self.fixture()
        with self.assertRaisesRegex(ValueError, 'CSV_TARGET_ROW'):
            compare_vintage(*self.encoded(rows+[rows[0]]), current)

    def test_missing_old_day(self):
        rows, current = self.fixture()
        with self.assertRaisesRegex(ValueError, 'TARGET_DAYS_MISSING'):
            compare_vintage(*self.encoded(rows[:-1]), current)

    def test_duplicate_current_day(self):
        rows, current = self.fixture()
        with self.assertRaisesRegex(ValueError, 'CURRENT_TARGET_ROW'):
            compare_vintage(*self.encoded(rows), current+[current[0]])

    def test_nonfinite(self):
        rows, current = self.fixture()
        rows[0]['SplyCur'] = 'NaN'
        with self.assertRaisesRegex(ValueError, 'INVALID_SUPPLY'):
            compare_vintage(*self.encoded(rows), current)

    def test_late_clock_not_accepted(self):
        rows, current = self.fixture()
        rows[0]['AssetEODCompletionTime'] = '2000000000'
        result = compare_vintage(*self.encoded(rows), current)
        self.assertEqual(result['old_eod_before_decision_days'], 7)


if __name__ == '__main__':
    unittest.main()
