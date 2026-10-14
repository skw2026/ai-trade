import copy
import datetime as dt
import io
import unittest
import zipfile

import cost_covered_intake as m


CONTROL = dict(symbol='BTCUSD_230929', month='2023-09', delivery_utc='2023-09-29T08:00:00+00:00')


def row(hour, day=29):
    t = int(dt.datetime(2023, 9, day, hour, tzinfo=dt.timezone.utc).timestamp()*1000)
    return [str(t), '100', '102', '99', '101', '2', str(t+3599999), '0.02', '1', '1', '0.01', '0']


def sample(rows=None, member=None):
    rows = [row(7), row(8)] if rows is None else rows
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr(member or m.filename(CONTROL)[:-4]+'.csv', '\n'.join(','.join(r) for r in rows))
    raw = out.getvalue()
    checksum = (m.digest(raw)+'  '+m.filename(CONTROL)+'\n').encode()
    return raw, checksum


class ArchiveIdentityTests(unittest.TestCase):
    def test_post_delivery_is_negative_not_silently_truncated(self):
        r = m.archive_stats(*sample(), CONTROL)
        self.assertEqual((r['rows'], r['post_delivery_rows'], r['post_delivery_active_rows']), (2, 1, 1))
        self.assertEqual(r['decision'], 'POST_DELIVERY_ROWS_MAPPING_UNBOUND')

    def test_clean_timestamps_are_not_identity_proof(self):
        r = m.archive_stats(*sample([row(6), row(7)]), CONTROL)
        self.assertEqual(r['decision'], 'IDENTITY_NOT_PROVEN_BY_TIMESTAMPS_ALONE')
        self.assertFalse(r['prices_or_returns_reported'])

    def test_expiry_boundary_is_inclusive(self):
        self.assertEqual(m.archive_stats(*sample([row(8)]), CONTROL)['post_delivery_rows'], 1)

    def test_inactive_post_delivery_rows_still_need_explanation(self):
        r = row(8); r[5] = r[8] = '0'
        result = m.archive_stats(*sample([r]), CONTROL)
        self.assertEqual(result['post_delivery_rows'], 1)
        self.assertEqual(result['post_delivery_active_rows'], 0)

    def test_checksum_bytes_and_filename_are_both_bound(self):
        raw, checksum = sample()
        for bad in (checksum.replace(b'BTCUSD', b'ETHUSD'), checksum.replace(checksum[:4], b'0000', 1)):
            with self.assertRaisesRegex(ValueError, 'CHECKSUM'): m.archive_stats(raw, bad, CONTROL)

    def test_wrong_member_stops(self):
        with self.assertRaisesRegex(ValueError, 'ZIP_MEMBER'): m.archive_stats(*sample(member='../bad.csv'), CONTROL)

    def test_size_limit_stops_before_expansion(self):
        with self.assertRaisesRegex(ValueError, 'UNCOMPRESSED'): m.archive_stats(*sample(), CONTROL, 1)

    def test_wrong_units_do_not_become_new_dates(self):
        r = row(7); r[0] = str(int(r[0])*1000)
        with self.assertRaisesRegex(ValueError, 'GRID_OR_UNIT'): m.archive_stats(*sample([r]), CONTROL)

    def test_malformed_rows_never_skipped(self):
        with self.assertRaisesRegex(ValueError, 'ROW_WIDTH'): m.archive_stats(*sample([row(7), ['bad']]), CONTROL)

    def test_duplicates_and_reverse_order_rejected(self):
        for rows in ([row(7), row(7)], [row(8), row(7)]):
            with self.assertRaisesRegex(ValueError, 'ORDER_OR_DUPLICATE'): m.archive_stats(*sample(rows), CONTROL)

    def test_holes_are_counted_not_filled(self):
        result = m.archive_stats(*sample([row(5), row(7)]), CONTROL)
        self.assertEqual(result['internal_missing_hours'], 1)

    def test_nonfinite_and_invalid_candles_rejected(self):
        for col, value in ((1, 'NaN'), (2, '100'), (3, '0'), (5, '-1'), (8, '-1'), (6, '0')):
            r = row(7); r[col] = value
            with self.assertRaises(ValueError): m.archive_stats(*sample([r]), CONTROL)

    def test_empty_is_not_valid(self):
        with self.assertRaisesRegex(ValueError, 'EMPTY'): m.archive_stats(*sample([]), CONTROL)

    def test_header_is_optional_not_arbitrary(self):
        result = m.archive_stats(*sample([['open_time']+['field']*11, row(7)]), CONTROL)
        self.assertEqual(result['rows'], 1)

    def test_filename_rejects_path_injection(self):
        c = copy.deepcopy(CONTROL); c['symbol'] = '../BTCUSD_230929'
        with self.assertRaisesRegex(ValueError, 'SYMBOL'): m.filename(c)


if __name__ == '__main__':
    unittest.main()
