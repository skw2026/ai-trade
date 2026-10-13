"""Local owner of private inputs/key and one approved cloud dispatch.

Secrets are stdin-only, created under a unique prefix and recorded before writes.
No previous evidence, validation gate, secret or existing file is overwritten.
"""
import argparse
import base64
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import zlib

import aave_cloud_input as x
a, r, t, backup = x.a, x.r, x.t, x.backup
ROOT = x.ROOT
AUTH = Path('.artifacts/aave-authenticated-access-20261013')
REPO = 'skw2026/ai-trade'
WORKFLOW = 'aave-cloud-input.yml'


def gh(args, payload=None):
    done = subprocess.run(['gh', *args], input=payload, capture_output=True, timeout=90)
    a.require(done.returncode == 0, 'GITHUB_OPERATION_FAILED')
    return done.stdout


def api(path):
    return json.loads(gh(['api', 'repos/'+REPO+'/'+path]))


def verify_protected():
    protected = json.loads((ROOT/'protected.json').read_text())
    a.require(all(Path(p).is_file() and a.digest(Path(p).read_bytes()) == h for p, h in protected.items()),
              'CLOUD_PREDECESSOR_CHANGED')
    return len(protected)


def old_cache():
    result = {}
    for root in (a.ROOT, AUTH):
        for folder in sorted((root/'operations').iterdir()):
            if not (folder/'record.json').exists():
                continue
            record = json.loads((folder/'record.json').read_text())
            if record.get('status') not in ('CONTROL_PASS', 'NATIVE_VALID', 'VALID'):
                continue
            req = json.loads((folder/'request.json').read_text())
            if req['method'] not in ('eth_chainId', 'eth_getTransactionReceipt', 'eth_getBlockByNumber', 'eth_call'):
                continue
            raw = (folder/'response.json').read_bytes()
            a.require(record['response_sha256'] == a.digest(raw), 'OLD_RESPONSE_CHANGED')
            value = r.envelope(json.loads(raw))
            name = x.key(req['method'], req['params'])
            if name in result:
                a.require(x.native_digest(req['method'], value) == x.native_digest(req['method'], result[name][2]),
                          'OLD_NATIVE_SOURCES_DISAGREE')
            result[name] = (req['method'], req['params'], value)
    return result


def prepare():
    os.umask(0o077)
    ROOT.chmod(0o700)
    a.require(not (ROOT/'seed.json').exists() and not (ROOT/'recipient.key').exists(), 'PREPARATION_ALREADY_EXISTS')
    a.require(a.now() < x.DEADLINE, 'CLOUD_TIME_BUDGET')
    protected = json.loads((AUTH/'protected.json').read_text())
    for p in AUTH.rglob('*'):
        if p.is_file() and '__pycache__' not in p.parts:
            protected[str(p)] = a.digest(p.read_bytes())
    a.save(ROOT/'protected.json', protected)
    verify_protected()
    catalog = (a.ROOT/'annual-catalog.json').read_bytes()
    a.require(a.digest(catalog) == x.CATALOG_SHA, 'CATALOG_CHANGED')
    rows = r.catalog()
    # Existing OpenSSL utility creates a fresh local-only recipient. Never reuse
    # a previous experiment's key or upload a private key to any service.
    done = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '8',
        '-subj', '/CN=ai-trade-aave-private-input-20261013', '-keyout', str(ROOT/'recipient.key'),
        '-out', str(ROOT/'recipient.pem')], capture_output=True, timeout=90)
    a.require(done.returncode == 0, 'RECIPIENT_GENERATION_FAILED')
    (ROOT/'recipient.key').chmod(0o600)
    certificate = backup.read_regular((ROOT/'recipient.pem').absolute(), 65536)
    backup.check_certificate(certificate)
    cache = old_cache()
    first = rows[0]
    specs = [('eth_getTransactionReceipt', [first['transactionHash']]),
             ('eth_getBlockByNumber', [first['blockNumber'], False]), ('eth_call', x.q.request(first['blockNumber']))]
    logical = sum(p.stat().st_size for root in (a.ROOT, AUTH) for p in root.rglob('*') if p.is_file())
    logical += json.loads((a.ROOT/'control.json').read_text())['imported_input_bytes']
    seed = {'schema': 'aave_cloud_seed_v1', 'events': rows, 'catalog_sha256': x.CATALOG_SHA,
            'rows_sha256': x.ROW_SHA, 'sources': x.source_bindings(), 'plan_sha256': a.digest(x.PLAN.read_bytes()),
            'certificate_sha256': a.digest(certificate), 'reserved_operations': 500,
            'prior_logical_bytes': logical, 'deadline_utc': x.DEADLINE.isoformat(),
            'fixed_control_value_sha256': [x.native_digest(m, cache[x.key(m, p)][2]) for m, p in specs],
            'predecessor_gate_sha256': a.digest((AUTH/'validation-state.json').read_bytes())}
    x.validate_seed(seed)
    raw = x.canonical(seed)
    encoded = base64.b64encode(zlib.compress(raw, 9))
    a.require(9*40000 < len(encoded) <= 10*40000, 'PRIVATE_SEED_SECRET_SIZING')
    a.save(ROOT/'seed.json', seed)
    a.save(ROOT/'prepared.json', {'seed_sha256': a.digest(raw), 'encoded_bytes': len(encoded),
           'protected_count': len(protected), 'plan_sha256': seed['plan_sha256'], 'certificate_sha256': a.digest(certificate)})
    print(json.dumps({'status': 'PRIVATE_CLOUD_INPUT_PREPARED', 'protected': len(protected),
                      'native_methods': 5773, 'new_rpc': 0}), flush=True)


