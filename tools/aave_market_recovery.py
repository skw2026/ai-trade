"""Explicitly approved, bounded transport recovery; immutable old input and gates."""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

import aave_market_input as old
from aave_economic_model import require, MINUTE
from mvp_reference_inputs import strict_json

ROOT = Path('.artifacts/aave-market-recovery-20261013')
CONTRACT = Path('docs/plans/2026-10-13-aave-market-recovery.json')
PLAN = CONTRACT.with_suffix('.md')
NEW_SOURCES = ('aave_market_recovery.py', 'run_aave_recovered_economics.py', 'test_aave_market_recovery.py')
sha, save, now = old.sha, old.save, old.now


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def sources():
    return dict(old.sources(), **{str(Path('tools', name)): sha(Path('tools', name)) for name in NEW_SOURCES})


def approved_correction():
    """One named implementation repair; never rewrite the original freeze."""
    path = ROOT/'header-correction.json'
    correction = strict_json(path.read_bytes())
    f = strict_json((ROOT/'freeze.json').read_bytes())
    state = strict_json((ROOT/'validation-state.json').read_bytes())
    review_path = Path(correction['review_path'])
    review = strict_json(review_path.read_bytes())
    require(correction['scope'] == 'RFC9110_TOKEN_AND_FIRST_VALID_WIRE' and
        correction['old_sources'] == f['sources'] and correction['new_sources'] == sources(), 'CORRECTION_SOURCE_BINDING')
    require(review['failure_id'] == correction['failure_id'] and
        review['correction_manifest_sha256'] == sha(path), 'CORRECTION_REVIEW_BINDING')
    require(any(v.get('event') == 'review' and v.get('decision') == 'retry' and
        v.get('failure_id') == correction['failure_id'] and v.get('review_sha256') == sha(review_path)
        for v in state['history']), 'CORRECTION_NOT_REVIEWED')
    require(all(sha(p) == h for p, h in correction['originals'].items()), 'ORIGINAL_IMPLEMENTATION_CHANGED')
    changed = {p for p in f['sources'] if f['sources'][p] != correction['new_sources'][p]}
    require(changed == {'tools/aave_market_recovery.py', 'tools/test_aave_market_recovery.py'}, 'CORRECTION_SCOPE_CHANGED')
    return correction


def guard():
    c = strict_json(CONTRACT.read_bytes())
    require(strict_json((ROOT/'validation-state.json').read_bytes())['status'] == 'RUNNING', 'RECOVERY_RUNNING_GATE_REQUIRED')
    current = dt.datetime.now(dt.timezone.utc)
    require(dt.datetime.fromisoformat(c['started_utc'].replace('Z', '+00:00')) <= current <
            dt.datetime.fromisoformat(c['deadline_utc'].replace('Z', '+00:00')), 'RECOVERY_STAGE_DEADLINE')
    require(sha(old.ROOT/'validation-state.json') == c['old_gate_sha256'], 'OLD_HALTED_GATE_CHANGED')
    require(sha(old.ROOT/'freeze.json') == c['old_freeze_sha256'], 'OLD_FREEZE_CHANGED')
    require(sha(old.CONTRACT) == c['economic_contract_sha256'], 'OLD_ECONOMICS_CHANGED')
    previous = strict_json((old.ROOT/'freeze.json').read_bytes())
    require(previous['sources'] == old.sources(), 'FROZEN_MODEL_OR_AUDIT_CHANGED')
    if (ROOT/'freeze.json').exists():
        f = strict_json((ROOT/'freeze.json').read_bytes())
        require(f['contract_sha256'] == sha(CONTRACT) and f['plan_sha256'] == sha(PLAN), 'RECOVERY_POLICY_CHANGED')
        if f['sources'] != sources(): approved_correction()
    return c


def preserve():
    f = strict_json((ROOT/'freeze.json').read_bytes())
    require(all(Path(p).is_file() and sha(p) == h for p, h in f['protected'].items()), 'RECOVERY_PREDECESSOR_CHANGED')
    return len(f['protected'])


