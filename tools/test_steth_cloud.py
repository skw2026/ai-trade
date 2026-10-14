import base64
import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

import steth_cloud_input as x
import steth_cloud_local as local
import audit_steth_cloud as audit
import test_steth_cost_headroom as fixtures


def sample(index):
    c = x.h.config()
    value = fixtures.reply(c, x.h.grid(c)[index])
    value[0]['result']['timestamp'] = hex(1705000000+index*500000)
    return value


def records():
    result = []
    for index in x.INDICES:
        raw = x.canonical([dict(jsonrpc='2.0', id=1, result='0x1')] if index is None else sample(index))
        result.append(dict(index=index, request=x.request(index), response=raw.decode(),
            response_bytes=len(raw), response_sha256=x.b.digest(raw), status='NATIVE_VALID', attempted=True,
            curl_returncode=0, transport=dict(http='200', tcp=0.1, tls=0.2, total=0.3)))
    return result


def seed():
    return dict(schema='steth_cloud_seed_v1', sources=x.bindings(), indices=x.INDICES,
        deadline=x.DEADLINE.isoformat(), prior_operations_upper=141, control_operations_reserved=100,
        cloud_methods=277, prior_bytes=1000,
        controls={str(i): x.b.digest(x.canonical(x.decode(i, x.canonical(sample(i))))) for i in (0, 7)})