def seed():
    verify_protected()
    value = json.loads((ROOT/'seed.json').read_text())
    x.validate_seed(value)
    prepared = json.loads((ROOT/'prepared.json').read_text())
    a.require(a.digest(x.canonical(value)) == prepared['seed_sha256'], 'LOCAL_SEED_CHANGED')
    return value, prepared


def read_credential():
    raw = backup.read_regular(Path('.env.local').absolute(), 65536, private=True)
    return t.parse_config(raw.decode())[0]


def configure():
    value, prepared = seed()
    a.require(not (ROOT/'secret-reservation.json').exists(), 'SECRET_CONFIGURATION_ONCE_ONLY')
    names = [x.PREFIX+'RPC'] + [x.PREFIX+'SEED_%02d' % i for i in range(x.SEED_PARTS)]
    existing = api('actions/secrets')
    a.require(existing['total_count'] <= 30 and not ({s['name'] for s in existing['secrets']} & set(names)),
              'SECRET_COLLISION_OR_PAGINATION')
    url = read_credential()
    encoded = base64.b64encode(zlib.compress(x.canonical(value), 9))
    payloads = [url.encode()] + [encoded[i*40000:(i+1)*40000] for i in range(x.SEED_PARTS)]
    a.require(all(0 < len(v) <= 40000 for v in payloads), 'SECRET_PAYLOAD_SIZE')
    a.save(ROOT/'secret-reservation.json', {'names': names, 'seed_sha256': prepared['seed_sha256'], 'started_utc': a.now().isoformat()})
    for index, (name, payload) in enumerate(zip(names, payloads)):
        gh(['secret', 'set', name, '--repo', REPO, '--app', 'actions'], payload)
        a.save(ROOT/('secret-created-%02d.json' % index), {'name': name, 'created_utc': a.now().isoformat()})
    listed = api('actions/secrets')
    a.require(set(names) <= {s['name'] for s in listed['secrets']}, 'SECRET_CONFIGURATION_INCOMPLETE')
    a.save(ROOT/'configured.json', {'status': 'CONFIGURED', 'names': names, 'seed_sha256': prepared['seed_sha256']})
    print(json.dumps({'status': 'PRIVATE_RUN_INPUTS_CONFIGURED', 'temporary_secrets': len(names)}), flush=True)


