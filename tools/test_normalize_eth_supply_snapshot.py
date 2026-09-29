import copy
import csv
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlencode

from normalize_eth_supply_snapshot import (ENDPOINT, canonical, normalize, revision_ledger,
                                            verdict, sha, save_new)
from audit_eth_supply_snapshot import audit_rows


class SnapshotInputTest(unittest.TestCase):
    def setUp(self):
        self.rows = []
        for i in range(8):
            day = dt.date(2023, 9, 9)+dt.timedelta(days=i)
            clock = str(int(dt.datetime.combine(day+dt.timedelta(days=1), dt.time(1),
                                tzinfo=dt.timezone.utc).timestamp()))
            self.rows.append(dict(asset='eth', time=str(day)+'T00:00:00.000000000Z',
                SplyCur='120000000.123456789012345678', AssetCompletionTime=clock,
                AssetEODCompletionTime=clock))
        self.receipt = dict(status=200, error=None, retrieved_utc='2023-09-18T12:00:00Z',
            url=ENDPOINT+'?'+urlencode(dict(assets='eth',
                metrics='SplyCur,AssetCompletionTime,AssetEODCompletionTime', frequency='1d',
                start_time='2023-09-09', end_time='2023-09-16', page_size=1000)))

    def page(self, rows=None, receipt=None):
        raw = canonical({'data':self.rows if rows is None else rows})
        r = copy.deepcopy(self.receipt if receipt is None else receipt)
        r.update(bytes=len(raw), sha256=sha(raw))
        return raw, canonical(r)

    def bundle(self, pages=None):
        return normalize([self.page()] if pages is None else pages,
                         '2023-09-09', '2023-09-16', 'c'*64, 'd'*64)

    def test_exact_decimal_and_boundary(self):
        b = self.bundle()
        self.assertEqual(b['rows'][0]['supply_eth'], self.rows[0]['SplyCur'])
        self.assertEqual(b['rows'][0]['period_end_utc'], '2023-09-10T00:00:00Z')
        self.assertEqual(b['missing_days'], [])

    def test_computation_is_not_publication_or_vendor_version(self):
        b = self.bundle()
        self.assertIsNone(b['rows'][0]['first_publication_utc'])
        self.assertFalse(b['historical_publication_proven'])
        self.assertFalse(b['vendor_atomic_snapshot_proven'])
        self.assertFalse(verdict(b, [])['economic_use_authorized'])

    def test_same_clock_changed_value_changes_snapshot(self):
        before = self.bundle()
        self.rows[0]['SplyCur'] = '120000001.123456789012345678'
        self.assertNotEqual(before['snapshot_id'], self.bundle()['snapshot_id'])

    def test_receipt_alone_changes_snapshot(self):
        before = self.bundle()
        self.receipt['retrieved_utc'] = '2023-09-19T12:00:00Z'
        self.assertNotEqual(before['snapshot_id'], self.bundle()['snapshot_id'])

    def test_changed_code_changes_snapshot(self):
        b = normalize([self.page()], '2023-09-09', '2023-09-16', 'c'*64, 'e'*64)
        self.assertNotEqual(b['snapshot_id'], self.bundle()['snapshot_id'])

    def test_raw_hash_mismatch(self):
        raw, receipt = self.page()
        with self.assertRaisesRegex(ValueError, 'RECEIPT_IDENTITY'):
            self.bundle([(raw+b' ', receipt)])

    def test_non_success_receipt(self):
        for field, value in (('status', 500), ('error', 'transport failure')):
            with self.subTest(field=field):
                receipt = dict(self.receipt, **{field:value})
                with self.assertRaisesRegex(ValueError, 'RECEIPT_IDENTITY'):
                    self.bundle([self.page(receipt=receipt)])

    def test_cross_source_and_metric_rejected(self):
        for wrong in (ENDPOINT.replace('community-api.coinmetrics.io', 'ultrasound.money'),
                      ENDPOINT.replace('https:', 'http:')):
            with self.subTest(wrong=wrong):
                r = dict(self.receipt, url=self.receipt['url'].replace(ENDPOINT, wrong))
                with self.assertRaisesRegex(ValueError, 'SOURCE_IDENTITY'):
                    self.bundle([self.page(receipt=r)])
        self.receipt['url'] = self.receipt['url'].replace('SplyCur', 'SplyCurEL')
        with self.assertRaisesRegex(ValueError, 'QUERY_IDENTITY'):
            self.bundle()

    def test_wrong_asset(self):
        self.rows[0]['asset'] = 'eth_cl'
        with self.assertRaisesRegex(ValueError, 'ROW_IDENTITY'):
            self.bundle()

    def test_duplicate_day_across_pages(self):
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_DAY'):
            self.bundle([self.page(), self.page()])

    def test_missing_day_not_filled(self):
        self.rows.pop(0)
        b = self.bundle()
        self.assertEqual(b['missing_days'], ['2023-09-09'])
        self.assertEqual(verdict(b, [])['input_status'], 'NOT_ADMITTED_INCOMPLETE_INPUT')

    def test_utc_boundary_rejects_offset_intraday_and_subseconds(self):
        for stamp in ('2023-09-09T00:00:00+08:00', '2023-09-09T01:00:00Z',
                      '2023-09-09T00:00:00.001Z'):
            with self.subTest(stamp=stamp):
                self.rows[0]['time'] = stamp
                with self.assertRaisesRegex(ValueError, 'UTC_DAY_BOUNDARY'):
                    self.bundle()

    def test_outside_range(self):
        self.rows[0]['time'] = '2023-09-08T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'ROW_OUTSIDE_PAGE'):
            self.bundle()

    def test_missing_completion_not_replaced_by_eod(self):
        del self.rows[0]['AssetCompletionTime']
        b = self.bundle()
        self.assertIsNone(b['rows'][0]['computation_clocks']['AssetCompletionTime'])
        self.assertIn('MISSING_AssetCompletionTime', b['rows'][0]['issues'])
        self.assertFalse(verdict(b, [])['economic_use_authorized'])

    def test_missing_supply_not_zero_filled(self):
        self.rows[0]['SplyCur'] = None
        b = self.bundle()
        self.assertIsNone(b['rows'][0]['supply_eth'])
        self.assertIn('MISSING_SUPPLY', b['rows'][0]['issues'])

    def test_supply_invalid_types_and_values(self):
        for value in ('NaN', 'Infinity', '-1', '0', True, 123.4, '1e8'):
            with self.subTest(value=value):
                self.rows[0]['SplyCur'] = value
                with self.assertRaisesRegex(ValueError, 'INVALID_SUPPLY'):
                    self.bundle()

    def test_clock_before_period_end_or_after_retrieval(self):
        for value in ('1', '1695124800'):
            with self.subTest(value=value):
                self.rows[0]['AssetCompletionTime'] = value
                with self.assertRaisesRegex(ValueError, 'CLOCK_OUTSIDE_PERIOD_RETRIEVAL'):
                    self.bundle()

    def test_millisecond_clock_rejected(self):
        self.rows[0]['AssetCompletionTime'] += '000'
        with self.assertRaisesRegex(ValueError, 'CLOCK_SECONDS_REQUIRED'):
            self.bundle()

    def test_duplicate_json_key(self):
        raw, _ = self.page()
        raw = raw.replace(b'"asset":"eth"', b'"asset":"eth","asset":"eth"', 1)
        receipt = dict(self.receipt, bytes=len(raw), sha256=sha(raw))
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_JSON_KEY'):
            self.bundle([(raw, canonical(receipt))])

    def revision_fixture(self):
        b = self.bundle()
        comparison, old_rows = [], []
        for row in self.rows:
            old = '123000000.123456789012345678'
            comparison.append(dict(day=row['time'][:10], vintage_supply=old,
                current_supply=row['SplyCur'], vintage_eod=row['AssetEODCompletionTime'],
                current_eod=row['AssetEODCompletionTime']))
            old_rows.append(dict(time=row['time'][:10], SplyCur=old,
                                 AssetEODCompletionTime=row['AssetEODCompletionTime']))
        s = io.StringIO()
        writer = csv.DictWriter(s, fieldnames=['time', 'SplyCur', 'AssetEODCompletionTime'])
        writer.writeheader()
        writer.writerows(old_rows)
        raw = s.getvalue().encode()
        ledger = revision_ledger({'rows':comparison}, b, sha(raw), 'e'*64)
        return dict(bundle=b, revisions=ledger, review=verdict(b, ledger)), raw

    def test_revision_rejects_economics_and_independent_audit_agrees(self):
        result, old = self.revision_fixture()
        self.assertEqual(result['review']['input_status'], 'NOT_ADMITTED_UNRESOLVED_REVISION')
        self.assertEqual(audit_rows(result, [self.page()], old)['same_eod_revisions'], 8)
        self.assertTrue(all(r['cause'] == 'UNKNOWN' for r in result['revisions']))

    def test_independent_audit_rejects_row_tampering(self):
        for field, value in (('supply_eth', '1'), ('period_end_utc', '2023-09-09T00:00:00Z'),
                             ('source_row_index', 7), ('first_publication_utc', '2023-09-10T01:00:00Z')):
            with self.subTest(field=field):
                result, old = self.revision_fixture()
                result['bundle']['rows'][0][field] = value
                with self.assertRaises(ValueError):
                    audit_rows(result, [self.page()], old)

    def test_independent_audit_rejects_revision_deletion_or_fabrication(self):
        for action in ('remove', 'cause', 'admit'):
            with self.subTest(action=action):
                result, old = self.revision_fixture()
                if action == 'remove':
                    result['revisions'].pop()
                elif action == 'cause':
                    result['revisions'][0]['cause'] = 'reviewed'
                else:
                    result['review']['economic_use_authorized'] = True
                with self.assertRaises(ValueError):
                    audit_rows(result, [self.page()], old)

    def test_append_only_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'result.json'
            save_new(path, {'result':'original'})
            with self.assertRaises(FileExistsError):
                save_new(path, {'result':'changed'})
            self.assertEqual(json.loads(path.read_bytes()), {'result':'original'})


if __name__ == '__main__':
    unittest.main()
