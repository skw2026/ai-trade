"""One approved, encrypted fixed-grid input recovery. No economic execution."""
import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zlib

import steth_cost_headroom as h
import forward_evidence_backup as backup

b = h.base
ROOT = Path('.artifacts/steth-cloud-input-20261014')
PLAN = Path('docs/plans/2026-10-14-steth-cloud-input.md')
DEADLINE = dt.datetime(2026, 10, 14, 13, 14, 30, tzinfo=dt.timezone.utc)
PREFIX = 'STETH_CLOUD_20261014_'
MAX_PLAIN = 32*1024**2
MAX_BODY = 2*1024**2
INDICES = [None, 0, 7] + list(range(8, 52))
SOURCES = ('tools/steth_cloud_input.py', 'tools/steth_cloud_local.py',
           'tools/audit_steth_cloud.py', 'tools/test_steth_cloud.py',
           'tools/steth_cost_headroom.py', 'tools/audit_steth_cost_headroom.py',
           'tools/cost_covered_intake.py', 'tools/aave_authenticated_rpc.py',
           'tools/mvp_reference_inputs.py', 'tools/forward_evidence_backup.py',
           '.github/workflows/steth-cloud-input.yml')


def canonical(v):
    return json.dumps(v, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def unpack(raw, limit):
    b.require(isinstance(raw, bytes) and len(raw) <= MAX_PLAIN, 'PACKED_LIMIT')
    decoder = zlib.decompressobj()
    plain = decoder.decompress(raw, limit+1)
    b.require(len(plain) <= limit and decoder.eof and not decoder.unused_data
              and not decoder.unconsumed_tail, 'COMPRESSED_STREAM_OR_SIZE')
    return plain


def bindings():
    return {p: b.sha(p) for p in (*SOURCES, str(PLAN), str(h.CONTRACT))}


def request(index):
    b.require(index is None or type(index) is int and index in INDICES, 'FIXED_INDEX')
    if index is None:
        return [dict(jsonrpc='2.0', id=1, method='eth_chainId', params=[])]
    c = h.config()
    return h.requests(h.grid(c)[index], c)


def decode(index, raw):
    value = h.strict_json(raw)
    if index is None:
        b.require(h.envelope(value, 1)[1] == '0x1', 'MAINNET_REQUIRED')
        return '0x1'
    c = h.config()
    return h.decode(value, h.grid(c)[index], c)


def validate_seed(seed):
    b.require(seed['schema'] == 'steth_cloud_seed_v1' and seed['sources'] == bindings(), 'SEED_SOURCE_BINDING')
    b.require(seed['indices'] == INDICES and seed['deadline'] == DEADLINE.isoformat(), 'FIXED_PLAN_BINDING')
    b.require(seed['prior_operations_upper'] == 141 and seed['control_operations_reserved'] == 100
              and seed['cloud_methods'] == 277 and 141+100+277 <= 800, 'OPERATION_BUDGET')
    b.require(0 <= seed['prior_bytes'] < 1024**3-MAX_PLAIN, 'BYTE_BUDGET')
    b.require(set(seed['controls']) == {'0', '7'} and all(re.fullmatch('[0-9a-f]{64}', s)
              for s in seed['controls'].values()), 'CONTROL_BINDING')


def budget(seed, records):
    b.require(dt.datetime.now(dt.timezone.utc) < DEADLINE, 'DEADLINE')
    b.require(sum(len(r['request']) for r in records) <= seed['cloud_methods'], 'METHOD_BUDGET')
    b.require(len(canonical(records)) + MAX_BODY < MAX_PLAIN, 'INPUT_BYTE_BUDGET')


class Client:
    def __init__(self, url):
        self.url, self.key = h.parse_config('ALCHEMY_RPC_URL='+url)
        self.last_send = 0

    def fetch(self, index, records, seed):
        req = request(index)
        row = dict(index=index, request=req, status='FAILED', attempted=False, started=b.stamp())
        records.append(row)
        try:
            time.sleep(max(0, 1-(time.monotonic()-self.last_send)))
            budget(seed, records)
            row['attempted'] = True
            self.last_send = time.monotonic()
            cfg = 'url = "'+self.url+'"\n'
            cfg += 'data = "'+canonical(req).decode().replace('\\', '\\\\').replace('"', '\\"')+'"\n'
            metrics = '\n{"http":"%{http_code}","tcp":%{time_connect},"tls":%{time_appconnect},"total":%{time_total}}'
            done = subprocess.run(['curl', '--disable', '--silent', '--proto', '=https', '--retry', '0',
                '--connect-timeout', '15', '--max-time', '30', '--max-filesize', str(MAX_BODY),
                '--header', 'Content-Type: application/json', '--write-out', metrics, '--config', '-'],
                input=cfg.encode(), capture_output=True, timeout=35)
            raw, sep, metric = done.stdout.rpartition(b'\n')
            b.require(sep and len(raw) <= MAX_BODY, 'BODY_FRAMING_OR_LIMIT')
            b.require(self.key.encode() not in raw, 'CREDENTIAL_ECHO')
            try:
                parsed = h.strict_json(raw)
            except (ValueError, UnicodeError):
                parsed = None
            b.require(self.key not in json.dumps(parsed, ensure_ascii=False), 'ENCODED_CREDENTIAL_ECHO')
            timing = h.strict_json(metric)
            b.require(set(timing) == {'http', 'tcp', 'tls', 'total'} and re.fullmatch('[0-9]{3}', timing['http'])
                      and all(type(timing[k]) in (float, int) and 0 <= timing[k] <= 40
                              for k in ('tcp', 'tls', 'total')), 'TRANSPORT_METRICS')
            row.update(response=raw.decode(), response_sha256=b.digest(raw), response_bytes=len(raw),
                       transport=timing, curl_returncode=done.returncode)
            b.require(done.returncode == 0 and timing['http'] == '200', 'TRANSPORT_OR_HTTP')
            decoded = decode(index, raw)
            row['status'] = 'NATIVE_VALID'
            return decoded
        finally:
            row['finished'] = b.stamp()


def validate_records(records, seed):
    validate_seed(seed)
    b.require([r['index'] for r in records] == INDICES, 'COMPLETE_FIXED_RECORD_SET')
    rows = {}
    for row in records:
        index = row['index']
        raw = row['response'].encode()
        b.require(row['request'] == request(index) and row['status'] == 'NATIVE_VALID'
                  and row['attempted'] is True and row['curl_returncode'] == 0
                  and row['transport']['http'] == '200' and row['response_sha256'] == b.digest(raw)
                  and row['response_bytes'] == len(raw) and len(raw) <= MAX_BODY, 'RAW_RECORD_BINDING')
        value = decode(index, raw)
        if index is not None:
            rows[index] = value
    for index in (0, 7):
        b.require(b.digest(canonical(rows[index])) == seed['controls'][str(index)], 'CONTROL_DISAGREEMENT')
    values = list(rows.values())
    b.require(len({r['code_sha256'] for r in values}) == 1 and
              all(a['timestamp'] < z['timestamp'] for a, z in zip(values, values[1:])), 'CODE_OR_TIME_ORDER')
    return rows


def acquire(seed, client, records):
    validate_seed(seed)
    controls = {}
    for index in INDICES:
        value = client.fetch(index, records, seed)
        if index in (0, 7):
            b.require(b.digest(canonical(value)) == seed['controls'][str(index)], 'CONTROL_DISAGREEMENT')
            controls[index] = value
        if index is not None:
            b.require(value['code_sha256'] == controls[0]['code_sha256'], 'POOL_CODE_CHANGED')
        print(json.dumps(dict(event='INPUT_PROGRESS', posts=len(records), total_posts=47)), flush=True)
    validate_records(records, seed)
    budget(seed, records)
    return dict(status='FIXED_INPUT_COMPLETE_NOT_ECONOMIC_EVIDENCE', rpc_methods=277, economic_attempts=0)


def seal(bundle, certificate, output):
    plain = canonical(bundle)
    b.require(len(plain) <= MAX_PLAIN, 'PLAIN_LIMIT')
    packed = zlib.compress(plain, 6)
    cipher = backup.encrypt(packed, certificate)
    output.mkdir(mode=0o700)
    with (output/'native.cms').open('xb') as f:
        f.write(cipher)
    receipt = dict(schema='steth_cloud_cipher_v1', cipher_sha256=b.digest(cipher), cipher_bytes=len(cipher),
        packed_sha256=b.digest(packed), plain_sha256=b.digest(plain), plain_bytes=len(plain),
        seed_sha256=b.digest(canonical(bundle['seed'])), certificate_sha256=b.digest(certificate),
        identity=bundle['identity'], status=bundle['result']['status'],
        rpc_methods=sum(len(r['request']) for r in bundle['records'] if r['attempted']))
    b.save(output/'receipt.json', receipt)
    return receipt


def collect():
    os.umask(0o077)
    b.require(os.environ['GITHUB_REF'] == 'refs/heads/main' and os.environ['GITHUB_RUN_ATTEMPT'] == '1'
              and os.environ['GITHUB_SHA'] == os.environ['EXPECTED_SHA'], 'RUN_IDENTITY')
    seed = h.strict_json(unpack(base64.b64decode(os.environ.pop('STETH_SEED'), validate=True), 65536))
    b.require(b.digest(canonical(seed)) == os.environ['EXPECTED_SEED_SHA'], 'SEED_HASH')
    validate_seed(seed)
    certificate = base64.b64decode(os.environ.pop('RECIPIENT_CERTIFICATE_BASE64'), validate=True)
    b.require(b.digest(certificate) == seed['certificate_sha256'], 'CERTIFICATE_HASH')
    backup.check_certificate(certificate)
    client = Client(os.environ.pop('ALCHEMY_RPC_URL'))
    records = []
    result = dict(status='INCOMPLETE')
    try:
        result = acquire(seed, client, records)
    finally:
        identity = dict(sha=os.environ['GITHUB_SHA'], run_id=os.environ['GITHUB_RUN_ID'], attempt=1)
        receipt = seal(dict(seed=seed, records=records, result=result, identity=identity), certificate,
                       Path(os.environ['CLOUD_OUTPUT']))
        print(json.dumps(dict(event='CLOUD_ENCRYPTED_RECEIPT', **receipt)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['collect'])
    parser.parse_args()
    try:
        collect()
    except Exception as exc:
        reason = str(exc)
        print(json.dumps(dict(event='CLOUD_FAILURE', reason=reason if re.fullmatch('[A-Z][A-Z0-9_]{0,95}', reason)
                              else 'UNEXPECTED_ERROR', error_type=type(exc).__name__)), flush=True)
        raise SystemExit(1)