def freeze():
    os.umask(0o077)
    c = guard()
    require(not (ROOT/'freeze.json').exists(), 'RECOVERY_ALREADY_FROZEN')
    previous = strict_json((old.ROOT/'freeze.json').read_bytes())
    cache, failures, attempts, raw_bytes = old.load_pages()
    require(len(cache) == c['existing_pages'] and len(failures) == 2 and attempts == c['existing_attempts'], 'RECOVERY_INPUT_COUNTS')
    require(not (old.ROOT/'economic-attempt.json').exists(), 'OLD_ECONOMIC_ATTEMPT_USED')
    missing = [i for i in range(len(previous['requests'])) if i not in cache]
    require(len(missing) == c['missing_pages'] == 636 and c['existing_attempts']+c['maximum_new_attempts'] <= 1500, 'RECOVERY_BUDGET')
    require(c['prior_active_seconds_charged']+c['active_seconds'] == c['original_active_seconds'] == 28800, 'RECOVERY_ACTIVE_BUDGET')
    protected = dict(previous['protected'])
    for p in old.ROOT.rglob('*'):
        if p.is_file(): protected[str(p)] = sha(p)
    for p in [old.CONTRACT, old.PLAN]+[Path(p) for p in old.sources()]: protected[str(p)] = sha(p)
    (ROOT/'attempts').mkdir(mode=0o700)
    save(ROOT/'freeze.json', dict(frozen_utc=now(), contract_sha256=sha(CONTRACT), plan_sha256=sha(PLAN),
        sources=sources(), protected=protected, requests=previous['requests'], missing=missing,
        prior_raw_bytes=raw_bytes, existing_records={str(i): str(p) for i, (p, _) in cache.items()}))
    print(json.dumps(dict(status='RECOVERY_FROZEN_OLD_HALTED_PRESERVED', missing=len(missing),
        protected_files=preserve(), economic_attempts=0)), flush=True)


def unpack(wire, c):
    require(len(wire) <= c['maximum_body_bytes']+c['maximum_header_bytes']+32, 'WIRE_SIZE')
    payload, separator, code = wire.rpartition(b'\n')
    require(separator and re.fullmatch(rb'[0-9]{3}', code), 'HTTP_TRAILER')
    original = payload
    consumed = 0
    headers = {}
    while True:
        block, separator, payload = payload.partition(b'\r\n\r\n')
        require(separator and block.startswith(b'HTTP/'), 'HTTP_HEADER_SHAPE')
        consumed += len(block)+4
        require(consumed <= c['maximum_header_bytes'], 'HEADER_SIZE')
        lines = block.decode('iso-8859-1').split('\r\n')
        status = re.fullmatch(r'HTTP/(?:1\.[01]|2|3) ([0-9]{3})(?: .*)?', lines[0])
        require(status is not None, 'HTTP_STATUS_LINE')
        status = int(status.group(1))
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(':')
            require(sep and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is not None, 'HEADER_FIELD')
            name, value = name.lower(), value.strip()
            if name in ('x-bapi-limit', 'x-bapi-limit-status', 'x-bapi-limit-reset-timestamp'):
                require(name not in headers, 'DUPLICATE_QUOTA_HEADER')
            headers[name] = value
        if not 100 <= status < 200:
            break
    require(status == int(code), 'HTTP_STATUS_MISMATCH')
    require(len(payload) <= c['maximum_body_bytes'], 'BODY_SIZE')
    return status, headers, original[:consumed], payload


def quota_wait(headers, server_ms, received_ms, c, mandatory=False):
    names = ('x-bapi-limit', 'x-bapi-limit-status', 'x-bapi-limit-reset-timestamp')
    values = [headers.get(k) for k in names]
    if not any(v is not None for v in values):
        require(not mandatory, 'RATE_LIMIT_WITHOUT_RESET_EVIDENCE')
        return 0.0
    require(all(isinstance(v, str) and re.fullmatch(r'[0-9]{1,16}', v) for v in values), 'INVALID_QUOTA_HEADERS')
    limit, remaining, reset = map(int, values)
    require(0 < limit and 0 <= remaining <= limit, 'INCONSISTENT_QUOTA_HEADERS')
    require(not mandatory or remaining == 0, 'RATE_LIMIT_REMAINING_NOT_ZERO')
    if remaining:
        return 0.0
    require(type(server_ms) is int and abs(received_ms-server_ms) <= c['maximum_clock_skew_ms'], 'QUOTA_CLOCK_NOT_FRESH')
    require(reset > server_ms, 'QUOTA_RESET_NOT_FUTURE')
    delay = (reset-server_ms)/1000+1
    require(delay <= c['maximum_one_wait_seconds'], 'QUOTA_RESET_BEYOND_WAIT_BUDGET')
    return delay


