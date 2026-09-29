#!/usr/bin/env python3
"""Independent raw-row/clock/revision reconstruction, not economic qualification."""
import csv
import datetime as dt
from decimal import Decimal, localcontext
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'.artifacts/eth-supply-input-contract-20260929'
SCREEN = ROOT/'.artifacts/eth-net-supply-screen-20260929'
PREVIOUS = ROOT/'.artifacts/free-eth-evidence-20260929'


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def audit_rows(result, pages, old_raw):
    """Do not import/call the production normalizer or vintage-comparison helpers."""
    bundle = result['bundle']
    manifest = bundle['manifest']
    check(bundle['snapshot_id'] == digest(json.dumps(manifest, sort_keys=True,
          separators=(',', ':'), allow_nan=False).encode()), 'SNAPSHOT_IDENTITY')
    check((manifest['provider'], manifest['asset'], manifest['metric'], manifest['unit']) ==
          ('coinmetrics', 'eth', 'SplyCur', 'ETH'), 'SOURCE_IDENTITY')
    check(len(manifest['pages']) == len(pages), 'PAGE_COUNT')
    expected, actual = {}, {r['day']:r for r in bundle['rows']}
    check(len(actual) == len(bundle['rows']), 'DUPLICATE_NORMALIZED_DAY')
    for i, (raw, receipt_raw) in enumerate(pages):
        receipt = json.loads(receipt_raw)
        provenance = manifest['pages'][i]
        check(provenance == dict(page_index=i, raw_sha256=digest(raw),
              receipt_sha256=digest(receipt_raw), url=receipt['url'],
              retrieved_utc=receipt['retrieved_utc']), 'PAGE_IDENTITY')
        for j, source in enumerate(json.loads(raw)['data']):
            day = source['time'][:10]
            check(day not in expected and day in actual, 'SOURCE_ROW_SET')
            row = actual[day]
            check(row['supply_eth'] == source.get('SplyCur'), 'DECIMAL_VALUE_CHANGED')
            check(row['page_index'] == i and row['source_row_index'] == j, 'ROW_PROVENANCE')
            check(row['period_start_utc'] == day+'T00:00:00Z', 'PERIOD_START')
            end = dt.datetime.fromisoformat(day).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
            check(row['period_end_utc'] == end.strftime('%Y-%m-%dT%H:%M:%SZ'), 'PERIOD_END')
            retrieval = dt.datetime.fromisoformat(receipt['retrieved_utc'].replace('Z', '+00:00'))
            for field in ('AssetCompletionTime', 'AssetEODCompletionTime'):
                check(row['computation_clocks'][field] == source.get(field), 'CLOCK_CHANGED')
                check(int(end.timestamp()) <= int(source[field]) <= int(retrieval.timestamp()),
                      'CLOCK_INVALID')
            check(row['issues'] == [] and row['first_publication_utc'] is None, 'ROW_LIMITATIONS')
            expected[day] = source
    check(set(expected) == set(actual), 'EXTRA_NORMALIZED_ROW')
    check(bundle['historical_publication_proven'] is False
          and bundle['vendor_atomic_snapshot_proven'] is False, 'UNPROVEN_CLAIM')

    ledger = {r['day']:r for r in result['revisions']}
    check(len(ledger) == len(result['revisions']), 'DUPLICATE_REVISION')
    # The predetermined eight-day comparison, not an after-the-fact selected subset.
    wanted = {(dt.date(2023, 9, 9)+dt.timedelta(days=i)).isoformat() for i in range(8)}
    old = {}
    for row in csv.DictReader(io.StringIO(old_raw.decode('utf-8-sig'))):
        day = row['time'][:10]
        if day in wanted:
            check(day not in old, 'DUPLICATE_OLD_DAY')
            old[day] = row
    check(set(old) == wanted, 'OLD_DAYS_MISSING')
    changed = {d for d in wanted if Decimal(old[d]['SplyCur']) != Decimal(expected[d]['SplyCur'])}
    check(set(ledger) == changed, 'REVISION_SET')
    for day in changed:
        revision, source = ledger[day], expected[day]
        check(revision['old_supply'] == old[day]['SplyCur']
              and revision['current_supply'] == source['SplyCur'], 'REVISION_VALUE')
        with localcontext() as ctx:
            ctx.prec = 160
            check(Decimal(revision['old_minus_current']) ==
                  Decimal(old[day]['SplyCur'])-Decimal(source['SplyCur']), 'REVISION_DELTA')
        check(revision['old_raw_sha256'] == digest(old_raw)
              and revision['current_snapshot_id'] == bundle['snapshot_id']
              and revision['current_page_index'] == actual[day]['page_index']
              and revision['current_source_row_index'] == actual[day]['source_row_index'],
              'REVISION_PROVENANCE')
        a, b = old[day].get('AssetEODCompletionTime'), source['AssetEODCompletionTime']
        check(revision['old_eod'] == a and revision['current_eod'] == b
              and revision['same_eod'] == (bool(a) and a == b), 'REVISION_CLOCK')
        check(revision['cause'] == 'UNKNOWN' and revision['effective_revision_time'] is None
              and revision['methodology_binding'] == 'UNRESOLVED', 'REVISION_CAUSE_UNPROVEN')
    check(result['review']['economic_use_authorized'] is False
          and result['review']['original_screen_reopened'] is False
          and result['review']['candidate_qualified'] is False, 'AUTHORITY_ESCALATION')
    if changed:
        check(result['review']['input_status'] == 'NOT_ADMITTED_UNRESOLVED_REVISION',
              'REVISION_ADMISSION')
    return dict(rows=len(actual), revision_rows=len(changed),
                same_eod_revisions=sum(r['same_eod'] for r in ledger.values()))