def dispatch(sha):
    value, prepared = seed()
    a.require(re.fullmatch(r'[0-9a-f]{40}', sha) is not None, 'EXPECTED_SHA_FORMAT')
    a.require(json.loads((ROOT/'configured.json').read_text())['seed_sha256'] == prepared['seed_sha256'], 'CONFIGURATION_REQUIRED')
    a.require(not (ROOT/'dispatch-reservation.json').exists(), 'DISPATCH_ALREADY_RESERVED')
    a.require(api('branches/main')['commit']['sha'] == sha, 'REMOTE_MAIN_CHANGED')
    runs = api('actions/workflows/'+WORKFLOW+'/runs?head_sha='+sha+'&per_page=10')
    a.require(runs['total_count'] == 0, 'EXISTING_CLOUD_RUN')
    certificate = (ROOT/'recipient.pem').read_bytes()
    a.require(a.digest(certificate) == value['certificate_sha256'], 'LOCAL_CERTIFICATE_CHANGED')
    inputs = {'expected_sha': sha, 'seed_sha256': prepared['seed_sha256'],
              'recipient_certificate_base64': base64.b64encode(certificate).decode()}
    a.save(ROOT/'dispatch-reservation.json', {'sha': sha, 'seed_sha256': prepared['seed_sha256'],
           'started_utc': a.now().isoformat(), 'maximum_runs': 1})
    gh(['api', '--method', 'POST', 'repos/'+REPO+'/actions/workflows/'+WORKFLOW+'/dispatches', '--input', '-'],
       x.canonical({'ref': 'main', 'inputs': inputs}))
    a.save(ROOT/'dispatched.json', {'sha': sha, 'seed_sha256': prepared['seed_sha256'], 'status': 'DISPATCH_ACCEPTED_NOT_RUN_COMPLETE'})
    print(json.dumps({'status': 'ONE_CLOUD_DISPATCH_ACCEPTED', 'sha': sha}), flush=True)


def restore(run_id):
    value, prepared = seed()
    dispatch = json.loads((ROOT/'dispatched.json').read_text())
    run = api('actions/runs/'+str(run_id))
    a.require(run['head_sha'] == dispatch['sha'] and run['event'] == 'workflow_dispatch'
              and run['run_attempt'] == 1 and run['status'] == 'completed'
              and run['conclusion'] == 'success' and run['path'] == '.github/workflows/'+WORKFLOW,
              'CLOUD_RUN_NOT_COMPLETE_OR_BOUND')
    items = api('actions/runs/'+str(run_id)+'/artifacts')
    name = 'aave-native-'+str(run_id)+'-1'
    a.require(items['total_count'] == 1 and len(items['artifacts']) == 1, 'CLOUD_ARTIFACT_COUNT')
    artifact = items['artifacts'][0]
    start, end = (a.dt.datetime.fromisoformat(artifact[k].replace('Z', '+00:00')) for k in ('created_at', 'expires_at'))
    a.require(artifact['name'] == name and not artifact['expired'] and 0 < artifact['size_in_bytes'] <= backup.MAX_CIPHER
              and abs((end-start).total_seconds()-7*86400) <= 300, 'CLOUD_ARTIFACT_RETENTION_OR_SIZE')
    destination = ROOT/'download'
    destination.mkdir(mode=0o700)
    gh(['run', 'download', str(run_id), '--repo', REPO, '--name', name, '--dir', str(destination)])
    a.require({p.name for p in destination.iterdir()} == {'native.cms', 'receipt.json'}, 'CLOUD_DOWNLOAD_FILES')
    receipt = json.loads(backup.read_regular((destination/'receipt.json').absolute(), 65536))
    cipher = backup.read_regular((destination/'native.cms').absolute(), backup.MAX_CIPHER)
    logs = gh(['run', 'view', str(run_id), '--repo', REPO, '--log']).decode()
    receipts = []
    for line in logs.splitlines():
        marker = '{"event": "CLOUD_ENCRYPTED_RECEIPT"'
        at = line.find(marker)
        if at >= 0:
            observed = json.loads(line[at:])
            observed.pop('event')
            receipts.append(observed)
    a.require(receipts == [receipt], 'ARTIFACT_LOG_RECEIPT_DISAGREEMENT')
    a.require(receipt['cipher_sha256'] == a.digest(cipher) and receipt['cipher_bytes'] == len(cipher)
              and receipt['seed_sha256'] == prepared['seed_sha256']
              and receipt['certificate_sha256'] == value['certificate_sha256']
              and receipt['identity'] == {'sha': dispatch['sha'], 'run_id': str(run_id), 'attempt': 1}, 'CIPHER_BINDING')
    backup.read_regular((ROOT/'recipient.key').absolute(), 65536, private=True)
    packed = backup.openssl(['cms', '-decrypt', '-binary', '-inform', 'DER', '-recip', str(ROOT/'recipient.pem'),
                            '-inkey', str(ROOT/'recipient.key'), '-passin', 'pass:'], cipher)
    a.require(a.digest(packed) == receipt['packed_sha256'], 'PACKED_HASH')
    plain = x.unpack(packed, x.MAX_PLAIN)
    a.require(a.digest(plain) == receipt['plain_sha256'] and len(plain) == receipt['plain_bytes'], 'PLAINTEXT_HASH')
    bundle = json.loads(plain)
    cache, flows = x.audit_bundle(bundle, value, classify=True)
    compared = 0
    for name, (method, params, old) in old_cache().items():
        if name in cache:
            a.require(x.native_digest(method, old) == x.native_digest(method, cache[name]), 'OLD_NEW_NATIVE_DISAGREEMENT')
            compared += 1
    # Persist only into a new private local tree, after complete structural checks.
    with (ROOT/'restored-native.json').open('xb') as handle:
        handle.write(plain)
    a.save(ROOT/'flows.json', flows)
    statuses = Counter(v['status'] for v in flows.values())
    reasons = Counter(reason.split(':')[0] for v in flows.values() for reason in v['unresolved_reasons'])
    signs = Counter('positive' if v['weth_dex_net_sale_wei'] > 0 else 'negative' if v['weth_dex_net_sale_wei'] < 0 else 'zero'
                    for v in flows.values() if v['weth_dex_net_sale_wei'] is not None)
    report = {'status': 'RESTORED_NATIVE_AND_WETH_BOUNDARIES_COMPLETE_NOT_ECONOMIC_ADMISSION',
              'sha': dispatch['sha'], 'run_id': run_id, 'transactions': len(flows), 'old_methods_compared': compared,
              'flow_statuses': dict(statuses), 'reasons': dict(reasons), 'known_signs': dict(signs),
              'protected_files': verify_protected(), 'cipher_receipt': receipt,
              'artifact_retention_seconds': (end-start).total_seconds(), 'rpc_methods': receipt['rpc_methods'],
              'economic_attempts': 0, 'signal_admitted': False, 'g4': 'NOT_ACHIEVED'}
    a.save(ROOT/'restoration.json', report)
    print(json.dumps(report, sort_keys=True), flush=True)