def interpret(wire, returncode, request, c, page_attempt, received_ms):
    require(returncode == 0, 'UNKNOWN_TRANSPORT_FAILURE')
    http, headers, header_raw, body = unpack(wire, c)
    require(http == 200, 'UNEXPECTED_HTTP_STATUS')
    obj = strict_json(body)
    require(type(obj.get('retCode')) is int, 'BUSINESS_CODE_TYPE')
    code = obj['retCode']
    base = dict(http=http, retCode=code, header_sha256=digest(header_raw), body_sha256=digest(body),
                header_bytes=len(header_raw), body_bytes=len(body))
    if code == 0:
        rows = old.validate(body, request)
        return dict(base, status='VALID', rows=len(rows), wait_seconds=quota_wait(headers, obj.get('time'), received_ms, c))
    require(code in (10016, 10006), 'UNEXPECTED_BUSINESS_ERROR')
    require(page_attempt < c['maximum_page_attempts'], 'PAGE_RECOVERY_EXHAUSTED')
    if code == 10016:
        delay = c['server_error_backoff_seconds'][page_attempt-1]
    else:
        delay = quota_wait(headers, obj.get('time'), received_ms, c, mandatory=True)
    return dict(base, status='RECOVERABLE_NOT_VALID', wait_seconds=delay)


class Budget:
    def __init__(self, c):
        self.c = c
        self.attempts = self.errors = 0
        self.waits = 0.0
        self.bytes = 0
        self.pages = collections.Counter()

    def reserve(self, index):
        require(self.attempts < self.c['maximum_new_attempts'], 'TOTAL_ATTEMPT_BUDGET')
        require(self.pages[index] < self.c['maximum_page_attempts'], 'PAGE_ATTEMPT_BUDGET')
        require(self.bytes+self.c['maximum_body_bytes']+self.c['maximum_header_bytes']+32 <=
                self.c['maximum_new_raw_bytes'], 'NEW_RAW_BUDGET')
        self.attempts += 1
        self.pages[index] += 1
        return self.pages[index]

    def account(self, result, wire_bytes):
        self.bytes += wire_bytes
        if result['status'] != 'VALID': self.errors += 1
        self.waits += result['wait_seconds']
        require(self.errors <= self.c['maximum_known_error_responses'], 'KNOWN_ERROR_BUDGET')
        require(self.waits <= self.c['maximum_total_recovery_wait_seconds'], 'RECOVERY_WAIT_BUDGET')


def wait_until(deadline):
    while time.monotonic() < deadline:
        guard()
        time.sleep(min(5, max(0, deadline-time.monotonic())))


def effective_record(folder):
    original = strict_json((folder/'record.json').read_bytes())
    if not (folder/'corrected-interpretation.json').exists(): return original
    correction = approved_correction()
    annotation = strict_json((folder/'corrected-interpretation.json').read_bytes())
    require(folder == ROOT/'attempts/0000' and sha(folder/'record.json') == correction['first_record_sha256']
        == annotation['original_record_sha256'] and annotation['correction_sha256'] == sha(ROOT/'header-correction.json'),
        'CORRECTED_INTERPRETATION_BINDING')
    require(original['status'] == 'FAILED' and original.get('error') == 'ValueError:HEADER_FIELD', 'NOT_HEADER_FAILURE')
    return dict(original, **annotation['interpretation'])


def resume_first_wire(c, f, budget):
    correction = approved_correction()
    require(not (ROOT/'header-repair-started.json').exists(), 'HEADER_REPAIR_ALREADY_USED')
    require(sorted(p.name for p in (ROOT/'attempts').iterdir()) == ['0000'], 'ONLY_FIRST_WIRE_REPAIR_ALLOWED')
    folder = ROOT/'attempts/0000'
    original = strict_json((folder/'record.json').read_bytes())
    wire = (folder/'response.bin').read_bytes()
    require(sha(folder/'record.json') == correction['first_record_sha256'] and
        digest(wire) == correction['first_wire_sha256'] == original['wire_sha256'], 'REPAIR_ORIGINAL_RESPONSE_CHANGED')
    require(original['status'] == 'FAILED' and original.get('error') == 'ValueError:HEADER_FIELD' and
        original['index'] == f['missing'][0] and original['page_attempt'] == 1, 'REPAIR_ORIGINAL_FAILURE')
    i = original['index']
    require(original['request'] == f['requests'][i], 'REPAIR_REQUEST_BINDING')
    attempt = budget.reserve(i)
    result = interpret(wire, original['curl_returncode'], original['request'], c, attempt, original['received_ms'])
    require(result['status'] == 'VALID' and result['wait_seconds'] == 0, 'REPAIR_ONLY_ALREADY_VALID_NONLIMITED_PAGE')
    budget.account(result, len(wire))
    save(ROOT/'header-repair-started.json', dict(correction_sha256=sha(ROOT/'header-correction.json'), started_utc=now()))
    save(folder/'corrected-interpretation.json', dict(original_record_sha256=sha(folder/'record.json'),
        correction_sha256=sha(ROOT/'header-correction.json'), interpretation=result, interpreted_utc=now()))
    return {i: folder}


