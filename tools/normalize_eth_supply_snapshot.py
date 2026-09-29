#!/usr/bin/env python3
"""Offline, append-only input review. No network, returns or admission override."""
import argparse
import datetime as dt
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from audit_free_eth_evidence import compare_vintage, receipt as vintage_receipt
from probe_free_eth_sources import initialized as previous_initialized
from screen_regional_session import need, strict, sha, save_new

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / '.artifacts/eth-supply-input-contract-20260929'
PREVIOUS = ROOT / '.artifacts/free-eth-evidence-20260929'
SCREEN = ROOT / '.artifacts/eth-net-supply-screen-20260929'
CONTRACT = 'docs/contracts/eth_supply_snapshot_input_v1.json'
CLOCKS = ('AssetCompletionTime', 'AssetEODCompletionTime')
ENDPOINT = 'https://community-api.coinmetrics.io/v4/timeseries/asset-metrics'
SOURCES = (CONTRACT, 'docs/plans/2026-09-29-eth-supply-input-contract.md',
           'tools/normalize_eth_supply_snapshot.py', 'tools/test_normalize_eth_supply_snapshot.py',
           'tools/audit_eth_supply_snapshot.py', 'tools/audit_free_eth_evidence.py',
           'tools/probe_free_eth_sources.py', 'tools/screen_regional_session.py')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def moment(value):
    need(isinstance(value, str) and value.endswith('Z'), 'UTC_TIMESTAMP_REQUIRED')
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('UTC_TIMESTAMP_REQUIRED') from None


def normalize(pages, first_day, last_day, contract_hash, code_hash):
    """Pure normalization; each page is (raw bytes, receipt bytes). Never fills gaps."""
    first, last = dt.date.fromisoformat(first_day), dt.date.fromisoformat(last_day)
    need(first <= last and pages, 'INVALID_RANGE')
    provenance, rows, seen = [], [], set()
    for page_index, (raw, receipt_raw) in enumerate(pages):
        receipt = strict(receipt_raw)
        need(receipt.get('status') == 200 and receipt.get('error') is None
             and receipt.get('sha256') == sha(raw) and receipt.get('bytes') == len(raw),
             'RECEIPT_IDENTITY')
        parts = urlsplit(receipt['url'])
        need(parts.scheme + '://' + parts.netloc + parts.path == ENDPOINT
             and not parts.fragment, 'SOURCE_IDENTITY')
        query = parse_qs(parts.query, keep_blank_values=True)
        need(set(query) == {'assets', 'metrics', 'frequency', 'start_time', 'end_time', 'page_size'}
             and all(len(v) == 1 for v in query.values()) and query['assets'] == ['eth']
             and query['metrics'] == ['SplyCur,AssetCompletionTime,AssetEODCompletionTime']
             and query['frequency'] == ['1d'] and query['page_size'] == ['1000'], 'QUERY_IDENTITY')
        a, b = (dt.date.fromisoformat(query[k][0]) for k in ('start_time', 'end_time'))
        need(first <= a <= b <= last, 'PAGE_RANGE')
        retrieved = moment(receipt['retrieved_utc'])
        provenance.append(dict(page_index=page_index, raw_sha256=sha(raw),
                               receipt_sha256=sha(receipt_raw), url=receipt['url'],
                               retrieved_utc=receipt['retrieved_utc']))
        obj = strict(raw)
        need(isinstance(obj, dict) and set(obj) == {'data'} and isinstance(obj['data'], list),
             'PAGE_SCHEMA')
        for row_index, row in enumerate(obj['data']):
            need(isinstance(row, dict) and row.get('asset') == 'eth', 'ROW_IDENTITY')
            stamp = row.get('time')
            need(isinstance(stamp, str) and re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T00:00:00(?:\.0{1,9})?Z', stamp), 'UTC_DAY_BOUNDARY')
            start = moment(stamp)
            day = start.date()
            need(a <= day <= b, 'ROW_OUTSIDE_PAGE')
            need(day not in seen, 'DUPLICATE_DAY')
            seen.add(day)
            end = start + dt.timedelta(days=1)
            value = row.get('SplyCur')
            issues = []
            if value is None or value == '':
                value = None
                issues.append('MISSING_SUPPLY')
            else:
                need(isinstance(value, str) and len(value) <= 128
                     and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value)
                     and Decimal(value) > 0, 'INVALID_SUPPLY')
            clocks = {}
            for field in CLOCKS:
                clock = row.get(field)
                if clock is None or clock == '':
                    clocks[field] = None
                    issues.append('MISSING_' + field)
                    continue
                need(isinstance(clock, str) and re.fullmatch(r'[0-9]{1,10}', clock),
                     'CLOCK_SECONDS_REQUIRED')
                need(int(end.timestamp()) <= int(clock) <= int(retrieved.timestamp()),
                     'CLOCK_OUTSIDE_PERIOD_RETRIEVAL')
                clocks[field] = clock
            rows.append(dict(day=str(day), period_start_utc=str(day)+'T00:00:00Z',
                period_end_utc=str(end.date())+'T00:00:00Z', supply_eth=value,
                computation_clocks=clocks, first_publication_utc=None,
                issues=issues, page_index=page_index, source_row_index=row_index))
    manifest = dict(provider='coinmetrics', asset='eth', metric='SplyCur', unit='ETH',
                    contract_sha256=contract_hash, normalizer_sha256=code_hash, pages=provenance)
    snapshot_id = sha(canonical(manifest))
    calendar = {first+dt.timedelta(days=i) for i in range((last-first).days+1)}
    rows.sort(key=lambda r: r['day'])
    return dict(snapshot_id=snapshot_id, manifest=manifest, rows=rows,
                missing_days=sorted(str(d) for d in calendar-seen),
                historical_publication_proven=False, vendor_atomic_snapshot_proven=False)


