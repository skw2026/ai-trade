"""One isolated, credential-safe native acquisition; economic admission disabled.

The caller supplies a frozen private seed. No old gate is read as READY or reset.
Linux verifies native evidence; the unchanged WETH classifier runs after local
restore so Linux OpenSSL need not support the newer keccak CLI digest.
"""
import argparse
import base64
from collections import Counter, defaultdict
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zlib

import aave_authenticated_rpc as t
import aave_weth_boundary as boundary
import forward_evidence_backup as backup
a, c, r, q = t.a, t.c, t.r, t.q
PLAN = Path('docs/plans/2026-10-13-aave-cloud-input.json')
ROOT = Path('.artifacts/aave-cloud-input-20261013')
DEADLINE = dt.datetime(2026, 10, 13, 11, 25, 50, tzinfo=dt.timezone.utc)
CATALOG_SHA = '100a01c5f5ce3eef47865a067d4318559da5999e5ca1ce2b7c50a81b57da9ffb'
ROW_SHA = 'a5c58aadbafdc2e760bb80f963e838a594729622ed150d3edf6b154e25a30ba7'
PREFIX = 'AAVE_CLOUD_20261013_'
SEED_PARTS = 10
MAX_SEED = 4 * 1024**2
MAX_PLAIN = 1024**3
MAX_PACKED = backup.MAX_PLAIN
SOURCES = ('aave_cloud_input.py', 'aave_cloud_local.py', 'aave_authenticated_rpc.py',
           'aave_complete_input.py', 'aave_complete_catalog.py', 'aave_complete_receipts.py',
           'aave_weth_flow.py', 'aave_weth_boundary.py', 'aave_reserve_identity.py',
           'probe_aave_forced_flow.py', 'forward_evidence_backup.py')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def source_bindings():
    return {name: a.digest(Path('tools', name).read_bytes()) for name in SOURCES}


def unpack(raw, limit):
    a.require(isinstance(raw, bytes) and len(raw) <= MAX_PACKED, 'COMPRESSED_SIZE')
    decoder = zlib.decompressobj()
    plain = decoder.decompress(raw, limit + 1)
    a.require(len(plain) <= limit and decoder.eof and not decoder.unused_data
              and not decoder.unconsumed_tail, 'COMPRESSED_STREAM_OR_SIZE')
    return plain


def key(method, params):
    return a.digest(canonical([method, params]))


def native_digest(method, value):
    if method == 'eth_getTransactionReceipt':
        fields = ('transactionHash', 'blockHash', 'blockNumber', 'transactionIndex', 'status')
        log_fields = ('transactionHash', 'blockHash', 'blockNumber', 'transactionIndex',
                      'logIndex', 'address', 'topics', 'data', 'removed')
        value = dict({k: value[k] for k in fields},
                     logs=[{k: row[k] for k in log_fields} for row in value['logs']])
    elif method == 'eth_getBlockByNumber':
        value = {k: value[k] for k in ('number', 'hash', 'timestamp', 'transactions')}
    return a.digest(canonical(value))


def native_plan(rows):
    blocks = defaultdict(dict)
    for tx, group in r.grouped(rows).items():
        blocks[group[0]['blockNumber']][tx] = group
    specs = []
    for number, transactions in sorted(blocks.items(), key=lambda p: int(p[0], 16)):
        specs.extend(('eth_getTransactionReceipt', [tx]) for tx in transactions)
        specs += [('eth_getBlockByNumber', [number, False]), ('eth_call', q.request(number))]
    return blocks, specs


def request_batch(specs):
    a.require(1 <= len(specs) <= 10, 'BATCH_SIZE')
    a.require(len({key(m, p) for m, p in specs}) == len(specs), 'DUPLICATE_METHOD')
    for method, params in specs:
        t.validate_request(method, params)
    return [dict(r.request_identity(m, p), id=i) for i, (m, p) in enumerate(specs, 1)]


def decode_batch(decoded, request):
    a.require(isinstance(decoded, list) and len(decoded) == len(request), 'BATCH_RESPONSE_COUNT')
    result = {}
    for row in decoded:
        a.require(isinstance(row, dict) and row.get('jsonrpc') == '2.0'
                  and type(row.get('id')) is int and 1 <= row['id'] <= len(request)
                  and row['id'] not in result and 'error' not in row and row.get('result') is not None,
                  'BATCH_ID_OR_RPC_ERROR')
        result[row['id']] = row['result']
    return [result[i] for i in range(1, len(request)+1)]