def collect():
    os.umask(0o077)
    c = guard(); preserve()
    f = strict_json((ROOT/'freeze.json').read_bytes())
    budget = Budget(c)
    if (ROOT/'collection-started.json').exists():
        require((ROOT/'header-correction.json').exists(), 'COLLECTION_ALREADY_STARTED_NO_AUTOMATIC_RELAUNCH')
        cache = resume_first_wire(c, f, budget)
    else:
        save(ROOT/'collection-started.json', dict(started_utc=now(), freeze_sha256=sha(ROOT/'freeze.json')))
        cache = {}
    next_start = 0.0
    for i in f['missing']:
        req = f['requests'][i]
        while i not in cache:
            wait_until(next_start); guard()
            page_attempt = budget.reserve(i)
            folder = ROOT/'attempts'/('%04d' % (budget.attempts-1))
            folder.mkdir(mode=0o700)
            save(folder/'started.json', dict(index=i, request=req, page_attempt=page_attempt, started_utc=now()))
            wire = b''
            record = dict(index=i, request=req, status='FAILED', page_attempt=page_attempt)
            started = time.monotonic()
            try:
                try:
                    response = subprocess.run(['curl', '--disable', '--silent', '--proto', '=https',
                        '--include', '--suppress-connect-headers', '--retry', '0', '--connect-timeout', '15',
                        '--max-time', '30', '--max-filesize', str(c['maximum_body_bytes']),
                        '--write-out', '\n%{http_code}', req['url']], capture_output=True, timeout=35)
                    wire, rc = response.stdout, response.returncode
                except subprocess.TimeoutExpired as exc:
                    wire, rc = exc.stdout or b'', -1
                record['curl_returncode'] = rc
                record['received_ms'] = int(time.time()*1000)
                result = interpret(wire, rc, req, c, page_attempt, record['received_ms'])
                record.update(result)
                budget.account(result, len(wire))
            except Exception as exc:
                record.update(status='FAILED', error=type(exc).__name__+':'+str(exc)[:160])
                raise
            finally:
                limit = c['maximum_body_bytes']+c['maximum_header_bytes']+32
                with (folder/'response.bin').open('xb') as handle: handle.write(wire[:limit])
                record.update(wire_sha256=digest(wire[:limit]), received_wire_sha256=digest(wire),
                    received_bytes=len(wire), retained_bytes=min(len(wire), limit),
                    ended_utc=now(), duration_seconds=time.monotonic()-started)
                save(folder/'record.json', record)
            next_start = max(started+c['minimum_start_interval_seconds'], time.monotonic()+result['wait_seconds'])
            if result['status'] == 'VALID':
                cache[i] = folder
            else:
                print(json.dumps(dict(event='BOUNDED_RECOVERY', retCode=result['retCode'], index=i,
                    page_attempt=page_attempt, wait_seconds=result['wait_seconds'], new_attempts=budget.attempts)), flush=True)
            total = c['existing_pages']+len(cache)
            if result['status'] == 'VALID' and (total % 25 == 0 or len(cache) == len(f['missing'])):
                print(json.dumps(dict(event='MARKET_PROGRESS', valid_pages=total, total_pages=len(f['requests']),
                    new_attempts=budget.attempts, known_error_responses=budget.errors, raw_bytes=budget.bytes)), flush=True)
    guard(); preserve()
    save(ROOT/'collection.json', dict(status='FIXED_MARKET_DELIVERY_COMPLETE_NOT_YET_ADMITTED',
        new_attempts=budget.attempts, known_errors=budget.errors, recovery_wait_seconds=budget.waits,
        new_raw_bytes=budget.bytes, records={str(i): dict(folder=str(p), sha256=sha(p/'record.json')) for i, p in cache.items()},
        attempt_records={str(p/'record.json'): sha(p/'record.json') for p in sorted((ROOT/'attempts').iterdir())}))


