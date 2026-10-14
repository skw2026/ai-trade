"""Frozen quote/claim cost-capacity screen, never a USD strategy backtest."""
import argparse
from decimal import Decimal, localcontext
from fractions import Fraction
import json
import os
from pathlib import Path
import re
import subprocess
import time

import cost_covered_intake as base
from aave_authenticated_rpc import parse_config
from mvp_reference_inputs import strict_json

ROOT = base.ROOT/'steth-headroom'
CONTRACT = Path('docs/plans/2026-10-14-steth-cost-headroom.json')
PLAN = CONTRACT.with_suffix('.md')
SOURCES = ('tools/steth_cost_headroom.py', 'tools/test_steth_cost_headroom.py',
           'tools/aave_authenticated_rpc.py', 'tools/mvp_reference_inputs.py')


def config():
    return strict_json(CONTRACT.read_bytes())


def grid(c):
    lo, hi, n = c['first_block_inclusive'], c['last_block_exclusive'], c['samples']
    base.require(type(n) is int and n > 0 and hi-lo >= n, 'SAMPLE_GRID')
    return [lo+(hi-lo)*(2*i+1)//(2*n) for i in range(n)]


def requests(block, c):
    def call(to, data):
        return 'eth_call', [dict(to=to, data=data), hex(block)]
    specs = [('eth_getBlockByNumber', [hex(block), False]), ('eth_getCode', [c['pool'], hex(block)]),
             call(c['pool'], '0xc6610657'+format(0, '064x')),
             call(c['pool'], '0xc6610657'+format(1, '064x')),
             call(c['coin1'], '0x313ce567'),
             call(c['pool'], '0x5e0d443f'+''.join(format(i, '064x') for i in (0, 1, c['input_wei'])))]
    return [dict(jsonrpc='2.0', id=i, method=m, params=p) for i, (m, p) in enumerate(specs, 1)]


def envelope(value, size):
    base.require(isinstance(value, list) and len(value) == size, 'BATCH_SIZE')
    results = {}
    for row in value:
        base.require(isinstance(row, dict) and type(row.get('id')) is int and
                     1 <= row['id'] <= size and row['id'] not in results and
                     row.get('jsonrpc') == '2.0' and 'error' not in row and 'result' in row, 'BATCH_ENVELOPE')
        results[row['id']] = row['result']
    return results


def uint256(value):
    base.require(isinstance(value, str) and re.fullmatch('0x[0-9a-f]{64}', value), 'UINT256_WORD')
    return int(value, 16)


def decode(value, block, c):
    r = envelope(value, 6)
    header = r[1]
    base.require(isinstance(header, dict) and header.get('number') == hex(block), 'BLOCK_NUMBER')
    base.require(re.fullmatch('0x[0-9a-f]{64}', header.get('hash', '')), 'BLOCK_HASH')
    base.require(re.fullmatch('0x[0-9a-f]+', header.get('timestamp', '')), 'BLOCK_TIMESTAMP')
    timestamp = int(header['timestamp'], 16)
    base.require(1704067200 <= timestamp < 1735689600, 'OUTSIDE_YEAR')
    base.require(isinstance(r[2], str) and re.fullmatch('0x(?:[0-9a-f]{2})+', r[2]), 'POOL_BYTECODE')
    base.require(uint256(r[3]) == int(c['coin0'], 16) and uint256(r[4]) == int(c['coin1'], 16), 'POOL_COINS')
    base.require(uint256(r[5]) == 18, 'STETH_DECIMALS')
    quote = uint256(r[6])
    base.require(0 < quote < 2**128, 'QUOTE_RANGE')
    return dict(block=block, block_hash=header['hash'], timestamp=timestamp,
                code_sha256=base.digest(bytes.fromhex(r[2][2:])), output_steth_wei=quote)


def guard():
    base.guard()
    c = config()
    if (ROOT/'freeze.json').exists():
        f = base.read(ROOT/'freeze.json')
        base.require(all(base.sha(p) == h for p, h in f['inputs'].items()), 'HEADROOM_FREEZE_CHANGED')
    return c


def freeze():
    os.umask(0o077)
    c = guard()
    base.preserve()
    base.require(not ROOT.exists(), 'HEADROOM_ALREADY_PREPARED')
    base.require(base.sha(c['boundary_manifest']) == c['boundary_manifest_sha256'], 'YEAR_BOUNDARY_BINDING')
    boundaries = base.read(c['boundary_manifest'])['boundary_points']
    base.require(boundaries[0]['block_number'] == c['first_block_inclusive'] and
                 boundaries[-1]['block_number'] == c['last_block_exclusive'], 'YEAR_BOUNDARIES')
    base.require(6*c['samples']+1 == c['planned_rpc_methods'] and
                 c['prior_operations_charged']+c['planned_rpc_methods'] <= 800, 'RPC_BUDGET')
    ROOT.mkdir(mode=0o700)
    base.save(ROOT/'freeze.json', dict(at=base.stamp(), blocks=grid(c),
        inputs={str(p): base.sha(p) for p in (CONTRACT, PLAN, *SOURCES)},
        old_control_sha256=base.sha(base.ROOT/'steth-control/receipt.json'), economic_attempts=0))
    print(json.dumps(dict(status='FROZEN_BEFORE_QUOTES', samples=c['samples'], rpc_methods=c['planned_rpc_methods'])))


def fetch(folder, reqs, url, key):
    folder.mkdir(mode=0o700)
    base.save(folder/'request.json', reqs)
    record = dict(status='FAILED', started_utc=base.stamp(), methods=len(reqs),
                  request_sha256=base.sha(folder/'request.json'), retries=0)
    try:
        done = subprocess.run(['curl', '--disable', '--silent', '--proto', '=https', '--connect-timeout', '15',
            '--max-time', '30', '--max-filesize', '2097152', '--retry', '0', '--header', 'Content-Type: application/json',
            '--data-binary', '@'+str(folder/'request.json'), '--write-out', '\n%{http_code}', '--config', '-'],
            input=('url = "'+url+'"\n').encode(), capture_output=True, timeout=35)
        raw, sep, http = done.stdout.rpartition(b'\n')
        record.update(curl_returncode=done.returncode, http=http.decode() if re.fullmatch(rb'[0-9]{3}', http) else 'UNKNOWN',
                      response_bytes=len(raw), response_sha256=base.digest(raw))
        base.require(sep and len(raw) <= 2097152, 'RESPONSE_FRAMING')
        base.require(key.encode() not in raw, 'KEY_ECHO')
        try:
            value = strict_json(raw)
        except (ValueError, UnicodeError):
            value = None
        base.require(key not in json.dumps(value, ensure_ascii=False), 'ENCODED_KEY_ECHO')
        with (folder/'response.json').open('xb') as out:
            out.write(raw)
        base.require(done.returncode == 0 and http == b'200', 'RPC_TRANSPORT_OR_HTTP')
        envelope(value, len(reqs))
        record['status'] = 'RECEIVED_NOT_YET_ADMITTED'
        return value
    except Exception as exc:
        record['error_type'] = type(exc).__name__
        reason = str(exc)
        record['reason'] = reason if re.fullmatch('[A-Z][A-Z0-9_]{0,95}', reason) else 'UNEXPECTED_ERROR'
        raise RuntimeError(record['reason']) from None
    finally:
        record['finished_utc'] = base.stamp()
        base.save(folder/'receipt.json', record)


def collect():
    os.umask(0o077)
    c = guard()
    base.preserve()
    base.require(not (ROOT/'operations').exists(), 'ONE_COLLECTION_ONLY')
    cfg = Path('.env.local')
    base.require(cfg.is_file() and not cfg.is_symlink() and cfg.stat().st_size <= 65536, 'LOCAL_CONFIG')
    url, key = parse_config(cfg.read_text())
    (ROOT/'operations').mkdir(mode=0o700)
    started = time.monotonic()
    chain = fetch(ROOT/'operations/chain', [dict(jsonrpc='2.0', id=1, method='eth_chainId', params=[])], url, key)
    base.require(envelope(chain, 1)[1] == '0x1', 'WRONG_CHAIN')
    rows = []
    for i, block in enumerate(grid(c)):
        guard()
        time.sleep(max(0, 1-(time.monotonic()-started)))
        started = time.monotonic()
        value = fetch(ROOT/'operations'/str(i), requests(block, c), url, key)
        row = decode(value, block, c)
        if rows:
            base.require(row['timestamp'] > rows[-1]['timestamp'], 'TIME_ORDER')
            base.require(row['code_sha256'] == rows[0]['code_sha256'], 'POOL_CODE_CHANGED')
        rows.append(row)
        print(json.dumps(dict(status='QUOTE_INPUT_ONLY', received=i+1, expected=c['samples'])), flush=True)
    base.save(ROOT/'quotes.json', dict(at=base.stamp(), rows=rows, rpc_methods=1+6*len(rows),
                                      freeze_sha256=base.sha(ROOT/'freeze.json')))
    print(json.dumps(dict(status='FIXED_QUOTE_GRID_COMPLETE_NOT_EXECUTIONS', samples=len(rows), economic_attempts=0)))


def evidence():
    c = config()
    f = base.read(ROOT/'freeze.json')
    base.require(all(base.sha(p) == h for p, h in f['inputs'].items()), 'HEADROOM_FREEZE_CHANGED')
    output = base.read(ROOT/'quotes.json')
    base.require(output['freeze_sha256'] == base.sha(ROOT/'freeze.json') and len(output['rows']) == c['samples'], 'QUOTE_BINDING')
    expected = [('chain', [dict(jsonrpc='2.0', id=1, method='eth_chainId', params=[])])]
    expected += [(str(i), requests(b, c)) for i, b in enumerate(grid(c))]
    reconstructed = []
    total = 0
    for name, reqs in expected:
        folder = ROOT/'operations'/name
        rec = base.read(folder/'receipt.json')
        base.require(base.read(folder/'request.json') == reqs and rec['request_sha256'] == base.sha(folder/'request.json') and
                     rec['response_sha256'] == base.sha(folder/'response.json') and rec['http'] == '200' and
                     rec['curl_returncode'] == 0 and rec['status'] == 'RECEIVED_NOT_YET_ADMITTED', 'RAW_RECEIPT_BINDING')
        raw = strict_json((folder/'response.json').read_bytes())
        total += len(reqs)
        if name == 'chain':
            base.require(envelope(raw, 1)[1] == '0x1', 'WRONG_CHAIN')
        else:
            reconstructed.append(decode(raw, grid(c)[int(name)], c))
    base.require(reconstructed == output['rows'] and total == c['planned_rpc_methods'], 'RAW_QUOTES_MISMATCH')
    base.require(len({r['code_sha256'] for r in reconstructed}) == 1 and
                 all(a['timestamp'] < b['timestamp'] for a, b in zip(reconstructed, reconstructed[1:])), 'GRID_CODE_OR_ORDER')
    return c, reconstructed


def measure(quotes, c):
    with localcontext() as context:
        context.prec = 70
        gross = [Decimal(10000)*(Decimal(q)/Decimal(c['input_wei'])-1) for q in quotes]
        basic = [g-Decimal(c['reference_fee_bps']) for g in gross]
        stress = [g-Decimal(c['stress_fee_bps']) for g in gross]
        count_basic, count_stress = (sum(v > 0 for v in values) for values in (basic, stress))
        decision = ('REJECT_SAMPLED_REFERENCE_COST_COVERAGE' if not count_basic else
                    'INSUFFICIENT_STRESS_COST_HEADROOM' if not count_stress else
                    'COST_HEADROOM_PRESENT_EXIT_MODEL_NOT_ADMITTED')
        return dict(decision=decision, samples=len(gross), qualified_candidates=0,
            positive_gross_samples=sum(v > 0 for v in gross), reference_cost_covered=count_basic,
            stress_cost_covered=count_stress, gross_min_bps=str(min(gross)), gross_max_bps=str(max(gross)),
            gross_mean_bps=str(sum(gross)/len(gross)), maximum_reference_headroom_bps=str(max(basic)),
            maximum_stress_headroom_bps=str(max(stress)), economic_attempts=1,
            full_strategy_pnl=None, sharpe=None, maximum_drawdown=None, g4=False)


def execute():
    c = guard()
    base.preserve()
    _, rows = evidence()
    base.save(ROOT/'economic-attempt.json', dict(started_utc=base.stamp(), attempt=1,
              freeze_sha256=base.sha(ROOT/'freeze.json'), input_sha256=base.sha(ROOT/'quotes.json')))
    started = time.monotonic()
    result = measure([r['output_steth_wei'] for r in rows], c)
    base.require(time.monotonic()-started < c['compute_seconds_maximum'], 'COMPUTE_LIMIT')
    base.save(ROOT/'result.json', result)
    print(json.dumps(result, sort_keys=True))


def audit():
    guard()
    c, rows = evidence()
    result = base.read(ROOT/'result.json')
    # Independent arithmetic path: integer ABI -> rational ETH claim difference.
    gross = [Fraction((r['output_steth_wei']-c['input_wei'])*10000, c['input_wei']) for r in rows]
    base.require(result['samples'] == len(gross) and result['qualified_candidates'] == 0, 'AUDIT_COUNTS')
    base.require(Fraction(result['gross_min_bps']) == min(gross) and Fraction(result['gross_max_bps']) == max(gross), 'AUDIT_EXTREMA')
    counts = [sum(g > Fraction(c[k]) for g in gross) for k in ('reference_fee_bps', 'stress_fee_bps')]
    base.require(counts == [result['reference_cost_covered'], result['stress_cost_covered']], 'AUDIT_COST_COUNTS')
    decision = ('REJECT_SAMPLED_REFERENCE_COST_COVERAGE' if counts[0] == 0 else
                'INSUFFICIENT_STRESS_COST_HEADROOM' if counts[1] == 0 else 'COST_HEADROOM_PRESENT_EXIT_MODEL_NOT_ADMITTED')
    base.require(result['decision'] == decision and all(result[k] is None for k in
                 ('full_strategy_pnl', 'sharpe', 'maximum_drawdown')) and result['g4'] is False, 'AUDIT_DECISION_OR_OVERCLAIM')
    base.save(ROOT/'audit.json', dict(status='RAW_BINDING_AND_INDEPENDENT_RATIONAL_ARITHMETIC_AGREE',
              result_sha256=base.sha(ROOT/'result.json'), checked_quotes=len(rows),
              qualified_candidates=0, protected_files=base.preserve()))
    print(json.dumps(base.read(ROOT/'audit.json'), sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'collect', 'execute', 'audit'))
    globals()[parser.parse_args().action]()