def cleanup(run_id):
    # Only this batch's recorded newly-created names; never a preexisting secret.
    dispatch = json.loads((ROOT/'dispatched.json').read_text())
    run = api('actions/runs/'+str(run_id))
    a.require(run['head_sha'] == dispatch['sha'] and run['status'] == 'completed' and run['run_attempt'] == 1,
              'CLEANUP_REQUIRES_TERMINAL_BOUND_RUN')
    reservation = json.loads((ROOT/'secret-reservation.json').read_text())
    for index, name in enumerate(reservation['names']):
        created = json.loads((ROOT/('secret-created-%02d.json' % index)).read_text())
        a.require(created['name'] == name and name.startswith(x.PREFIX), 'CLEANUP_OWNERSHIP')
        a.require(not (ROOT/('secret-removed-%02d.json' % index)).exists(), 'CLEANUP_ALREADY_RECORDED')
        gh(['secret', 'delete', name, '--repo', REPO, '--app', 'actions'])
        a.save(ROOT/('secret-removed-%02d.json' % index), {'name': name, 'removed_utc': a.now().isoformat()})
    print(json.dumps({'status': 'TEMPORARY_SECRETS_REMOVED', 'count': len(reservation['names'])}), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['prepare', 'configure', 'dispatch', 'restore', 'cleanup'])
    parser.add_argument('--sha')
    parser.add_argument('--run-id', type=int)
    args = parser.parse_args()
    try:
        gate = json.loads((ROOT/'validation-state.json').read_text())
        # Terminal credential removal is independent security cleanup, not a
        # dependent acquisition or a way to clear BLOCKED after a remote failure.
        a.require(gate['status'] == 'RUNNING' or args.step == 'cleanup', 'CLOUD_LOCAL_RUNNING_GATE_REQUIRED')
        if args.step == 'prepare': prepare()
        elif args.step == 'configure': configure()
        elif args.step == 'dispatch': dispatch(args.sha)
        elif args.step == 'restore': restore(args.run_id)
        else: cleanup(args.run_id)
    except Exception as exc:
        reason = str(exc)
        print(json.dumps({'status': 'CLOUD_LOCAL_FAILURE', 'reason': reason if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', reason)
                          else 'UNEXPECTED_ERROR', 'error_type': type(exc).__name__}), flush=True)
        raise SystemExit(1)