def verify_new_ledger(c, f, collection):
    folders = sorted((ROOT/'attempts').iterdir())
    require(len(folders) == collection['new_attempts'] and
            set(collection['attempt_records']) == {str(p/'record.json') for p in folders}, 'ATTEMPT_LEDGER_COVERAGE')
    budget = Budget(c)
    cursor = 0
    rows = {}
    for ordinal, folder in enumerate(folders):
        require(folder.name == '%04d' % ordinal, 'ATTEMPT_ORDINAL')
        require(sha(folder/'record.json') == collection['attempt_records'][str(folder/'record.json')], 'ATTEMPT_RECORD_CHANGED')
        rec = effective_record(folder)
        i = rec['index']
        require(cursor < len(f['missing']) and i == f['missing'][cursor], 'ATTEMPT_ORIGINAL_ORDER')
        attempt = budget.reserve(i)
        require(rec['page_attempt'] == attempt and rec['request'] == f['requests'][i], 'ATTEMPT_REQUEST_CHANGED')
        wire = (folder/'response.bin').read_bytes()
        require(digest(wire) == rec['wire_sha256'] == rec['received_wire_sha256'] and
                len(wire) == rec['retained_bytes'] == rec['received_bytes'], 'ATTEMPT_WIRE_CHANGED')
        result = interpret(wire, rec['curl_returncode'], rec['request'], c, attempt, rec['received_ms'])
        require(all(rec[k] == v for k, v in result.items()), 'ATTEMPT_CLASSIFICATION_CHANGED')
        budget.account(result, len(wire))
        if result['status'] == 'VALID':
            entry = collection['records'].get(str(i))
            require(entry == dict(folder=str(folder), sha256=sha(folder/'record.json')), 'NEW_PAGE_MANIFEST')
            rows[i] = old.validate(unpack(wire, c)[3], rec['request'])
            cursor += 1
    require(cursor == len(f['missing']) and len(collection['records']) == cursor, 'ATTEMPT_LEDGER_INCOMPLETE')
    require(budget.errors == collection['known_errors'] and budget.bytes == collection['new_raw_bytes'] and
            budget.waits == collection['recovery_wait_seconds'], 'ATTEMPT_TOTALS_CHANGED')
    return rows


def market():
    c = guard(); preserve()
    f = strict_json((ROOT/'freeze.json').read_bytes())
    collection = strict_json((ROOT/'collection.json').read_bytes())
    cache, _, _, _ = old.load_pages()
    new_rows = verify_new_ledger(c, f, collection)
    require(not (set(cache) & set(new_rows)), 'NEW_CACHE_OVERLAP')
    require(set(cache) | set(new_rows) == set(range(1422)) and len(new_rows) == 636, 'FULL_MARKET_NOT_PRESENT')
    opportunities = strict_json((old.PRIOR/'causal-capacity-private.json').read_bytes())['opportunities']
    needed = {k: set() for k in ('trade', 'mark')}
    for e in opportunities:
        if e['exclusion'] is not None: continue
        first, entry, end = (e[k]*1000 for k in ('first', 'entry', 'exit'))
        anchor = (first-1)//MINUTE*MINUTE
        needed['trade'].update(range(anchor-61*MINUTE, anchor, MINUTE))
        needed['trade'].update((entry, end))
        needed['mark'].update(range(entry-MINUTE, end+1, MINUTE))
    data = {k: {} for k in ('trade', 'mark', 'funding')}
    counts = collections.Counter()
    for i, req in enumerate(f['requests']):
        rows = new_rows[i] if i in new_rows else old.validate((cache[i][0]/'response.json').read_bytes(), req)
        kind = req['kind']; counts[kind] += len(rows)
        for t, values in rows:
            if kind == 'funding' or t in needed[kind]:
                require(t not in data[kind], 'DUPLICATE_REQUIRED_PRICE')
                data[kind][t] = values
    require(counts['trade'] == counts['mark'] == 527100, 'FULL_MINUTE_DOMAIN')
    require(all(set(data[k]) == needed[k] for k in needed), 'MISSING_REQUIRED_PRICE')
    return data, opportunities, dict(counts=dict(counts), attempted_gets=788+collection['new_attempts'],
        raw_bytes=f['prior_raw_bytes']+collection['new_raw_bytes'])


def admit():
    data, _, stats = market()
    report = dict(status='FULL_FIXED_MARKET_ADMITTED', **stats, selected_rows={k: len(v) for k, v in data.items()},
        collection_sha256=sha(ROOT/'collection.json'), protected_files=preserve(), economic_attempts=0)
    save(ROOT/'market-audit.json', report)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'collect', 'admit'))
    args = parser.parse_args()
    if args.command == 'collect' and (ROOT/'header-correction.json').exists():
        guard(); approved_correction()
        # Same formal retry includes focused regression before any network I/O.
        completed = subprocess.run(['python3', '-m', 'unittest', 'discover', '-s', 'tools', '-p', 'test_aave_market_recovery.py'], timeout=180)
        require(completed.returncode == 0, 'HEADER_REPAIR_REGRESSION_FAILED')
    {'freeze': freeze, 'collect': collect, 'admit': admit}[args.command]()