def validate_seed(seed):
    a.require(isinstance(seed, dict) and seed['schema'] == 'aave_cloud_seed_v1', 'SEED_SCHEMA')
    rows = seed['events']
    a.require(seed['catalog_sha256'] == CATALOG_SHA and seed['rows_sha256'] == ROW_SHA
              and a.digest(json.dumps(rows, sort_keys=True).encode()) == ROW_SHA, 'FIXED_CATALOG_CHANGED')
    blocks, specs = native_plan(rows)
    a.require(len(rows) == 2825 and sum(len(v) for v in blocks.values()) == 2429
              and len(blocks) == 1672 and len(specs) == 5773, 'FIXED_POPULATION_CHANGED')
    a.require(seed['sources'] == source_bindings() and seed['plan_sha256'] == a.digest(PLAN.read_bytes()),
              'CLOUD_SOURCE_BINDING_CHANGED')
    a.require(seed['reserved_operations'] == 500 and seed['deadline_utc'] == DEADLINE.isoformat()
              and 0 <= seed['prior_logical_bytes'] < a.MAX_INPUT, 'CLOUD_RESOURCE_BINDING')
    a.require(rows == sorted(rows, key=c.key) and len({c.identity(v) for v in rows}) == len(rows),
              'CATALOG_ORDER_OR_DUPLICATE')
    return blocks, specs


def verify_values(specs, values, cache, blocks):
    pending = dict(zip((key(m, p) for m, p in specs), values))
    def get(m, p):
        name = key(m, p)
        return pending[name] if name in pending else cache[name]
    groups = {tx: group for block in blocks.values() for tx, group in block.items()}
    for (method, params), value in zip(specs, values):
        if method == 'eth_chainId':
            a.require(value == '0x1', 'ETHEREUM_MAINNET_REQUIRED')
        elif method == 'eth_getTransactionReceipt':
            r.validate_transaction(value, groups[params[0]])
        elif method == 'eth_getBlockByNumber':
            for tx, rows in blocks[params[0]].items():
                for row in rows:
                    a.validate_block(value, row, get('eth_getTransactionReceipt', [tx]))
        elif method == 'eth_call':
            q.decode(value, get('eth_getBlockByNumber', [params[1], False]))
        else:
            raise ValueError('METHOD_NOT_ALLOWED')


class Client(t.Client):
    def fetch_batch(self, specs, records, before_send):
        request = request_batch(specs)
        row = {'request': request, 'status': 'FAILED', 'attempted': False, 'started_utc': a.now().isoformat()}
        records.append(row)
        try:
            time.sleep(max(0, 1-(time.monotonic()-self._last_send)))
            before_send()
            row['attempted'] = True
            self._last_send = time.monotonic()
            # Both request and URL travel via stdin config; no credential in argv/env.
            config = 'url = "' + self._url + '"\n'
            config += 'data = "' + canonical(request).decode().replace('\\', '\\\\').replace('"', '\\"') + '"\n'
            metrics = '\n{"http":"%{http_code}","tcp":%{time_connect},"tls":%{time_appconnect},"total":%{time_total}}'
            done = subprocess.run(['curl', '--disable', '--silent', '--proto', '=https',
                '--retry', '0', '--connect-timeout', '15', '--max-time', '30',
                '--max-filesize', str(a.MAX_BODY), '--header', 'Content-Type: application/json',
                '--write-out', metrics, '--config', '-'], input=config.encode(), capture_output=True, timeout=35)
            raw, sep, metric = done.stdout.rpartition(b'\n')
            a.require(sep and len(raw) <= a.MAX_BODY, 'RESPONSE_SIZE_OR_FRAMING')
            a.require(self._key.encode() not in raw, 'CREDENTIAL_ECHO_SUPPRESSED')
            try:
                decoded = json.loads(raw)
            except (ValueError, UnicodeError):
                decoded = None
            a.require(self._key not in json.dumps(decoded, ensure_ascii=False), 'ENCODED_CREDENTIAL_ECHO_SUPPRESSED')
            # Only allowlisted timing fields may leave the transport metadata.
            timing = json.loads(metric)
            a.require(isinstance(timing, dict) and set(timing) == {'http', 'tcp', 'tls', 'total'}
                      and re.fullmatch(r'[0-9]{3}', timing['http'])
                      and all(type(timing[k]) in (float, int) and 0 <= timing[k] <= 40
                              for k in ('tcp', 'tls', 'total')), 'TRANSPORT_METRICS')
            row.update(response=raw.decode('utf-8'), response_sha256=a.digest(raw),
                       response_bytes=len(raw), transport=timing, curl_returncode=done.returncode)
            a.require(done.returncode == 0 and timing['http'] == '200', 'BATCH_TRANSPORT_OR_HTTP')
            result = decode_batch(decoded, request)
            row['status'] = 'RESPONSE_VALID_NOT_NATIVE_VALIDATED'
            return result
        except Exception as exc:
            reason = str(exc)
            row['reason'] = reason if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', reason) else 'UNEXPECTED_ERROR'
            raise ValueError(row['reason']) from None
        finally:
            row['ended_utc'] = a.now().isoformat()