def main():
    baseline = json.loads((RUN/'baseline.json').read_bytes())
    for kind in ('preserved', 'sources'):
        for name, expected_hash in baseline[kind].items():
            check(digest((ROOT/name).read_bytes()) == expected_hash, 'BASELINE_CHANGED:'+name)
    result = json.loads((RUN/'result.json').read_bytes())
    pages = [((SCREEN/'raw'/f'{i:03}.json').read_bytes(),
              (SCREEN/'receipts'/f'{i:03}.json').read_bytes()) for i in (1, 2)]
    old = (PREVIOUS/'raw/vintage-csv-0.bin').read_bytes()
    stats = audit_rows(result, pages, old)
    c = json.loads((ROOT/'docs/contracts/eth_supply_snapshot_input_v1.json').read_bytes())
    manifest = result['bundle']['manifest']
    check(manifest['contract_sha256'] == baseline['sources']['docs/contracts/eth_supply_snapshot_input_v1.json']
          and manifest['normalizer_sha256'] == baseline['sources']['tools/normalize_eth_supply_snapshot.py'],
          'IMPLEMENTATION_IDENTITY')
    first = dt.date.fromisoformat(c['range']['first_day'])
    calendar = {(first+dt.timedelta(days=i)).isoformat() for i in range(c['range']['expected_days'])}
    check({r['day'] for r in result['bundle']['rows']} == calendar
          and result['bundle']['missing_days'] == [], 'COMPLETE_CALENDAR')
    check(result['new_public_reads'] == result['economic_experiments'] == 0, 'REVIEW_SCOPE')
    check(result['original_capacity'] == {'maximum':142, 'required':144, 'recomputed':False},
          'ORIGINAL_CAPACITY_CHANGED')
    old_receipt_hash = digest((PREVIOUS/'receipts/vintage-csv-0.json').read_bytes())
    check(all(r['old_receipt_sha256'] == old_receipt_hash for r in result['revisions']),
          'OLD_RECEIPT_IDENTITY')
    print(json.dumps(dict(status='INDEPENDENT_INPUT_AUDIT_PASS_NOT_ADMISSION', **stats,
                         result_sha256=digest((RUN/'result.json').read_bytes())), indent=2))


if __name__ == '__main__':
    main()