def revision_ledger(comparison, bundle, old_sha, old_receipt_sha):
    indexed = {r['day']: r for r in bundle['rows']}
    revisions = []
    for item in comparison['rows']:
        row = indexed.get(item['day'])
        need(row and row['supply_eth'] == item['current_supply'], 'REVISION_ROW_BINDING')
        if Decimal(item['vintage_supply']) == Decimal(item['current_supply']):
            continue
        with localcontext() as ctx:
            ctx.prec = 160
            difference = str(Decimal(item['vintage_supply']) - Decimal(item['current_supply']))
        revisions.append(dict(day=item['day'], old_supply=item['vintage_supply'],
            current_supply=item['current_supply'], old_minus_current=difference,
            old_raw_sha256=old_sha, old_receipt_sha256=old_receipt_sha,
            current_snapshot_id=bundle['snapshot_id'], current_page_index=row['page_index'],
            current_source_row_index=row['source_row_index'], old_eod=item['vintage_eod'],
            current_eod=item['current_eod'], same_eod=bool(item['vintage_eod'])
                and item['vintage_eod'] == item['current_eod'],
            cause='UNKNOWN', effective_revision_time=None, methodology_binding='UNRESOLVED'))
    return revisions


def verdict(bundle, revisions):
    # No manual review or override parameter; this tool never authorizes economic use.
    reasons = []
    if revisions:
        reasons.append('UNRESOLVED_REVISION')
    if bundle['missing_days'] or any(r['issues'] for r in bundle['rows']):
        reasons.append('INCOMPLETE_INPUT')
    return dict(input_status='NOT_ADMITTED_'+reasons[0] if reasons else
                'NORMALIZED_ONLY_REQUIRES_SEPARATE_INPUT_REVIEW', reasons=reasons,
                economic_use_authorized=False, original_screen_reopened=False,
                candidate_qualified=False, historical_publication_proven=False)


def initialized():
    baseline = strict((RUN/'baseline.json').read_bytes())
    for name in ('preserved', 'sources'):
        need(all(sha((ROOT/p).read_bytes()) == h for p, h in baseline[name].items()),
             'FROZEN_' + name.upper() + '_CHANGED')
    return baseline


def freeze():
    previous = previous_initialized()
    c = strict((ROOT/CONTRACT).read_bytes())
    need(sha((ROOT/c['original_contract']).read_bytes()) == c['original_contract_sha256'],
         'ORIGINAL_CONTRACT_CHANGED')
    need(all(v is False for v in c['authority'].values()), 'ACTUATION_FORBIDDEN')
    paths = set(previous['preserved']) | set(previous['sources'])
    paths.update(str(p.relative_to(ROOT)) for p in PREVIOUS.rglob('*')
                 if p.is_file() and p.suffix != '.lock')
    paths.update(('docs/reviews/2026-09-29-free-eth-evidence-result.md',
                  'docs/reviews/2026-09-29-free-eth-evidence.sources.json'))
    paths.update(str(p.relative_to(ROOT)) for p in (ROOT/'.artifacts').glob('*/validation-state.json')
                 if p.parent != RUN)
    RUN.mkdir(exist_ok=True)
    save_new(RUN/'baseline.json', dict(preserved={p:sha((ROOT/p).read_bytes()) for p in sorted(paths)},
             sources={p:sha((ROOT/p).read_bytes()) for p in SOURCES}))
    print('INPUT_CONTRACT_FROZEN', len(paths))


def build():
    baseline = initialized()
    c = strict((ROOT/CONTRACT).read_bytes())
    pages = [((SCREEN/'raw'/f'{i:03}.json').read_bytes(),
              (SCREEN/'receipts'/f'{i:03}.json').read_bytes()) for i in (1, 2)]
    bundle = normalize(pages, c['range']['first_day'], c['range']['last_day'],
                       baseline['sources'][CONTRACT],
                       baseline['sources']['tools/normalize_eth_supply_snapshot.py'])
    _, old = vintage_receipt('vintage-csv-0')
    _, meta_raw = vintage_receipt('official-old-file-0')
    meta = strict(meta_raw)
    known = c['known_revision']
    need(sha(old) == known['old_sha256'] and meta['sha'] == known['old_blob_sha1'],
         'OLD_VERSION_IDENTITY')
    current = [r for raw, _ in pages for r in strict(raw)['data']]
    with localcontext() as ctx:
        ctx.prec = 160
        comparison = compare_vintage(old, meta['sha'], current, known['comparison_deadline'])
    revisions = revision_ledger(comparison, bundle, sha(old),
        sha((PREVIOUS/'receipts/vintage-csv-0.json').read_bytes()))
    # Write one complete append-only document; a partial/previous output prevents overwrite.
    result = dict(normalization_status='NORMALIZED', bundle=bundle, revisions=revisions,
        review=verdict(bundle, revisions), new_public_reads=0, economic_experiments=0,
        original_capacity={'maximum':142, 'required':144, 'recomputed':False},
        preserved_files=len(baseline['preserved']),
        preserved_gate_count=sum(p.endswith('validation-state.json') or p.endswith('validation-gate/state.json')
                                 for p in baseline['preserved']))
    save_new(RUN/'result.json', result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('bundle', 'revisions')}, indent=2))
    print('NORMALIZED_ROWS', len(bundle['rows']), 'REVISION_ROWS', len(revisions),
          'SAME_EOD_REVISIONS', sum(r['same_eod'] for r in revisions))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'build', 'verify-preserved'))
    args = parser.parse_args()
    if args.action == 'freeze':
        freeze()
    elif args.action == 'build':
        build()
    else:
        b = initialized()
        print('PRESERVED', len(b['preserved']))