def resources(seed, records):
    methods = sum(len(v['request']) for v in records)
    # Bound JSON escaping conservatively without repeatedly serializing all raw
    # responses (quadratic work across hundreds of batches).
    size = len(canonical(seed)) + sum(2*v.get('response_bytes', 0) + len(canonical(v['request'])) + 8192
                                    for v in records)
    a.require(a.now() < DEADLINE, 'CLOUD_TIME_BUDGET')
    a.require(seed['reserved_operations'] + methods + 1422 + 200 <= 10000, 'CLOUD_METHOD_BUDGET')
    a.require(seed['prior_logical_bytes'] + size + a.MAX_BODY < a.MAX_INPUT, 'CLOUD_BYTE_BUDGET')
    return {'rpc_methods': methods, 'new_logical_bytes': size}


def acquire(seed, client, records):
    blocks, plan = validate_seed(seed)
    groups = r.grouped(seed['events'])
    first = seed['events'][0]
    tx, number = first['transactionHash'], first['blockNumber']
    fixed = [('eth_chainId', []), ('eth_getTransactionReceipt', [tx]),
             ('eth_getBlockByNumber', [number, False]), ('eth_call', q.request(number))]
    values = client.fetch_batch(fixed, records, lambda: resources(seed, records))
    verify_values(fixed, values, {}, {number: {tx: groups[tx]}})
    a.require([native_digest(m, v) for (m, p), v in zip(fixed[1:], values[1:])]
              == seed['fixed_control_value_sha256'],
              'FIXED_NATIVE_CONTROL_DISAGREEMENT')
    records[-1]['status'] = 'NATIVE_VALID'
    cache = {key(m, p): v for (m, p), v in zip(fixed, values)}
    pending = [(m, p) for m, p in plan if key(m, p) not in cache]
    print(json.dumps({'event': 'CLOUD_FIXED_CONTROLS_PASS', 'methods': 4, 'prices': 0}), flush=True)
    # Three control methods plus97additional native methods: exactly100 at the
    # first checkpoint, with no overrun from a ten-method batch.
    used = 0
    while used < len(pending):
        boundary_index = min(97, len(pending)) if used < 97 else len(pending)
        specs = pending[used:min(used+10, boundary_index)]
        values = client.fetch_batch(specs, records, lambda: resources(seed, records))
        verify_values(specs, values, cache, blocks)
        for (m, p), value in zip(specs, values):
            a.require(key(m, p) not in cache, 'DUPLICATE_NATIVE_INPUT')
            cache[key(m, p)] = value
        records[-1]['status'] = 'NATIVE_VALID'
        used += len(specs)
        if used == 97 or (used+3) % 500 == 0 or used == len(pending):
            print(json.dumps({'event': 'CLOUD_NATIVE_PROGRESS', 'additional_native_methods': used,
                              'native_methods': used+3, 'remaining': len(pending)-used,
                              'resources': resources(seed, records)}), flush=True)
    # Complete dependency closure, not merely a counter of successful requests.
    verify_values(plan, [cache[key(m, p)] for m, p in plan], {}, blocks)
    return {'status': 'NATIVE_COMPLETE_NOT_FLOW_OR_ECONOMIC_ADMISSION', 'events': len(seed['events']),
            'transactions': len(groups), 'blocks': len(blocks), 'native_methods': len(plan),
            'resources': resources(seed, records), 'economic_attempts': 0}


