"""Offline trade/kline cross-check; consistency is not historical admission."""
import csv
from decimal import Decimal
import io
import zipfile

from dated_basis_input_probe import digest, require


def archive_rows(raw, checksum, filename):
    tokens = checksum.decode('ascii').strip().split()
    require(len(tokens) == 2 and tokens[0] == digest(raw) and
            tokens[1].lstrip('*') == filename, 'ARCHIVE_CHECKSUM_IDENTITY')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        require(len(members) == 1 and members[0].filename == filename[:-4]+'.csv', 'MEMBER_IDENTITY')
        require(members[0].file_size <= 16777216 and not members[0].flag_bits & 1, 'ARCHIVE_LIMIT')
        raw_csv = archive.read(members[0])
    rows = list(csv.reader(io.StringIO(raw_csv.decode('utf-8-sig'))))
    require(bool(rows), 'EMPTY_ARCHIVE')
    if rows[0][0] in ('agg_trade_id', 'open_time'):
        rows = rows[1:]
    require(bool(rows), 'NO_DATA_ROWS')
    return rows


def integer(value):
    require(isinstance(value, str) and value.isascii() and value.isdigit(), 'INTEGER_REQUIRED')
    return int(value)


def number(value, nonzero=False):
    value = Decimal(value)
    require(value.is_finite() and (value > 0 if nonzero else value >= 0), 'NONNEGATIVE_FINITE')
    return value


def compare(trades, klines, expiry_ms):
    require(type(expiry_ms) is int and expiry_ms % 86400000 == 28800000, 'EXPIRY_08_UTC')
    start = expiry_ms - 28800000
    require(trades and klines, 'EMPTY_ROWS')
    groups, parsed, prior_id, prior_time, gaps = {}, [], -1, -1, 0
    prior_last_id = None
    for row in trades:
        require(len(row) == 7, 'TRADE_WIDTH')
        aid, first, last, timestamp = [integer(row[i]) for i in (0, 3, 4, 5)]
        price, qty = number(row[1], True), number(row[2], True)
        require(qty == qty.to_integral_value(), 'CONTRACT_QUANTITY')
        require(start <= timestamp < start+86400000, 'TRADE_DAY_OR_UNIT')
        require(aid > prior_id and timestamp >= prior_time and first <= last, 'TRADE_ORDER')
        require(row[6] in ('true', 'false'), 'MAKER_FLAG')
        if prior_last_id is not None:
            require(first > prior_last_id, 'TRADE_ID_OVERLAP')
            gaps += first-prior_last_id-1
        prior_id, prior_time, prior_last_id = aid, timestamp, last
        item = (timestamp, price, qty, last-first+1, row[6] == 'false')
        groups.setdefault(timestamp//3600000*3600000, []).append(item)
        parsed.append(item)
    candles = {}
    prior_open = -1
    for row in klines:
        require(len(row) == 12, 'KLINE_WIDTH')
        t = integer(row[0])
        require(t % 3600000 == 0 and t > prior_open, 'KLINE_GRID_ORDER')
        require(integer(row[6]) == t+3599999, 'KLINE_CLOSE_TIME')
        o, h, l, cl = [number(row[i], True) for i in (1, 2, 3, 4)]
        require(l <= min(o, cl) <= max(o, cl) <= h, 'OHLC')
        candles[t] = (o, h, l, cl, number(row[5]), integer(row[8]), number(row[9]))
        prior_open = t
    matches, mismatches = [], []
    for t, items in sorted(groups.items()):
        expect = (items[0][1], max(x[1] for x in items), min(x[1] for x in items), items[-1][1],
                  sum(x[2] for x in items), sum(x[3] for x in items),
                  sum(x[2] for x in items if x[4]))
        (matches if candles.get(t) == expect else mismatches).append(t)
    # All nonempty kline hours of this day must appear in the trade archive too.
    unmatched_nonempty = [t for t, row in candles.items() if start <= t < start+86400000 and
                         (row[4] > 0 or row[5] > 0) and t not in groups]
    post = [x for x in parsed if x[0] >= expiry_ms]
    later = [row for t, row in candles.items() if t > parsed[-1][0]]
    constant_padding = sum(row[:4] == (parsed[-1][1],)*4 and row[4] == 0 and row[5] == 0
                           and row[6] == 0 for row in later)
    return dict(agg_rows=len(parsed), underlying_trade_count=sum(x[3] for x in parsed),
                underlying_id_gaps=gaps, matched_hours=len(matches), mismatched_hours=mismatches,
                unmatched_nonempty_hours=unmatched_nonempty,
                pre_expiry_matched_hours=sum(t < expiry_ms for t in matches),
                post_expiry_agg_rows=len(post), post_expiry_trades=sum(x[3] for x in post),
                post_expiry_contracts=str(sum(x[2] for x in post)),
                last_trade_ms=parsed[-1][0], last_trade_delay_ms=parsed[-1][0]-expiry_ms,
                subsequent_kline_rows=len(later), constant_zero_volume_rows=constant_padding,
                consistency_pass=not mismatches and not unmatched_nonempty,
                historical_identity_admitted=False, settlement_price_proven=False,
                conclusion='CROSS_FORMAT_CONSISTENCY_ONLY')
