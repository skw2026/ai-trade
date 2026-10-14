"""Local-only keys, one authorized dispatch, restore and frozen cost screen."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zlib

import steth_cloud_input as x
b, h, backup, ROOT = x.b, x.h, x.backup, x.ROOT
REPO = 'skw2026/ai-trade'
WORKFLOW = 'steth-cloud-input.yml'


def gh(args, payload=None):
    ledger = ROOT/'github-operations'
    ledger.mkdir(exist_ok=True, mode=0o700)
    index = len(list(ledger.iterdir()))
    b.require(index < 90, 'CONTROL_PLANE_RESERVE_EXHAUSTED')
    b.save(ledger/(str(index)+'.json'), dict(argv=args, at=b.stamp(), attempted=True))
    done = subprocess.run(['gh', *args], input=payload, capture_output=True, timeout=90)
    b.require(done.returncode == 0, 'GITHUB_OPERATION_FAILED')
    return done.stdout


def api(path):
    return h.strict_json(gh(['api', 'repos/'+REPO+'/'+path]))


def protect():
    protected = b.read(ROOT/'protected.json')
    b.require(all(Path(p).is_file() and b.sha(p) == sha for p, sha in protected.items()), 'OLD_EVIDENCE_CHANGED')
    return len(protected)


def old_record(index):
    folder = h.ROOT/'operations'/('chain' if index is None else str(index))
    rec, req = b.read(folder/'receipt.json'), b.read(folder/'request.json')
    raw = (folder/'response.json').read_bytes()
    expected = [dict(jsonrpc='2.0', id=1, method='eth_chainId', params=[])] if index is None else h.requests(h.grid(h.config())[index], h.config())
    b.require(req == expected and rec['request_sha256'] == b.sha(folder/'request.json')
              and rec['response_sha256'] == b.digest(raw) and rec['response_bytes'] == len(raw)
              and rec['curl_returncode'] == 0 and rec['http'] == '200' and rec['retries'] == 0
              and rec['status'] == 'RECEIVED_NOT_YET_ADMITTED', 'OLD_RECEIPT_INVALID')
    if index is None:
        b.require(h.envelope(h.strict_json(raw), 1)[1] == '0x1', 'OLD_CHAIN_INVALID')
    else:
        h.decode(h.strict_json(raw), h.grid(h.config())[index], h.config())
    return dict(index=index, request=req, response=raw.decode(), response_sha256=b.digest(raw),
                response_bytes=len(raw), source=str(folder), source_receipt_sha256=b.sha(folder/'receipt.json'))


def prepare():
    b.require(not (ROOT/'seed.json').exists() and not (ROOT/'recipient.key').exists(), 'PREPARE_ONCE')
    b.require(x.dt.datetime.now(x.dt.timezone.utc) < x.DEADLINE, 'DEADLINE')
    b.preserve()
    old = b.read(b.ROOT/'freeze.json')['protected']
    for path in b.ROOT.rglob('*'):
        if path.is_file() and '__pycache__' not in path.parts:
            old[str(path)] = b.sha(path)
    for freeze in (b.ROOT/'freeze.json', h.ROOT/'freeze.json'):
        for name, digest in b.read(freeze)['inputs'].items():
            b.require(b.sha(name) == digest, 'OLD_FROZEN_SOURCE_CHANGED')
            old[name] = digest
    b.save(ROOT/'protected.json', old)
    protect()
    b.require(b.read(b.ROOT/'validation-state.json')['status'] == 'BLOCKED', 'OLD_GATE_STATUS_CHANGED')
    b.require(not any((h.ROOT/f).exists() for f in ('quotes.json', 'economic-attempt.json', 'result.json')), 'PRIOR_ECONOMIC_ATTEMPT')
    old_record(None)
    rows = [h.decode(h.strict_json(old_record(i)['response'].encode()), h.grid(h.config())[i], h.config()) for i in range(8)]
    done = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '8',
        '-subj', '/CN=ai-trade-steth-private-input-20261014', '-keyout', str(ROOT/'recipient.key'),
        '-out', str(ROOT/'recipient.pem')], capture_output=True, timeout=90)
    b.require(done.returncode == 0, 'RECIPIENT_GENERATION_FAILED')
    (ROOT/'recipient.key').chmod(0o600)
    cert = backup.read_regular((ROOT/'recipient.pem').absolute(), 65536)
    backup.check_certificate(cert)
    value = dict(schema='steth_cloud_seed_v1', sources=x.bindings(), indices=x.INDICES,
        deadline=x.DEADLINE.isoformat(), prior_operations_upper=141, control_operations_reserved=100,
        cloud_methods=277, certificate_sha256=b.digest(cert),
        controls={str(i): b.digest(x.canonical(rows[i])) for i in (0, 7)},
        prior_bytes=sum(p.stat().st_size for p in b.ROOT.rglob('*') if p.is_file()),
        predecessor_freeze_sha256=b.sha(h.ROOT/'freeze.json'), predecessor_gate_sha256=b.sha(b.ROOT/'validation-state.json'))
    x.validate_seed(value)
    encoded = base64.b64encode(zlib.compress(x.canonical(value), 9))
    b.require(len(encoded) <= 40000, 'SECRET_SIZE')
    b.save(ROOT/'seed.json', value)
    b.save(ROOT/'prepared.json', dict(seed_sha256=b.digest(x.canonical(value)), protected_files=len(old),
                                    encoded_bytes=len(encoded)))
    print(json.dumps(dict(status='PREPARED', protected_files=len(old), new_rpc=0)))


def seed():
    protect()
    value, prepared = b.read(ROOT/'seed.json'), b.read(ROOT/'prepared.json')
    x.validate_seed(value)
    b.require(b.digest(x.canonical(value)) == prepared['seed_sha256'], 'SEED_CHANGED')
    return value, prepared


def secret_names():
    listed = api('actions/secrets?per_page=100')
    b.require(listed['total_count'] == len(listed['secrets']) <= 90, 'SECRET_PAGINATION')
    return {v['name'] for v in listed['secrets']}


def configure():
    value, prepared = seed()
    b.require(not (ROOT/'secret-reservation.json').exists(), 'CONFIGURE_ONCE')
    names = [x.PREFIX+'RPC', x.PREFIX+'SEED']
    existing = secret_names()
    b.require(not existing.intersection(names), 'SECRET_COLLISION')
    cfg = backup.read_regular(Path('.env.local').absolute(), 65536, private=True).decode()
    url, _ = h.parse_config(cfg)
    payloads = [url.encode(), base64.b64encode(zlib.compress(x.canonical(value), 9))]
    b.save(ROOT/'secret-reservation.json', dict(names=names, previous=sorted(existing), at=b.stamp()))
    for i, (name, payload) in enumerate(zip(names, payloads)):
        gh(['secret', 'set', name, '--repo', REPO, '--app', 'actions'], payload)
        b.save(ROOT/('secret-created-'+str(i)+'.json'), dict(name=name, at=b.stamp()))
    b.require(secret_names() == existing.union(names), 'SECRETS_NOT_CONFIGURED')
    b.save(ROOT/'configured.json', prepared)
    print(json.dumps(dict(status='TEMPORARY_SECRETS_CONFIGURED', count=2)))


def dispatch(sha):
    value, prepared = seed()
    b.require(re.fullmatch('[0-9a-f]{40}', sha or ''), 'SHA_FORMAT')
    b.require(not (ROOT/'dispatch-reservation.json').exists(), 'ONE_DISPATCH_ONLY')
    b.require(b.read(ROOT/'configured.json') == prepared, 'CONFIGURATION_BINDING')
    b.require(api('branches/main')['commit']['sha'] == sha, 'REMOTE_MAIN_CHANGED')
    runs = api('actions/runs?head_sha='+sha+'&per_page=100')
    deployments = api('deployments?sha='+sha+'&per_page=100')
    b.require(runs['total_count'] == 0 and deployments == [], 'UNEXPECTED_AUTOMATIC_RUN_OR_DEPLOYMENT')
    cert = (ROOT/'recipient.pem').read_bytes()
    b.require(b.digest(cert) == value['certificate_sha256'], 'CERTIFICATE_CHANGED')
    inputs = dict(expected_sha=sha, seed_sha256=prepared['seed_sha256'],
                  recipient_certificate_base64=base64.b64encode(cert).decode())
    b.save(ROOT/'dispatch-reservation.json', dict(sha=sha, at=b.stamp(), maximum_runs=1,
                                               preexisting_runs=0, preexisting_deployments=0))
    gh(['api', '--method', 'POST', 'repos/'+REPO+'/actions/workflows/'+WORKFLOW+'/dispatches', '--input', '-'],
       x.canonical(dict(ref='main', inputs=inputs)))
    b.save(ROOT/'dispatched.json', dict(sha=sha, seed_sha256=prepared['seed_sha256']))
    print(json.dumps(dict(status='DISPATCH_ACCEPTED', sha=sha)))


def bound_run(run_id):
    run = api('actions/runs/'+str(run_id))
    dispatch = b.read(ROOT/'dispatched.json')
    b.require(run['head_sha'] == dispatch['sha'] and run['run_attempt'] == 1 and run['event'] == 'workflow_dispatch'
              and run['head_branch'] == 'main' and run['path'] == '.github/workflows/'+WORKFLOW, 'RUN_BINDING')
    return run


def wait_run():
    dispatch = b.read(ROOT/'dispatched.json')
    started = time.monotonic()
    while True:
        b.require(time.monotonic()-started < 1900 and x.dt.datetime.now(x.dt.timezone.utc) < x.DEADLINE, 'RUN_WAIT_BUDGET')
        runs = api('actions/workflows/'+WORKFLOW+'/runs?head_sha='+dispatch['sha']+'&per_page=100')
        b.require(runs['total_count'] <= 1, 'MULTIPLE_RUNS')
        if runs['total_count']:
            run = bound_run(runs['workflow_runs'][0]['id'])
            if run['status'] == 'completed':
                b.save(ROOT/'run.json', run)
                b.require(run['conclusion'] == 'success', 'REMOTE_RUN_FAILED')
                print(json.dumps(dict(status='CLOUD_SUCCESS', run_id=run['id'])))
                return
        time.sleep(30)


def restore(run_id):
    value, prepared = seed()
    run = bound_run(run_id)
    b.require(run['status'] == 'completed' and run['conclusion'] == 'success', 'RUN_NOT_SUCCESS')
    artifacts = api('actions/runs/'+str(run_id)+'/artifacts')
    b.require(artifacts['total_count'] == len(artifacts['artifacts']) == 1, 'ARTIFACT_COUNT')
    artifact = artifacts['artifacts'][0]
    name = 'steth-native-'+str(run_id)+'-1'
    start, end = (x.dt.datetime.fromisoformat(artifact[k].replace('Z', '+00:00')) for k in ('created_at', 'expires_at'))
    b.require(artifact['name'] == name and not artifact['expired'] and 0 < artifact['size_in_bytes'] <= backup.MAX_CIPHER
              and abs((end-start).total_seconds()-7*86400) <= 300, 'ARTIFACT_LIMIT_OR_RETENTION')
    destination = ROOT/'download'
    destination.mkdir(mode=0o700)
    gh(['run', 'download', str(run_id), '--repo', REPO, '--name', name, '--dir', str(destination)])
    b.require({p.name for p in destination.iterdir()} == {'native.cms', 'receipt.json'}, 'ARTIFACT_FILES')
    receipt = h.strict_json(backup.read_regular((destination/'receipt.json').absolute(), 65536))
    cipher = backup.read_regular((destination/'native.cms').absolute(), backup.MAX_CIPHER)
    logs = gh(['run', 'view', str(run_id), '--repo', REPO, '--log']).decode()
    logged = []
    for line in logs.splitlines():
        pos = line.find('{"event": "CLOUD_ENCRYPTED_RECEIPT"')
        if pos >= 0:
            observed = h.strict_json(line[pos:].encode()); observed.pop('event'); logged.append(observed)
    b.require(logged == [receipt], 'LOG_RECEIPT_MISMATCH')
    identity = dict(sha=run['head_sha'], run_id=str(run_id), attempt=1)
    b.require(receipt['cipher_sha256'] == b.digest(cipher) and receipt['cipher_bytes'] == len(cipher)
              and receipt['seed_sha256'] == prepared['seed_sha256'] and receipt['identity'] == identity
              and receipt['certificate_sha256'] == value['certificate_sha256'], 'CIPHER_BINDING')
    backup.read_regular((ROOT/'recipient.key').absolute(), 65536, private=True)
    packed = backup.openssl(['cms', '-decrypt', '-binary', '-inform', 'DER', '-recip', str(ROOT/'recipient.pem'),
                            '-inkey', str(ROOT/'recipient.key'), '-passin', 'pass:'], cipher)
    b.require(b.digest(packed) == receipt['packed_sha256'], 'PACKED_HASH')
    plain = x.unpack(packed, x.MAX_PLAIN)
    b.require(b.digest(plain) == receipt['plain_sha256'] and len(plain) == receipt['plain_bytes'], 'PLAIN_HASH')
    bundle = h.strict_json(plain)
    b.require(bundle['seed'] == value and bundle['identity'] == identity and
              bundle['result'] == dict(status='FIXED_INPUT_COMPLETE_NOT_ECONOMIC_EVIDENCE', rpc_methods=277,
                                       economic_attempts=0), 'BUNDLE_BINDING')
    x.validate_records(bundle['records'], value)
    with (ROOT/'restored.json').open('xb') as out:
        out.write(plain)
    b.save(ROOT/'restoration.json', dict(status='COMPLETE_ENCRYPTED_RESTORE', samples_added=44,
        controls_repeated=2, rpc_methods=277, receipt=receipt, protected_files=protect(),
        artifact_retention_seconds=(end-start).total_seconds()))
    print(json.dumps(dict(status='RESTORED', added=44, controls=2, economic_attempts=0)))


def cleanup():
    reservation = b.read(ROOT/'secret-reservation.json')
    if (ROOT/'dispatched.json').exists():
        runs = api('actions/workflows/'+WORKFLOW+'/runs?head_sha='+b.read(ROOT/'dispatched.json')['sha']+'&per_page=100')
        b.require(runs['total_count'] == 1, 'CLEANUP_RUN_COUNT')
        b.require(bound_run(runs['workflow_runs'][0]['id'])['status'] == 'completed', 'RUN_NOT_TERMINAL')
    b.require(reservation['names'] == [x.PREFIX+'RPC', x.PREFIX+'SEED'] and
              not set(reservation['names']).intersection(reservation['previous']), 'CLEANUP_OWNERSHIP')
    current = secret_names()
    for name in reservation['names']:
        if name in current:
            gh(['secret', 'delete', name, '--repo', REPO, '--app', 'actions'])
    b.require(secret_names() == set(reservation['previous']), 'CLEANUP_PREEXISTING_SET_CHANGED')
    b.save(ROOT/'cleanup.json', dict(status='ONLY_NEW_SECRETS_REMOVED', removed=2,
                                   remaining_new=0, preexisting_preserved=len(reservation['previous']), at=b.stamp()))
    print(json.dumps(b.read(ROOT/'cleanup.json')))


def evidence():
    value, _ = seed()
    bundle = b.read(ROOT/'restored.json')
    b.require(bundle['seed'] == value, 'RESTORED_SEED_CHANGED')
    x.validate_records(bundle['records'], value)
    cloud = {r['index']: r for r in bundle['records']}
    records = [old_record(i) if i < 8 else dict(cloud[i], source='cloud', bundle_sha256=b.sha(ROOT/'restored.json'))
               for i in range(52)]
    c = h.config()
    rows = [h.decode(h.strict_json(r['response'].encode()), h.grid(c)[i], c) for i, r in enumerate(records)]
    b.require(len({r['code_sha256'] for r in rows}) == 1 and all(a['timestamp'] < z['timestamp']
              for a, z in zip(rows, rows[1:])), 'FULL_GRID_CODE_OR_TIME_ORDER')
    return c, records, rows


def execute():
    c, records, rows = evidence()
    b.require(b.read(ROOT/'cleanup.json')['remaining_new'] == 0, 'CREDENTIAL_CLEANUP_REQUIRED')
    b.require(not (h.ROOT/'economic-attempt.json').exists(), 'OLD_ECONOMIC_ATTEMPT')
    b.save(ROOT/'inputs.json', dict(records=records, rows=rows, predecessor_freeze=b.sha(h.ROOT/'freeze.json'),
                                  cloud_bundle=b.sha(ROOT/'restored.json')))
    b.save(ROOT/'economic-attempt.json', dict(at=b.stamp(), attempt=1, contract_sha256=b.sha(h.CONTRACT),
                                             inputs_sha256=b.sha(ROOT/'inputs.json')))
    started = time.monotonic()
    result = h.measure([r['output_steth_wei'] for r in rows], c)
    b.require(time.monotonic()-started <= c['compute_seconds_maximum'], 'COMPUTE_BUDGET')
    b.save(ROOT/'result.json', result)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['prepare', 'configure', 'dispatch', 'wait', 'restore', 'cleanup', 'execute'])
    parser.add_argument('--sha')
    parser.add_argument('--run-id', type=int)
    args = parser.parse_args()
    try:
        b.require(args.step == 'cleanup' or b.read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'RUNNING_GATE_REQUIRED')
        b.require(args.step == 'cleanup' or x.dt.datetime.now(x.dt.timezone.utc) < x.DEADLINE, 'DEADLINE')
        if args.step == 'dispatch': dispatch(args.sha)
        elif args.step == 'restore': restore(args.run_id)
        elif args.step == 'wait': wait_run()
        else: globals()[args.step]()
    except Exception as exc:
        reason = str(exc)
        print(json.dumps(dict(status='LOCAL_FAILURE', reason=reason if re.fullmatch('[A-Z][A-Z0-9_]{0,95}', reason)
                              else 'UNEXPECTED_ERROR', error_type=type(exc).__name__)))
        raise SystemExit(1)
