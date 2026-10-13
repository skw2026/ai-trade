import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

import aave_cloud_input as x
from test_aave_authenticated_rpc import KEY, URL, AuthenticatedRpcTests


def fixture():
    tx, blockhash = '0x'+'1'*64, '0x'+'2'*64
    row = {'transactionHash': tx, 'transactionIndex': '0x0', 'blockNumber': '0x1',
           'timeStamp': '0x65a00000', 'logIndex': '0x0', 'address': x.c.POOL,
           'topics': [x.c.TOPIC, '0x'+'0'*24+x.c.WETH[2:], '0x'+'0'*64, '0x'+'0'*64],
           'data': '0x'+'0'*256}
    log = {k: v for k, v in row.items() if k != 'timeStamp'}
    log.update(blockHash=blockhash, removed=False)
    receipt = {'transactionHash': tx, 'transactionIndex': '0x0', 'blockNumber': '0x1',
               'blockHash': blockhash, 'status': '0x1', 'logs': [log]}
    block = {'number': '0x1', 'hash': blockhash, 'timestamp': row['timeStamp'], 'transactions': [tx]}
    reserve = '0x'+''.join(format(1 if i == 8 else 0, '064x') for i in range(15))
    specs = [('eth_getTransactionReceipt', [tx]), ('eth_getBlockByNumber', ['0x1', False]),
             ('eth_call', x.q.request('0x1'))]
    return row, [receipt, block, reserve], specs


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.row, self.values, self.specs = fixture()
        self.blocks, self.plan = x.native_plan([self.row])
        self.req = x.request_batch(self.specs)
        self.responses = [dict(jsonrpc='2.0', id=i, result=v) for i, v in enumerate(self.values, 1)]

    def test_native_plan_and_validation(self):
        self.assertEqual(self.specs, self.plan)
        x.verify_values(self.specs, self.values, {}, self.blocks)

    def test_whole_receipt_logs_still_bound(self):
        bad = copy.deepcopy(self.values)
        bad[0]['logs'][0]['blockHash'] = '0x'+'3'*64
        with self.assertRaisesRegex(ValueError, 'LOG_IDENTITY'): x.verify_values(self.specs, bad, {}, self.blocks)

    def test_header_timestamp_and_transaction_position(self):
        for field, value in [('timestamp', '0x1'), ('transactions', [])]:
            bad = copy.deepcopy(self.values); bad[1][field] = value
            with self.assertRaises(ValueError): x.verify_values(self.specs, bad, {}, self.blocks)

    def test_historical_reserve_width_and_time(self):
        bad = copy.deepcopy(self.values); bad[2] = '0x01'
        with self.assertRaises(ValueError): x.verify_values(self.specs, bad, {}, self.blocks)

    def test_wrong_chain_rejects(self):
        with self.assertRaisesRegex(ValueError, 'MAINNET'):
            x.verify_values([('eth_chainId', [])], ['0xa'], {}, {})

    def test_batch_order_by_id_not_response_position(self):
        self.assertEqual(x.decode_batch(self.responses[::-1], self.req), self.values)

    def test_missing_duplicate_boolean_error_and_null_ids(self):
        examples = [self.responses[:2], [self.responses[0]]*3, {}]
        for update in ({'id': True}, {'id': 4}, {'result': None}, {'error': {'code': 429}}):
            v = copy.deepcopy(self.responses); v[0].update(update); examples.append(v)
        for value in examples:
            with self.subTest(value=value), self.assertRaises(ValueError): x.decode_batch(value, self.req)

    def test_method_and_batch_allowlist(self):
        for specs in ([], self.specs*4, self.specs*2, [('eth_sendRawTransaction', ['0x01'])]):
            with self.assertRaises(ValueError): x.request_batch(specs)

    def exercise(self, raw=None, code=0, http='200', raises=False):
        if raw is None: raw = json.dumps(self.responses).encode()
        timing = json.dumps({'http': http, 'tcp': .1, 'tls': .2, 'total': .3}).encode()
        result = subprocess.CompletedProcess([], code, raw+b'\n'+timing, URL.encode())
        records = []
        with patch.object(x.subprocess, 'run', return_value=result) as command, patch.object(x.time, 'sleep'):
            client = x.Client(config_text='ALCHEMY_RPC_URL='+URL)
            if raises:
                with self.assertRaises(ValueError) as caught: client.fetch_batch(self.specs, records, lambda: None)
                self.assertNotIn(KEY, str(caught.exception))
            else:
                self.assertEqual(client.fetch_batch(self.specs, records, lambda: None), self.values)
        self.assertEqual(command.call_count, 1)
        self.assertNotIn(KEY, repr(command.call_args.args))
        self.assertIn(URL.encode(), command.call_args.kwargs['input'])
        argv = command.call_args.args[0]
        self.assertEqual(argv[1], '--disable')
        self.assertNotIn('--location', argv); self.assertNotIn('--insecure', argv)
        self.assertEqual(argv[argv.index('--retry')+1], '0')
        self.assertNotIn(KEY, json.dumps(records))
        self.assertEqual(len(records), 1)
        return records

    def test_private_transport_success(self): self.exercise()
    def test_timeout_kept_no_retry(self): self.exercise(b'', code=28, http='000', raises=True)
    def test_429_stops_without_retry(self): self.exercise(b'{}', http='429', raises=True)
    def test_key_echo_not_retained(self): self.exercise(URL.encode(), http='403', raises=True)
    def test_json_escaped_key_echo_not_retained(self):
        raw = ('{"error":"'+''.join('\\u%04x' % ord(c) for c in KEY)+'"}').encode()
        self.exercise(raw, raises=True)
    def test_item_error_not_native_valid(self):
        value = copy.deepcopy(self.responses); value[0]['error'] = {'code': -1}
        self.exercise(json.dumps(value).encode(), raises=True)

    def test_cross_provider_projection_keeps_every_log(self):
        old = self.values[0]
        new = dict(old, extraProviderMetadata='not part of the WETH ledger')
        self.assertEqual(x.native_digest(self.specs[0][0], old), x.native_digest(self.specs[0][0], new))
        new = copy.deepcopy(old); new['logs'][0]['data'] = '0x01'
        self.assertNotEqual(x.native_digest(self.specs[0][0], old), x.native_digest(self.specs[0][0], new))

    def test_compression_trailing_truncated_and_size_reject(self):
        raw = zlib.compress(b'abcd')
        self.assertEqual(x.unpack(raw, 4), b'abcd')
        for value, cap in [(raw, 3), (raw[:-1], 100), (raw+b'junk', 100), (raw+raw, 100)]:
            with self.assertRaises(ValueError): x.unpack(value, cap)

    def test_resource_deadline_and_method_cap(self):
        seed = {'reserved_operations': 500, 'prior_logical_bytes': 0}
        with patch.object(x.a, 'now', return_value=x.DEADLINE):
            with self.assertRaisesRegex(ValueError, 'TIME'): x.resources(seed, [])
        with patch.object(x.a, 'now', return_value=x.DEADLINE-x.dt.timedelta(seconds=1)):
            self.assertEqual(x.resources(seed, [])['rpc_methods'], 0)
            with self.assertRaisesRegex(ValueError, 'METHOD'):
                x.resources(seed, [{'request': [None]*8000}])
            seed['prior_logical_bytes'] = x.a.MAX_INPUT
            with self.assertRaisesRegex(ValueError, 'BYTE'): x.resources(seed, [])

    def small_seed(self):
        return {'events': [self.row], 'reserved_operations': 500, 'prior_logical_bytes': 0,
                'fixed_control_value_sha256': [x.native_digest(m, v) for (m, p), v in zip(self.specs, self.values)]}

    def test_end_to_end_native_and_offline_audit_no_keccak_or_network(self):
        seed, records = self.small_seed(), []
        parent = self
        class FakeClient:
            def fetch_batch(self, specs, sink, before_send):
                request = x.request_batch(specs)
                values = ['0x1']+parent.values
                raw = json.dumps([dict(jsonrpc='2.0', id=i, result=v) for i, v in enumerate(values, 1)]).encode()
                sink.append({'request': request, 'attempted': True, 'status': 'RESPONSE_VALID_NOT_NATIVE_VALIDATED',
                             'response': raw.decode(), 'response_sha256': x.a.digest(raw), 'response_bytes': len(raw),
                             'transport': {'http': '200'}, 'curl_returncode': 0})
                before_send()
                return values
        with patch.object(x, 'validate_seed', return_value=(self.blocks, self.plan)), \
             patch.object(x.a, 'now', return_value=x.DEADLINE-x.dt.timedelta(seconds=1)), \
             patch.object(x.boundary.f, 'keccak', side_effect=AssertionError('Linux path must not invoke keccak')), \
             redirect_stdout(io.StringIO()):
            result = x.acquire(seed, FakeClient(), records)
            bundle = {'seed': seed, 'records': records, 'result': result}
            cache, flows = x.audit_bundle(bundle, seed)
            self.assertEqual(len(cache), 4); self.assertEqual(flows, {})
            records[0]['response'] += ' '
            with self.assertRaisesRegex(ValueError, 'RESPONSE_CHANGED'): x.audit_bundle(bundle, seed)

    def test_control_mismatch_stops_before_bulk(self):
        seed = self.small_seed(); seed['fixed_control_value_sha256'][0] = '0'*64
        client = unittest.mock.Mock()
        client.fetch_batch.return_value = ['0x1']+self.values
        with patch.object(x, 'validate_seed', return_value=(self.blocks, self.plan)):
            with self.assertRaisesRegex(ValueError, 'CONTROL_DISAGREEMENT'): x.acquire(seed, client, [])
        self.assertEqual(client.fetch_batch.call_count, 1)

    def test_seed_population_cannot_be_replaced(self):
        with self.assertRaisesRegex(ValueError, 'FIXED_CATALOG_CHANGED'):
            x.validate_seed(dict(self.small_seed(), schema='aave_cloud_seed_v1', catalog_sha256=x.CATALOG_SHA,
                                 rows_sha256=x.ROW_SHA))

    def test_native_missing_not_unknown(self):
        seed = self.small_seed()
        bundle = {'seed': seed, 'records': [], 'result': {'status': 'NATIVE_COMPLETE_NOT_FLOW_OR_ECONOMIC_ADMISSION'}}
        with patch.object(x, 'validate_seed', return_value=(self.blocks, self.plan)):
            with self.assertRaisesRegex(ValueError, 'METHOD_SET'): x.audit_bundle(bundle, seed)

    def test_archive_crypto_roundtrip_and_hash_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); key, cert = root/'key.pem', root/'cert.pem'
            generated = subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                '-subj', '/CN=synthetic-only', '-keyout', str(key), '-out', str(cert)], capture_output=True, timeout=30)
            self.assertEqual(generated.returncode, 0)
            bundle = {'seed': {'synthetic': True}, 'records': [], 'identity': {'sha': '0'*40},
                      'result': {'status': 'SYNTHETIC_ONLY'}}
            receipt = x.seal(bundle, cert.read_bytes(), root/'out')
            cipher = (root/'out/native.cms').read_bytes()
            self.assertEqual(receipt['cipher_sha256'], x.a.digest(cipher))
            packed = x.backup.openssl(['cms', '-decrypt', '-binary', '-inform', 'DER', '-recip', str(cert),
                                      '-inkey', str(key), '-passin', 'pass:'], cipher)
            plain = x.unpack(packed, 10000)
            self.assertEqual(json.loads(plain), bundle)
            self.assertEqual(receipt['plain_sha256'], x.a.digest(plain))
            with self.assertRaises(FileExistsError): x.seal(bundle, cert.read_bytes(), root/'out')


if __name__ == '__main__': unittest.main()
