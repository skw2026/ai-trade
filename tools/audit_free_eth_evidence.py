#!/usr/bin/env python3
"""Offline source/clock audit; does not promote recovered data or compute returns."""
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json

from probe_free_eth_sources import ROOT, RUN, initialized, supply_summary
from screen_regional_session import need, strict, sha, save_new

DEADLINE = '2023-09-18T12:00:00Z'
COMMIT = '46f409bbd8d933e58172cab721c9de6bc0a77e50'


def positive(text):
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError):
        raise ValueError('INVALID_SUPPLY') from None
    need(value.is_finite() and value > 0, 'INVALID_SUPPLY')
    return value


def compare_vintage(raw, blob_sha, current, deadline=DEADLINE):
    need(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == blob_sha,
         'GIT_BLOB_IDENTITY')
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    fields = reader.fieldnames
    need(fields and len(fields) == len(set(fields)) and 'time' in fields and 'SplyCur' in fields,
         'CSV_COLUMNS')
    end = dt.datetime.fromisoformat(deadline.replace('Z', '+00:00'))
    wanted = {(end.date()-dt.timedelta(days=i)).isoformat() for i in range(2, 10)}
    old = {}
    for row in reader:
        day = row['time'][:10]
        if day not in wanted:
            continue
        need(day not in old and None not in row, 'CSV_TARGET_ROW')
        old[day] = {k: row.get(k) for k in ('time', 'SplyCur', 'AssetCompletionTime', 'AssetEODCompletionTime')}
    new = {}
    for row in current:
        day = row['time'][:10]
        if day in wanted:
            need(day not in new and row.get('asset') == 'eth', 'CURRENT_TARGET_ROW')
            new[day] = row
    need(set(old) == set(new) == wanted, 'TARGET_DAYS_MISSING')
    comparison = []
    for day in sorted(wanted):
        a, b = old[day], new[day]
        old_value, new_value = positive(a['SplyCur']), positive(b['SplyCur'])
        clock = a['AssetEODCompletionTime']
        valid = False
        clock_utc = None
        if clock:
            need(clock.isdigit(), 'CLOCK_UNIT_SECONDS')
            moment = dt.datetime.fromtimestamp(int(clock), dt.timezone.utc)
            period_end = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc)+dt.timedelta(days=1)
            valid = period_end <= moment <= end
            clock_utc = moment.isoformat()
        comparison.append(dict(day=day, vintage_supply=a['SplyCur'], current_supply=b['SplyCur'],
            vintage_minus_current=str(old_value-new_value),
            vintage_eod=clock, vintage_eod_utc=clock_utc, vintage_eod_within_decision=valid,
            current_eod=b.get('AssetEODCompletionTime'),
            vintage_completion=a['AssetCompletionTime']))
    return dict(rows=comparison, dates=len(comparison),
        changed_supply_days=sum(Decimal(r['vintage_minus_current']) != 0 for r in comparison),
        old_completion_column_present='AssetCompletionTime' in fields,
        old_eod_before_decision_days=sum(r['vintage_eod_within_decision'] for r in comparison),
        old_eod_missing_days=[r['day'] for r in comparison if not r['vintage_eod']],
        historical_publication_proven=False, original_contract_admitted=False)


def receipt(key):
    r = strict((RUN/'receipts'/f'{key}.json').read_bytes())
    b = (RUN/'raw'/f'{key}.bin').read_bytes()
    need(r['http_status'] == 200 and r['error'] is None and not r['oversized'], 'SOURCE_NOT_READABLE')
    need(sha(b) == r['sha256'] and len(b) == r['bytes'], 'RECEIPT_IDENTITY')
    return r, b


def main():
    baseline = initialized()
    meta_receipt, meta_raw = receipt('official-old-file-0')
    meta = strict(meta_raw)
    raw_receipt, raw = receipt('vintage-csv-0')
    _, commit_raw = receipt('fork-first-deadline')
    commit = strict(commit_raw)
    need(len(commit) == 1 and commit[0]['sha'] == COMMIT, 'COMMIT_IDENTITY')
    need(meta_receipt['url'].endswith('?ref='+COMMIT) and meta['path'] == 'csv/eth.csv'
         and meta['type'] == 'file' and meta['size'] == len(raw), 'METADATA_IDENTITY')
    need(meta['download_url'] == raw_receipt['url'] ==
         'https://raw.githubusercontent.com/coinmetrics/data/'+COMMIT+'/csv/eth.csv', 'OFFICIAL_DOWNLOAD')
    current = strict((ROOT/'.artifacts/eth-net-supply-screen-20260929/raw/001.json').read_bytes())['data']
    comparison = compare_vintage(raw, meta['sha'], current)
    _, supply_raw = receipt('ultrasound-supply')
    coverage = supply_summary(supply_raw)
    # Independent day-set check, without the production summarizer's float parsing.
    points = json.loads(supply_raw, parse_float=Decimal)['since_merge']
    dates = {p['timestamp'][:10] for p in points if p['timestamp'][10:] == 'T00:00:00Z'}
    first, final = dt.date(2022, 12, 24), dt.date(2025, 12, 27)
    calendar = {(first+dt.timedelta(days=i)).isoformat() for i in range((final-first).days+1)}
    need(sorted(calendar-dates) == coverage['missing_days'], 'INDEPENDENT_COVERAGE_MISMATCH')
    receipts = [strict(p.read_bytes()) for p in sorted((RUN/'receipts').glob('*.json'))]
    need(len(receipts)+7 <= 40, 'READ_BUDGET')
    source_paths = ('tools/audit_free_eth_evidence.py', 'tools/test_audit_free_eth_evidence.py')
    result = dict(status='FREE_RECONSTRUCTED_ONLY',
        detail='HISTORICAL_CONTENT_RECOVERED_BUT_OLD_CONTRACT_NOT_ADMITTED',
        vintage_commit=COMMIT, blob_sha1=meta['sha'], raw_sha256=sha(raw),
        comparison=comparison, ultrasound_coverage=coverage,
        public_get_attempts=len(receipts), visible_document_reads=7,
        http_status_counts={str(s):sum(r['http_status']==s for r in receipts)
                            for s in sorted({r['http_status'] for r in receipts}, key=str)},
        preserved_files=len(baseline['preserved']),
        preserved_gate_count=sum(p.endswith('validation-state.json') or p.endswith('validation-gate/state.json')
                                 for p in baseline['preserved']),
        validator_sha256={p:sha((ROOT/p).read_bytes()) for p in source_paths},
        original_screen_status='CAPACITY_INSUFFICIENT', original_max_weeks=142, required_weeks=144,
        candidate_status='NO_QUALIFIED_CANDIDATE', economic_experiments=0,
        historical_publication_proven=False, old_gates_reset=False)
    save_new(RUN/'result.json', result)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