def audit_bundle(bundle, expected_seed, classify=False):
    a.require(bundle['seed'] == expected_seed, 'RESTORED_SEED_CHANGED')
    blocks, plan = validate_seed(expected_seed)
    cache = {}
    for record in bundle['records']:
        a.require(record['status'] == 'NATIVE_VALID' and record['attempted'] is True,
                  'INCOMPLETE_NATIVE_DELIVERY')
        raw = record['response'].encode()
        a.require(a.digest(raw) == record['response_sha256'] and len(raw) == record['response_bytes']
                  and record['curl_returncode'] == 0 and record['transport']['http'] == '200',
                  'RESTORED_RESPONSE_CHANGED')
        specs = [(v['method'], v['params']) for v in record['request']]
        a.require(request_batch(specs) == record['request'], 'RESTORED_REQUEST_CHANGED')
        for (m, p), value in zip(specs, decode_batch(json.loads(raw), record['request'])):
            name = key(m, p)
            a.require(name not in cache, 'RESTORED_DUPLICATE_METHOD')
            cache[name] = value
    expected = [('eth_chainId', [])] + plan
    a.require(set(cache) == {key(m, p) for m, p in expected}, 'RESTORED_METHOD_SET')
    verify_values(expected, [cache[key(m, p)] for m, p in expected], {}, blocks)
    a.require(bundle['result']['status'] == 'NATIVE_COMPLETE_NOT_FLOW_OR_ECONOMIC_ADMISSION', 'REMOTE_NOT_COMPLETE')
    flows = {}
    if classify:
        for number, transactions in blocks.items():
            block = cache[key('eth_getBlockByNumber', [number, False])]
            reserve = cache[key('eth_call', q.request(number))]
            for tx, rows in transactions.items():
                flows[tx] = boundary.classify(cache[key('eth_getTransactionReceipt', [tx])], rows, block, reserve)
    return cache, flows


def seal(bundle, certificate, output):
    plain = canonical(bundle)
    a.require(len(plain) <= MAX_PLAIN, 'CLOUD_PLAINTEXT_LIMIT')
    packed = zlib.compress(plain, 6)
    cipher = backup.encrypt(packed, certificate)
    output.mkdir(mode=0o700)
    with (output/'native.cms').open('xb') as f:
        f.write(cipher)
    result = {'schema': 'aave_cloud_cipher_v1', 'cipher_sha256': a.digest(cipher),
              'packed_sha256': a.digest(packed), 'plain_sha256': a.digest(plain),
              'plain_bytes': len(plain), 'cipher_bytes': len(cipher),
              'seed_sha256': a.digest(canonical(bundle['seed'])),
              'certificate_sha256': a.digest(certificate), 'identity': bundle['identity'],
              'status': bundle['result']['status'], 'rpc_methods': sum(len(r['request']) for r in bundle['records'])}
    a.save(output/'receipt.json', result)
    return result


def collect():
    os.umask(0o077)
    a.require(os.environ['GITHUB_REF'] == 'refs/heads/main' and os.environ['GITHUB_RUN_ATTEMPT'] == '1'
              and os.environ['EXPECTED_SHA'] == os.environ['GITHUB_SHA'], 'CLOUD_RUN_IDENTITY')
    raw = base64.b64decode(''.join(os.environ.pop('SEED_%02d' % i) for i in range(SEED_PARTS)), validate=True)
    seed = json.loads(unpack(raw, MAX_SEED))
    a.require(a.digest(canonical(seed)) == os.environ['EXPECTED_SEED_SHA'], 'CLOUD_SEED_IDENTITY')
    certificate = base64.b64decode(os.environ.pop('RECIPIENT_CERTIFICATE_BASE64'), validate=True)
    a.require(a.digest(certificate) == seed['certificate_sha256'], 'CLOUD_RECIPIENT_IDENTITY')
    backup.check_certificate(certificate)
    url = os.environ.pop('ALCHEMY_RPC_URL')
    client = Client(config_text='ALCHEMY_RPC_URL='+url)
    del url
    identity = {'sha': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'], 'attempt': 1}
    records = []
    result = {'status': 'FAILED_BEFORE_COMPLETION'}
    try:
        result = acquire(seed, client, records)
    finally:
        # Preserve partial evidence on failure; never turn failed requests into PASS.
        receipt = seal({'seed': seed, 'records': records, 'result': result, 'identity': identity},
                       certificate, Path(os.environ['CLOUD_OUTPUT']))
        print(json.dumps({'event': 'CLOUD_ENCRYPTED_RECEIPT', **receipt}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['collect'])
    parser.parse_args()
    try:
        collect()
    except Exception as exc:
        reason = str(exc)
        print(json.dumps({'event': 'CLOUD_FAILURE', 'reason': reason if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', reason)
                          else 'UNEXPECTED_ERROR', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1)
