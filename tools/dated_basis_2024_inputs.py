"""Fixed 2024 quarterly archives for development transfer, never independent admission."""
import calendar
import csv
import datetime as dt
from decimal import Decimal
import io
import json
from pathlib import Path
import subprocess
import zipfile
from dated_basis_input_probe import digest, require, save

TARGETS = {
    'BTCUSD_' + day: int(dt.datetime.strptime('20' + day, '%Y%m%d')
        .replace(hour=8, tzinfo=dt.timezone.utc).timestamp() * 1000)
    for day in ('240329', '240628', '240927', '241227')
}

MAX_RESPONSE = 8388608
MAX_UNCOMPRESSED = 16777216
BAR_MS = 300000


HORIZONS = (30, 60, 90)
DAY_MS = 86400000


def requests():
    """Fixed union of months for the longest horizon; no caller-chosen URLs."""
    result = {}
    for symbol, expiry in TARGETS.items():
        first = dt.datetime.fromtimestamp((expiry-90*DAY_MS-BAR_MS)/1000, dt.timezone.utc)
        last = dt.datetime.fromtimestamp((expiry+3600000)/1000, dt.timezone.utc)
        cursor = first.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cursor <= last:
            month = cursor.strftime('%Y-%m')
            for kind in ('spot', 'future', 'mark'):
                instrument = 'BTCUSDT' if kind == 'spot' else symbol
                prefix = ('spot/monthly/klines' if kind == 'spot' else
                          'futures/cm/monthly/' + ('klines' if kind == 'future' else 'markPriceKlines'))
                filename = instrument+'-5m-'+month+'.zip'
                url = 'https://data.binance.vision/data/'+prefix+'/'+instrument+'/5m/'+filename
                result[url] = dict(symbol=instrument, kind=kind, month=month, filename=filename, url=url)
            cursor = (cursor.replace(year=cursor.year+1, month=1) if cursor.month==12
                      else cursor.replace(month=cursor.month+1))
    return [result[url] for url in sorted(result)]


def fetch(request, folder, checksum=False):
    require(request in requests(), 'UNAPPROVED_REQUEST')
    url = request['url'] + ('.CHECKSUM' if checksum else '')
    folder = Path(folder)
    folder.mkdir(mode=0o700)
    save(folder/'request.json', dict(url=url, method='GET',
         started_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
    argv = ['curl', '--disable', '--silent', '--show-error', '--proto', '=https',
            '--connect-timeout', '10', '--max-time', '40', '--max-filesize', str(MAX_RESPONSE),
            '--output', str(folder/'body'), '--write-out', '%{http_code}', url]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=45)
        rc, out, err = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = -1, exc.stdout or b'', exc.stderr or b''
    body = (folder/'body').read_bytes() if (folder/'body').exists() else b''
    status = out.decode('ascii', errors='replace').strip()
    save(folder/'receipt.json', dict(returncode=rc, http=status, body_bytes=len(body),
         body_sha256=digest(body), stderr_sha256=digest(err),
         finished_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
    require(rc == 0, 'TRANSPORT_FAILURE_'+str(rc))
    require(len(body) <= MAX_RESPONSE, 'RESPONSE_LIMIT')
    require(status in ('200', '404', '403', '451'), 'UNEXPECTED_HTTP_'+status)
    return status, body


def decimal(value, positive=False):
    number = Decimal(value)
    require(number.is_finite() and (number > 0 if positive else number >= 0), 'FINITE_VALUE')
    return number


def parse_archive(raw, checksum, request):
    require(request in requests(), 'REQUEST_IDENTITY')
    parts = checksum.decode('ascii').strip().split()
    require(len(parts) == 2 and parts[0] == digest(raw) and
            parts[1].lstrip('*') == request['filename'], 'CHECKSUM_IDENTITY')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        require(len(members) == 1 and members[0].filename == request['filename'][:-4]+'.csv',
                'CSV_IDENTITY')
        require(not members[0].flag_bits & 1 and members[0].file_size <= MAX_UNCOMPRESSED, 'ZIP_LIMIT')
        payload = archive.read(members[0])
    rows = list(csv.reader(io.StringIO(payload.decode('utf-8-sig'))))
    require(rows, 'EMPTY_CSV')
    if rows[0][0] in ('open_time', 'open time'):
        require(len(rows[0]) == 12, 'HEADER_WIDTH')
        rows = rows[1:]
    require(rows, 'EMPTY_DATA')
    year, month = map(int, request['month'].split('-'))
    start = int(dt.datetime(year, month, 1, tzinfo=dt.timezone.utc).timestamp()*1000)
    stop = start + calendar.monthrange(year, month)[1]*86400000
    bars, previous = {}, -1
    for row in rows:
        require(len(row) == 12, 'KLINE_WIDTH')
        require(row[0].isascii() and row[0].isdigit(), 'TIME_TYPE')
        t = int(row[0])
        require(start <= t < stop and t % BAR_MS == 0 and t > previous, 'TIME_GRID_ORDER')
        end = int(row[6])
        require(t <= end <= t+BAR_MS-1, 'BAR_END_OUTSIDE_INTERVAL')
        o, h, l, c = [decimal(row[i], True) for i in (1, 2, 3, 4)]
        require(l <= min(o, c) <= max(o, c) <= h, 'OHLC_ORDER')
        values = [decimal(row[i]) for i in (5, 7, 9, 10)]
        require(row[8].isascii() and row[8].isdigit(), 'TRADES_TYPE')
        bars[t] = dict(o=str(o), h=str(h), l=str(l), c=str(c), v=str(values[0]), n=int(row[8]),
                       close_ms=end, complete=end == t+BAR_MS-1)
        previous = t
    return bars


def quality(series, expiry, days):
    """Missingness is reported, never filled; boundary absence cannot be selected away."""
    require(days in HORIZONS and not isinstance(days, bool), 'HORIZON_SCOPE')
    entry = expiry-days*86400000+BAR_MS
    decision = entry-2*BAR_MS
    exits = [expiry-8*3600000+h*3600000+d*60000 for h in (0, 8) for d in (0, 5, 60)]
    critical = {'spot': [decision, entry, *exits], 'future': [decision, entry]}
    missing = {kind: [t for t in times if t not in series[kind] or
                     not series[kind][t].get('complete', True)] for kind, times in critical.items()}
    missing_path, zero_trades, incomplete = {}, {}, {}
    for kind in ('spot', 'future', 'mark'):
        stop = max(exits)+BAR_MS if kind == 'spot' else expiry
        grid = range(entry, stop, BAR_MS)
        missing_path[kind] = sum(t not in series[kind] for t in grid)
        incomplete[kind] = sum(t in series[kind] and not series[kind][t].get('complete', True) for t in grid)
        zero_trades[kind] = (sum(t in series[kind] and
                            (Decimal(series[kind][t]['v']) == 0 or series[kind][t]['n'] == 0)
                            for t in grid) if kind != 'mark' else None)
    return dict(critical_missing=missing, missing_path=missing_path, incomplete_bars=incomplete,
                zero_trade_bars=zero_trades,
                critical_complete=not any(missing.values()),
                risk_path_observed=not any(missing_path.values()) and not any(incomplete.values()) and not any(
                    zero_trades[kind] for kind in ('spot', 'future')),
                historical_identity_admitted=False)
