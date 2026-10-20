"""Read-only local intake for the explicitly reopened frozen BTC experiment.

No network, replay, model fitting, account access or changes to old artifacts.
An incomplete input is a terminal intake outcome, not an economic rejection.
"""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

WARM = 1703980800000
START = 1704067200000
END = 1735689600000
MINUTE = 60000
BAR = 300000
MODEL = Path('.artifacts/offline-diagnostic-safety-20260930/frozen-model.json')
MODEL_SHA = 'af278f3b9612944d7bf5825ba01d910628e7604f96fa92785301464c58d9ed4c'
ARCHIVE = Path('.artifacts/cross-asset-relative-strength-20261020')
RUN = Path('.artifacts/sparse-reversal-2024-20261020')
PLAN = Path('docs/plans/2026-10-20-sparse-reversal-2024.md')


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write_new(path, obj):
    with Path(path).open('x') as out:
        json.dump(obj, out, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        out.write('\n')


def assess(start, end):
    require(type(start) is int and type(end) is int and start < end, 'INPUT_BOUNDS')
    require(start % BAR == end % BAR == 0, 'BAR_ALIGNMENT')
    complete_year = start <= START and end >= END
    complete_warmup = start <= WARM and end >= START
    missing_warmup = max(0, min(start, START)-WARM)//BAR
    return dict(status=('COVERAGE_READY_NOT_ECONOMICALLY_ADMITTED'
                        if complete_year and complete_warmup else 'INPUT_INSUFFICIENT'),
                evaluation_coverage=complete_year, warmup_coverage=complete_warmup,
                required_warmup_bars=288, missing_warmup_bars=missing_warmup,
                required_source_start_ms=WARM, evaluation_start_ms=START,
                evaluation_end_ms=END, evaluation_bars=(END-START)//BAR,
                actual_source_start_ms=start, actual_source_end_ms=end,
                economic_attempts=0, new_market_requests=0, qualification=False)


def overlapping_requests(obj):
    """Requested-range leads only, not a claim that response coverage exists."""
    todo = [obj]
    result = set()
    while todo:
        item = todo.pop()
        if isinstance(item, dict):
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
        elif isinstance(item, str) and item.startswith('https://api.bybit.com/'):
            parsed = urlsplit(item)
            if parsed.hostname != 'api.bybit.com' or parsed.path not in (
                    '/v5/market/kline', '/v5/market/mark-price-kline'):
                continue
            q = parse_qs(parsed.query)
            if (q.get('symbol') != ['BTCUSDT'] or q.get('category') != ['linear'] or
                    q.get('interval') not in (['1'], ['5'])):
                continue
            try:
                a, b = int(q['start'][0]), int(q['end'][0])
            except (KeyError, ValueError, IndexError):
                continue
            if a < START-3600000 and b >= WARM:
                result.add(item)
    return sorted(result)


def metadata_inventory(root):
    selected = []
    for folder in Path(root).iterdir():
        if not folder.is_dir() or folder.name == RUN.name:
            continue
        selected.extend(p for p in folder.glob('*.json') if any(
            s in p.name for s in ('manifest', 'freeze', 'request', 'receipt', 'record', 'scope')))
        selected.extend((folder/'archive').glob('*manifest*.json'))
    files, skipped, hits = {}, [], []
    for path in sorted(set(selected)):
        if path.is_symlink() or path.stat().st_size > 8*1024*1024:
            skipped.append(str(path))
            continue
        try:
            obj = read(path)
        except (ValueError, UnicodeError):
            skipped.append(str(path))
            continue
        files[str(path)] = sha(path)
        for url in overlapping_requests(obj):
            hits.append(dict(file=str(path), url=url))
    return dict(files=files, files_read=len(files), skipped=skipped, leads=hits,
                scope='Top-level named input metadata and archive manifests only; not global absence proof')


def inspect():
    require(read(RUN/'validation-state.json')['status'] == 'RUNNING', 'GATE_REQUIRED')
    require(sha(MODEL) == MODEL_SHA and read(MODEL)['coefficients'] == [0, -4], 'FROZEN_MODEL')
    audit = read(ARCHIVE/'input-audit.json')
    require(audit['status'] == 'THREE_ASSET_INPUT_COMPLETE_DEVELOPMENT_ONLY', 'OLD_INPUT_STATUS')
    for p, h in audit['files'].items():
        require(sha(p) == h, 'OLD_INPUT_HASH')
    from cross_asset_inputs import unpack, WIRE
    record_path = ARCHIVE/'attempts/trade-000/record.json'
    record = read(record_path)
    raw = ARCHIVE/'attempts/trade-000/response.bin'
    require(sha(raw) == record['wire_sha256'], 'FIRST_RAW_HASH')
    status, _, _, body = unpack(raw.read_bytes(), WIRE)
    payload = json.loads(body)
    require(status == 200 and payload['retCode'] == 0 and
            payload['result']['symbol'] == 'BTCUSDT' and
            payload['result']['category'] == 'linear', 'FIRST_RAW_IDENTITY')
    starts = [int(row[0]) for row in payload['result']['list']]
    require(min(starts) == audit['start_ms'], 'RAW_START_MISMATCH')
    for kind, width in (('trade', 5), ('mark', 4)):
        p = ARCHIVE/'compiled'/('BTCUSDT-'+kind+'.f64')
        require(p.stat().st_size == (audit['end_ms']-audit['start_ms'])//MINUTE*width*8,
                'ARRAY_LENGTH')
    inventory = metadata_inventory('.artifacts')
    write_new(RUN/'metadata-inventory.json', inventory)
    result = assess(audit['start_ms'], audit['end_ms'])
    result.update(model_sha256=sha(MODEL), plan_sha256=sha(PLAN),
                  input_audit_sha256=sha(ARCHIVE/'input-audit.json'),
                  inventory_sha256=sha(RUN/'metadata-inventory.json'),
                  local_request_leads=len(inventory['leads']),
                  metadata_skipped=len(inventory['skipped']),
                  source_identity='BYBIT_LINEAR_BTCUSDT',
                  reference_native_binary_exists=Path(
                      '.artifacts/execution-net-learning-20260930/build/bounded_learning_driver').is_file(),
                  accounts=0, orders=0, deployments=0, training_vectors=0,
                  candidate_status='NO_QUALIFIED_CANDIDATE', g4=False)
    write_new(RUN/'intake-result.json', result)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect'])
    parser.parse_args()
    inspect()
