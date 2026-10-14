"""One bounded, research-only archive identity check. No PnL, keys or actuator."""
import argparse
import calendar
import csv
import datetime as dt
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path('.artifacts/cost-covered-mechanisms-20261014')
CONTRACT = Path('docs/plans/2026-10-14-cost-covered-intake.json')
PLAN = CONTRACT.with_name('2026-10-14-cost-covered-mechanism-batch.md')
PRIOR = Path('.artifacts/aave-market-recovery-20261013')
SOURCES = ('tools/cost_covered_intake.py', 'tools/test_cost_covered_intake.py')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def sha(path):
    return digest(Path(path).read_bytes())


def save(path, value):
    with Path(path).open('x') as out:
        json.dump(value, out, sort_keys=True, indent=2, allow_nan=False)
        out.write('\n')


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def guard():
    c = read(CONTRACT)
    require(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'RUNNING_GATE_REQUIRED')
    require(dt.datetime.fromisoformat(c['started_utc']) <= dt.datetime.now(dt.timezone.utc) <
            dt.datetime.fromisoformat(c['deadline_utc']), 'BATCH_DEADLINE')
    if (ROOT/'freeze.json').exists():
        f = read(ROOT/'freeze.json')
        require(all(sha(p) == h for p, h in f['inputs'].items()), 'FROZEN_INPUT_CHANGED')
    return c


def preserve():
    protected = read(ROOT/'freeze.json')['protected']
    require(all(Path(p).is_file() and sha(p) == h for p, h in protected.items()), 'OLD_EVIDENCE_CHANGED')
    return len(protected)


def freeze():
    os.umask(0o077)
    c = guard()
    old = read(PRIOR/'freeze.json')['protected']
    require(all(sha(p) == h for p, h in old.items()), 'PREDECESSOR_ALREADY_CHANGED')
    for p in PRIOR.rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:
            old[str(p)] = sha(p)
    for name in ('aave_authenticated_batch', 'aave_authenticated_input', 'aave_complete_boundaries'):
        for prefix in ('', 'test_'):
            p = Path('tools', prefix+name+'.py')
            old[str(p)] = sha(p)
    require(c['document_operations_before_freeze']+2*len(c['controls']) <= c['maximum_total_operations'], 'BUDGET')
    save(ROOT/'freeze.json', dict(at=stamp(), inputs={str(p): sha(p) for p in (CONTRACT, PLAN, *SOURCES)},
                                 protected=old))
    print(json.dumps(dict(status='INPUT_IDENTITY_CHECK_FROZEN', protected_files=preserve(), economic_attempts=0)))


def filename(control):
    require(re.fullmatch(r'BTCUSD_[0-9]{6}', control['symbol']) is not None, 'SYMBOL')
    require(re.fullmatch(r'20[0-9]{2}-[01][0-9]', control['month']) is not None, 'MONTH')
    return control['symbol']+'-1h-'+control['month']+'.zip'


