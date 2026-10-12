#!/usr/bin/env python3
"""Once-only native controls for the explicitly approved complete Aave batch.

No prices, signing, accounts, transactions, automatic retries or gate resets.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROOT = Path('.artifacts/aave-complete-input-20261012')
PLAN = Path('docs/plans/2026-10-12-aave-complete-input-batch.md')
OLD = Path('.artifacts/aave-annual-input-20261012.F5cpT6/post-reset.H1PSRN')
JAN_SHA = '84186452e7f27689c37f03c17edfc42a592305138f8abe5cae1334fc97e531b5'
ENDPOINT = 'https://eth.drpc.org/'
SOURCE_REVIEW = Path('docs/reviews/2026-10-12-aave-complete-source-correction.json')
DEADLINE = dt.datetime(2026, 10, 14, 10, 52, 7, tzinfo=dt.timezone.utc)
MAX_BODY = 8 * 1024 * 1024
MAX_INPUT = 2 * 1024 ** 3


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc)


def save(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write('\n')


def hx(value, width=None):
    return isinstance(value, str) and re.fullmatch(
        r'0x[0-9a-fA-F]{%d}' % width if width else r'0x[0-9a-fA-F]+', value) is not None


def first_transactions():
    raw = (OLD / 'month-0.response.json').read_bytes()
    require(digest(raw) == JAN_SHA, 'JANUARY_SOURCE_CHANGED')
    ordered = sorted(json.loads(raw)['result'], key=lambda r: tuple(
        int(r[k], 16) for k in ('blockNumber', 'transactionIndex', 'logIndex')))
    unique = {}
    for row in ordered:
        unique.setdefault(row['transactionHash'], row)
    return list(unique.values())[:3]


def validate_receipt(value, indexed):
    require(isinstance(value, dict) and value.get('status') == '0x1', 'RECEIPT_STATUS')
    for field in ('transactionHash', 'blockNumber', 'transactionIndex'):
        require(value.get(field) == indexed[field], 'RECEIPT_' + field)
    require(hx(value.get('blockHash'), 64), 'RECEIPT_BLOCK_HASH')
    logs = value.get('logs')
    require(isinstance(logs, list) and bool(logs), 'RECEIPT_LOGS')
    previous = -1
    matches = []
    for row in logs:
        require(isinstance(row, dict), 'LOG_OBJECT')
        for field in ('transactionHash', 'blockHash', 'blockNumber', 'transactionIndex'):
            require(row.get(field) == value[field], 'LOG_IDENTITY_' + field)
        require(row.get('removed') is False, 'LOG_REMOVED_OR_UNSPECIFIED')
        require(hx(row.get('logIndex')), 'LOG_INDEX')
        current = int(row['logIndex'], 16)
        require(current > previous, 'LOG_DUPLICATE_OR_UNORDERED')
        previous = current
        require(hx(row.get('address'), 40), 'LOG_ADDRESS')
        require(isinstance(row.get('topics'), list) and len(row['topics']) <= 4
                and all(hx(t, 64) for t in row['topics']), 'LOG_TOPICS')
        require(isinstance(row.get('data'), str) and
                re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', row['data']), 'LOG_DATA')
        if current == int(indexed['logIndex'], 16):
            matches.append(row)
    require(len(matches) == 1, 'TARGET_EVENT_MISSING')
    target = matches[0]
    require(target['address'].lower() == indexed['address'].lower()
            and target['topics'] == indexed['topics'] and target['data'] == indexed['data'],
            'TARGET_EVENT_PAYLOAD_MISMATCH')
    return len(logs)


def validate_block(value, indexed, receipt):
    require(isinstance(value, dict) and value.get('number') == indexed['blockNumber']
            and value.get('hash') == receipt['blockHash'], 'BLOCK_IDENTITY')
    require(value.get('timestamp') == indexed['timeStamp'], 'BLOCK_TIMESTAMP')
    require(isinstance(value.get('transactions'), list)
            and indexed['transactionHash'] in value['transactions'], 'BLOCK_TX_MEMBERSHIP')
    index = int(indexed['transactionIndex'], 16)
    require(index < len(value['transactions']) and value['transactions'][index]
            == indexed['transactionHash'], 'BLOCK_TX_POSITION')


def protect():
    control = json.loads((ROOT / 'control.json').read_text())
    require(now() < DEADLINE, 'WALL_CLOCK_BUDGET')
    require(digest(PLAN.read_bytes()) == control['plan_sha256'], 'PLAN_CHANGED')
    protected = json.loads((ROOT / 'protected.json').read_text())
    require(all(digest(Path(p).read_bytes()) == h for p, h in protected.items()), 'OLD_EVIDENCE_CHANGED')
    return control


def initialize():
    require(not ROOT.exists(), 'BATCH_ALREADY_EXISTS')
    rows = first_transactions()
    old_manifest = Path('.artifacts/aave-alternative-access-20261012/protected.json')
    protected = json.loads(old_manifest.read_text())
    require(all(digest(Path(p).read_bytes()) == h for p, h in protected.items()), 'OLD_EVIDENCE_CHANGED')
    for directory in ('.artifacts/aave-alternative-access-20261012',
                      '.artifacts/aave-indexed-input-20261012.PANkqT',
                      '.artifacts/aave-annual-input-20261012.F5cpT6'):
        for path in Path(directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                protected[str(path)] = digest(path.read_bytes())
    for path in Path('docs/plans').glob('2026-10-12-aave-*.md'):
        if path != PLAN:
            protected[str(path)] = digest(path.read_bytes())
    for path in Path('docs/reviews').glob('2026-10-12-aave-*'):
        if path.is_file():
            protected[str(path)] = digest(path.read_bytes())
    ROOT.mkdir(mode=0o700)
    (ROOT / 'operations').mkdir(mode=0o700)
    save(ROOT / 'protected.json', protected)
    imported = sum(p.stat().st_size for p in OLD.glob('month-*.response.json'))
    imported += sum(Path(p['response_path']).stat().st_size for p in
                    json.loads((OLD / 'month-requests.json').read_text())['boundary_points'])
    save(ROOT / 'control.json', {'authorization': 'user: 接受完整批次',
         'start_utc': '2026-10-12T10:52:07Z', 'deadline_utc': DEADLINE.isoformat(),
         'max_active_seconds': 28800, 'max_public_operations': 10000,
         'max_input_bytes': MAX_INPUT, 'imported_input_bytes': imported,
         'plan_sha256': digest(PLAN.read_bytes()), 'protected_file_count': len(protected),
         'first_transactions': rows, 'old_batches_closed': True})
    print(json.dumps({'event': 'INITIALIZED', 'protected_files': len(protected),
                      'imported_input_bytes': imported, 'network_requests': 0}))


def load_result(name):
    if name == 'receipt0':
        name = 'receipt0-corrected'
    root = ROOT / 'operations' / name
    record = json.loads((root / 'record.json').read_text())
    raw = (root / 'response.json').read_bytes()
    require(record['status'] == 'CONTROL_PASS' and digest(raw) == record['response_sha256'],
            'DEPENDENCY_NOT_VERIFIED')
    return json.loads(raw)['result']


def control(name):
    config = protect()
    row = config['first_transactions'][int(name[-1]) if name.startswith('receipt') else 0]
    if name == 'block':
        prior = load_result('receipt0')
        method, params = 'eth_getBlockByNumber', [row['blockNumber'], False]
    else:
        if name == 'receipt1':
            load_result('block')
        elif name == 'receipt2':
            load_result('receipt1')
        method, params = 'eth_getTransactionReceipt', [row['transactionHash']]
    operations = ROOT / 'operations'
    require(len(list(operations.iterdir())) < 10000, 'REQUEST_BUDGET')
    used_bytes = config['imported_input_bytes'] + sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())
    require(used_bytes + MAX_BODY < MAX_INPUT, 'INPUT_SIZE_BUDGET')
    output = operations / name
    if name == 'receipt0':
        # The original null response remains immutable. Only the reviewed retry
        # can create one additional attempt for the same fixed transaction.
        original = output / 'response.json'
        require(digest(original.read_bytes()) ==
                'ca3db3fe00f86a9485058afc1daf2f0547027fc3262b67c570f89d0b4638c8c6',
                'ORIGINAL_FAILURE_CHANGED')
        gate = json.loads((ROOT / 'validation-state.json').read_text())
        require(gate['status'] == 'RUNNING' and gate['active']['failure_id'] ==
                '951a7615bcee475487d3d8b0a16bc9d2' and
                gate['active'].get('review_sha256') == digest(SOURCE_REVIEW.read_bytes()),
                'REVIEWED_SAME_GATE_RETRY_REQUIRED')
        output = operations / 'receipt0-corrected'
    require(not output.exists(), 'ALREADY_ATTEMPTED_NO_IMPLICIT_RETRY')
    output.mkdir(mode=0o700)
    request = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    save(output / 'request.json', request)
    record = {'name': name, 'endpoint': ENDPOINT, 'method': method, 'status': 'FAILED',
              'started_utc': now().isoformat(), 'source_sha256': digest(Path(__file__).read_bytes()),
              'request_sha256': digest((output / 'request.json').read_bytes()), 'retries': 0}
    started = time.monotonic()
    try:
        time.sleep(1)
        result = subprocess.run(['curl', '--silent', '--show-error', '--proto', '=https',
             '--connect-timeout', '5', '--max-time', '30', '--max-filesize', str(MAX_BODY),
             '--retry', '0', '-H', 'Content-Type: application/json', '--data-binary',
             '@' + str(output / 'request.json'), '--dump-header', str(output / 'headers.txt'),
             '--output', str(output / 'response.json'), '--write-out', '%{http_code}', ENDPOINT],
             capture_output=True, text=True, timeout=35)
        record.update(curl_returncode=result.returncode, http_status=result.stdout.strip())
        require(result.returncode == 0, 'TRANSPORT_FAILURE')
        raw = (output / 'response.json').read_bytes()
        record['response_sha256'] = digest(raw)
        record['response_bytes'] = len(raw)
        require(result.stdout.strip() == '200' and len(raw) <= MAX_BODY, 'HTTP_OR_SIZE_FAILURE')
        decoded = json.loads(raw)
        require(isinstance(decoded, dict) and decoded.get('jsonrpc') == '2.0'
                and type(decoded.get('id')) is int and decoded['id'] == 1
                and 'error' not in decoded and 'result' in decoded, 'RPC_ENVELOPE_OR_ERROR')
        if name == 'block':
            validate_block(decoded['result'], row, prior)
        else:
            record['receipt_log_count'] = validate_receipt(decoded['result'], row)
        record['status'] = 'CONTROL_PASS'
    except Exception as exc:
        record.update(error_type=type(exc).__name__, error=str(exc)[:240])
        raise
    finally:
        record.update(ended_utc=now().isoformat(), elapsed_seconds=time.monotonic() - started)
        save(output / 'record.json', record)
        print(json.dumps(record, sort_keys=True), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=('init', 'receipt0', 'block', 'receipt1', 'receipt2'))
    args = parser.parse_args()
    initialize() if args.step == 'init' else control(args.step)


if __name__ == '__main__':
    main()