class CloudTests(unittest.TestCase):
    def test_fixed_method_budget_and_readonly(self):
        self.assertEqual(len(x.INDICES), 47)
        self.assertEqual(sum(len(x.request(i)) for i in x.INDICES), 277)
        self.assertEqual(x.INDICES[3:], list(range(8, 52)))
        with self.assertRaises(ValueError): x.request(6)
        with self.assertRaises(ValueError): x.request(True)

    def test_seed_source_and_budget_bound(self):
        s = seed(); x.validate_seed(s)
        for k, v in [('indices', [None]), ('cloud_methods', 278), ('sources', {}), ('prior_bytes', 1024**3)]:
            bad = copy.deepcopy(s); bad[k] = v
            with self.assertRaises(ValueError): x.validate_seed(bad)

    def test_complete_records_and_controls(self):
        self.assertEqual(len(x.validate_records(records(), seed())), 46)

    def test_missing_extra_reordered_rejected(self):
        r = records()
        for bad in (r[:-1], r+[r[0]], r[::-1]):
            with self.assertRaisesRegex(ValueError, 'COMPLETE_FIXED'): x.validate_records(bad, seed())

    def test_control_disagreement(self):
        s = seed(); s['controls']['7'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'CONTROL_DISAGREEMENT'): x.validate_records(records(), s)

    def test_record_hash_transport_status_fail_closed(self):
        for key, val in [('response_sha256', '0'*64), ('curl_returncode', 28), ('status', 'FAILED'),
                         ('attempted', False), ('request', []), ('response_bytes', 0)]:
            r = records(); r[-1][key] = val
            with self.assertRaisesRegex(ValueError, 'RAW_RECORD'): x.validate_records(r, seed())

    def test_unpack_size_trailing_and_truncation(self):
        raw = zlib.compress(b'x'*100)
        self.assertEqual(x.unpack(raw, 100), b'x'*100)
        for bad, limit in ((raw, 99), (raw+b'other', 100), (raw[:-1], 100)):
            with self.assertRaises(ValueError): x.unpack(bad, limit)

    def test_credential_only_stdin_and_readonly_wire(self):
        key = 'fakecredential1234567890'
        client = x.Client('https://eth-mainnet.g.alchemy.com/v2/'+key)
        raw = x.canonical([dict(jsonrpc='2.0', id=1, result='0x1')])
        response = raw+b'\n'+x.canonical(dict(http='200', tcp=0.1, tls=0.2, total=0.3))
        with patch.object(x, 'budget'), patch.object(x.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, response, b'')) as run:
            r = []; self.assertEqual(client.fetch(None, r, seed()), '0x1')
            args, kwargs = run.call_args
            self.assertNotIn(key, ' '.join(args[0]))
            self.assertIn(key.encode(), kwargs['input'])
            self.assertNotIn(key, json.dumps(r))
            self.assertEqual(args[0][args[0].index('--retry')+1], '0')

    def test_plain_and_encoded_key_echo_never_persisted(self):
        key = 'fakecredential1234567890'
        for echoed in (key, ''.join('\\u%04x' % ord(ch) for ch in key)):
            response = ('{"bad":"'+echoed+'"}\n'+json.dumps(dict(http='200', tcp=0, tls=0, total=0))).encode()
            with patch.object(x, 'budget'), patch.object(x.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, response, b'')):
                r = []
                with self.assertRaisesRegex(ValueError, 'CREDENTIAL_ECHO'):
                    x.Client('https://eth-mainnet.g.alchemy.com/v2/'+key).fetch(None, r, seed())
                self.assertNotIn('response', r[0])

    def test_transport_failure_is_not_retried(self):
        response = b'\n'+x.canonical(dict(http='000', tcp=0, tls=0, total=15))
        with patch.object(x, 'budget'), patch.object(x.subprocess, 'run', return_value=subprocess.CompletedProcess([], 28, response, b'')) as run:
            r = []
            with self.assertRaisesRegex(ValueError, 'TRANSPORT_OR_HTTP'):
                x.Client('https://eth-mainnet.g.alchemy.com/v2/fakecredential1234567890').fetch(None, r, seed())
            self.assertEqual(run.call_count, 1); self.assertEqual(r[0]['status'], 'FAILED')

    def test_independent_all_52_raw_grid(self):
        c = x.h.config(); rs = []
        for i in range(52):
            raw = x.canonical(sample(i))
            rs.append(dict(index=i, request=x.h.requests(x.h.grid(c)[i], c), response=raw.decode(),
                           response_sha256=x.b.digest(raw), response_bytes=len(raw)))
        outputs, decoded = audit.raw_outputs(rs, c)
        self.assertEqual(outputs, [10**18]*52); self.assertEqual(len(decoded), 52)
        for bad in (rs[:-1], rs[::-1]):
            with self.assertRaisesRegex(ValueError, 'AUDIT_FIXED_SET'): audit.raw_outputs(bad, c)
        rs[0]['request'][-1]['params'][0]['data'] = '0x'
        with self.assertRaisesRegex(ValueError, 'AUDIT_REQUEST'): audit.raw_outputs(rs, c)

    def test_real_crypto_roundtrip_no_plaintext_artifact(self):
        with tempfile.TemporaryDirectory() as name:
            p = Path(name)
            done = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-subj', '/CN=synthetic', '-keyout', str(p/'key'), '-out', str(p/'cert')], capture_output=True)
            self.assertEqual(done.returncode, 0)
            bundle = dict(seed={'synthetic': True}, records=[], result={'status': 'SYNTHETIC'}, identity={'attempt': 1})
            receipt = x.seal(bundle, (p/'cert').read_bytes(), p/'output')
            self.assertEqual({v.name for v in (p/'output').iterdir()}, {'native.cms', 'receipt.json'})
            cipher = (p/'output/native.cms').read_bytes()
            self.assertEqual(receipt['cipher_sha256'], x.b.digest(cipher))
            plain = x.unpack(x.backup.openssl(['cms', '-decrypt', '-binary', '-inform', 'DER', '-recip', str(p/'cert'),
                '-inkey', str(p/'key'), '-passin', 'pass:'], cipher), x.MAX_PLAIN)
            self.assertEqual(json.loads(plain), bundle)

    def test_secret_collision_stops_before_any_write(self):
        with tempfile.TemporaryDirectory() as name, patch.object(local, 'ROOT', Path(name)), \
                patch.object(local, 'seed', return_value=({}, {})), \
                patch.object(local, 'secret_names', return_value={x.PREFIX+'RPC'}), patch.object(local, 'gh') as gh:
            with self.assertRaisesRegex(ValueError, 'SECRET_COLLISION'): local.configure()
            gh.assert_not_called()

    def test_cleanup_only_new_reserved_names(self):
        with tempfile.TemporaryDirectory() as name, patch.object(local, 'ROOT', Path(name)):
            p = Path(name)
            x.b.save(p/'secret-reservation.json', dict(names=[x.PREFIX+'RPC', x.PREFIX+'SEED'], previous=['EXISTING']))
            with patch.object(local, 'secret_names', side_effect=[{'EXISTING', x.PREFIX+'RPC', x.PREFIX+'SEED'}, {'EXISTING'}]), \
                    patch.object(local, 'gh') as gh:
                local.cleanup()
                self.assertEqual([call.args[0][2] for call in gh.call_args_list], [x.PREFIX+'RPC', x.PREFIX+'SEED'])
                self.assertEqual(json.loads((p/'cleanup.json').read_text())['remaining_new'], 0)

    def test_preexisting_secret_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as name, patch.object(local, 'ROOT', Path(name)), patch.object(local, 'gh') as gh:
            x.b.save(Path(name)/'secret-reservation.json', dict(names=[x.PREFIX+'RPC', x.PREFIX+'SEED'], previous=[x.PREFIX+'RPC']))
            with self.assertRaisesRegex(ValueError, 'CLEANUP_OWNERSHIP'): local.cleanup()
            gh.assert_not_called()

    def test_bound_run_rejects_rerun_and_wrong_sha(self):
        with tempfile.TemporaryDirectory() as name, patch.object(local, 'ROOT', Path(name)):
            x.b.save(Path(name)/'dispatched.json', dict(sha='a'*40))
            good = dict(head_sha='a'*40, run_attempt=1, event='workflow_dispatch', head_branch='main',
                        path='.github/workflows/steth-cloud-input.yml')
            for field, value in [('head_sha', 'b'*40), ('run_attempt', 2), ('event', 'push'), ('head_branch', 'other')]:
                bad = dict(good); bad[field] = value
                with patch.object(local, 'api', return_value=bad):
                    with self.assertRaisesRegex(ValueError, 'RUN_BINDING'): local.bound_run(1)

    def test_existing_dispatch_reservation_blocks_second_dispatch(self):
        with tempfile.TemporaryDirectory() as name, patch.object(local, 'ROOT', Path(name)), \
                patch.object(local, 'seed', return_value=({}, {})), patch.object(local, 'api') as api:
            x.b.save(Path(name)/'dispatch-reservation.json', {})
            with self.assertRaisesRegex(ValueError, 'ONE_DISPATCH_ONLY'): local.dispatch('a'*40)
            api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