def archive_stats(raw, checksum, control, maximum_uncompressed_bytes=8388608):
    name = filename(control)
    parts = checksum.decode('ascii').strip().split()
    require(len(parts) == 2 and parts[0] == digest(raw) and parts[1].lstrip('*') == name, 'CHECKSUM_IDENTITY')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        entries = archive.infolist()
        require(len(entries) == 1 and entries[0].filename == name[:-4]+'.csv', 'ZIP_MEMBER_IDENTITY')
        require(entries[0].file_size <= maximum_uncompressed_bytes, 'UNCOMPRESSED_LIMIT')
        require(not entries[0].flag_bits & 1, 'ENCRYPTED_ZIP')
        payload = archive.read(entries[0])
    rows = list(csv.reader(io.StringIO(payload.decode('utf-8-sig'))))
    if rows and rows[0][0] in ('open_time', 'open time'):
        require(len(rows[0]) == 12, 'HEADER_WIDTH')
        rows = rows[1:]
    require(bool(rows), 'EMPTY_ARCHIVE')
    year, month = map(int, control['month'].split('-'))
    start = int(dt.datetime(year, month, 1, tzinfo=dt.timezone.utc).timestamp()*1000)
    stop = start+calendar.monthrange(year, month)[1]*86400000
    expiry = int(dt.datetime.fromisoformat(control['delivery_utc']).timestamp()*1000)
    times = []
    post_active = 0
    for row in rows:
        require(len(row) == 12, 'ROW_WIDTH')
        require(re.fullmatch('[0-9]+', row[0]) is not None, 'OPEN_TIME_TYPE')
        t = int(row[0])
        require(start <= t < stop and (t-start) % 3600000 == 0, 'OPEN_TIME_GRID_OR_UNIT')
        require(int(row[6]) == t+3599999, 'CLOSE_TIME')
        values = [Decimal(row[i]) for i in (1, 2, 3, 4, 5, 7, 9, 10, 11)]
        require(all(v.is_finite() for v in values), 'NONFINITE_VALUE')
        o, h, l, cl = values[:4]
        require(0 < l <= min(o, cl) <= max(o, cl) <= h, 'OHLC_ORDER')
        require(all(v >= 0 for v in values[4:]), 'NEGATIVE_VOLUME')
        require(re.fullmatch('[0-9]+', row[8]) is not None, 'TRADES_TYPE')
        times.append(t)
        post_active += int(t >= expiry and (values[4] > 0 or int(row[8]) > 0))
    require(times == sorted(set(times)), 'ORDER_OR_DUPLICATE')
    gaps = (times[-1]-times[0])//3600000+1-len(times)
    post = sum(t >= expiry for t in times)
    return dict(symbol=control['symbol'], month=control['month'], rows=len(rows),
                first_open_ms=times[0], last_open_ms=times[-1], delivery_ms=expiry,
                internal_missing_hours=gaps, post_delivery_rows=post,
                post_delivery_active_rows=post_active, prices_or_returns_reported=False,
                decision='POST_DELIVERY_ROWS_MAPPING_UNBOUND' if post else 'IDENTITY_NOT_PROVEN_BY_TIMESTAMPS_ALONE')


def fetch(url, folder, c):
    """No redirects, credentials or retry; preserve response before classifying it."""
    require(url.startswith(c['source']), 'SOURCE_NOT_ALLOWED')
    folder.mkdir(mode=0o700)
    save(folder/'request.json', dict(url=url, started_utc=stamp()))
    body = folder/'body'
    result = subprocess.run(['curl', '--silent', '--show-error', '--proto', '=https',
        '--connect-timeout', str(c['connect_timeout_seconds']), '--max-time', str(c['request_timeout_seconds']),
        '--max-filesize', str(c['maximum_response_bytes']), '--output', str(body),
        '--write-out', '%{http_code}', url], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
    raw = body.read_bytes() if body.exists() else b''
    status = result.stdout.decode('ascii', errors='replace').strip()
    save(folder/'receipt.json', dict(finished_utc=stamp(), returncode=result.returncode,
        http=status, body_sha256=digest(raw), body_bytes=len(raw),
        stderr_sha256=digest(result.stderr)))
    require(result.returncode == 0, 'TRANSPORT_FAILED_'+str(result.returncode))
    require(len(raw) <= c['maximum_response_bytes'], 'RESPONSE_LIMIT')
    require(status in ('200', '404'), 'UNEXPECTED_HTTP_'+status)
    return status, raw


def collect():
    os.umask(0o077)
    c = guard()
    preserve()
    require(not (ROOT/'attempts').exists(), 'ONE_COLLECTION_ONLY_NO_AUTOMATIC_RETRY')
    (ROOT/'attempts').mkdir(mode=0o700)
    reports = []
    requests = 0
    for i, control in enumerate(c['controls']):
        guard()
        url = c['source']+control['symbol']+'/1h/'+filename(control)
        status, raw = fetch(url, ROOT/'attempts'/str(2*i), c)
        requests += 1
        if status == '404':
            reports.append(dict(symbol=control['symbol'], decision='HTTP_404_ARCHIVE_ABSENT'))
            continue
        status, checksum = fetch(url+'.CHECKSUM', ROOT/'attempts'/str(2*i+1), c)
        requests += 1
        require(status == '200', 'CHECKSUM_UNAVAILABLE')
        reports.append(archive_stats(raw, checksum, control, c['maximum_uncompressed_bytes']))
    save(ROOT/'archive-identity.json', dict(at=stamp(), status='FEASIBILITY_ONLY_NOT_ADMITTED',
        reports=reports, actual_gets=requests, economic_attempts=0, protected_files=preserve(),
        contract_sha256=sha(CONTRACT)))
    print(json.dumps(read(ROOT/'archive-identity.json'), sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'collect', 'preserve'))
    args = parser.parse_args()
    if args.action == 'preserve':
        print(json.dumps(dict(protected_files=preserve())))
    else:
        globals()[args.action]()
