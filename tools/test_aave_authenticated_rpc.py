import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import aave_authenticated_rpc as x

KEY = 'synthetic_not_a_real_key_1234567890'
URL = 'https://eth-mainnet.g.alchemy.com/v2/'+KEY


class AuthenticatedRpcTests(unittest.TestCase):
    def test_config(self):
        for value in [URL, '"'+URL+'"', "'"+URL+"'"]:
            self.assertEqual(x.parse_config('OTHER=ignored\nexport ALCHEMY_RPC_URL='+value), (URL, KEY))

    def test_config_rejects_missing_duplicate_wrong_host_and_injection(self):
        for text in ['', 'ALCHEMY_RPC_URL='+URL+'\nALCHEMY_RPC_URL='+URL,
                     'ALCHEMY_RPC_URL='+URL.replace('https:', 'http:'),
                     'ALCHEMY_RPC_URL='+URL.replace('.com/', '.com.evil/'),
                     'ALCHEMY_RPC_URL='+URL+'?redirect=x',
                     'ALCHEMY_RPC_URL='+URL+'"\ninsecure',
                     'ALCHEMY_RPC_URL=https://user:password@eth-mainnet.g.alchemy.com/v2/'+KEY]:
            with self.subTest(text=text), self.assertRaises(ValueError): x.parse_config(text)

    def test_methods_are_narrowly_read_only(self):
        for method, params in [('eth_sendRawTransaction', ['0x01']), ('eth_accounts', []),
                               ('eth_getBlockByNumber', ['latest', False]),
                               ('eth_getBlockByNumber', ['0x1', 0]),
                               ('eth_call', [{'to': x.c.POOL, 'data': '0x'}, '0x1'])]:
            with self.subTest(method=method), self.assertRaises(ValueError): x.validate_request(method, params)
        x.validate_request('eth_call', x.q.request('0x12087f7'))

    def exercise(self, body, status=b'200', returncode=0, exception=None, valid=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'call'
            client = x.Client(config_text='ALCHEMY_RPC_URL='+URL)
            stream = io.StringIO()
            response = subprocess.CompletedProcess([], returncode, body+b'\n'+status, URL.encode())
            with patch.object(x.subprocess, 'run', side_effect=exception, return_value=response) as run, \
                    patch.object(x.time, 'sleep'), contextlib.redirect_stdout(stream):
                if valid:
                    self.assertEqual(client.fetch(root, 'eth_chainId', [], lambda v: x.a.require(v == '0x1', 'CHAIN_ID')), '0x1')
                else:
                    with self.assertRaises(RuntimeError) as caught:
                        client.fetch(root, 'eth_chainId', [], lambda v: x.a.require(v == '0x1', 'CHAIN_ID'))
                    self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(run.call_count, 1)
            argv = run.call_args.args[0]
            self.assertEqual(argv[1], '--disable')
            self.assertNotIn(KEY, repr(argv))
            self.assertNotIn('--location', argv)
            self.assertNotIn('--insecure', argv)
            self.assertEqual(argv[argv.index('--connect-timeout')+1], '15')
            self.assertEqual(argv[argv.index('--max-time')+1], '30')
            self.assertIn(URL.encode(), run.call_args.kwargs['input'])
            self.assertNotIn(KEY, repr(client))
            self.assertNotIn(KEY, stream.getvalue())
            for path in root.iterdir(): self.assertNotIn(KEY.encode(), path.read_bytes())
            record = json.loads((root/'record.json').read_text())
            self.assertEqual(record['status'], 'VALID' if valid else 'FAILED')
            if valid:
                self.assertEqual(x.cached(root, 'eth_chainId', [], lambda _: None), '0x1')
                with self.assertRaises(ValueError): client.fetch(root, 'eth_chainId', [], lambda _: None)
                (root/'response.json').write_text('{}')
                with self.assertRaises(ValueError): x.cached(root, 'eth_chainId', [], lambda _: None)
            return record

    def test_success_no_key_in_argv_artifacts_or_logs_and_cache_bound(self):
        self.exercise(b'{"jsonrpc":"2.0","id":1,"result":"0x1"}')

    def test_auth_rate_redirect_and_null_stop_without_retry(self):
        for status, body in [(b'401', b'unauthorized'), (b'429', b'rate limited'),
                             (b'302', b'redirect'), (b'200', b'{"jsonrpc":"2.0","id":1,"result":null}'),
                             (b'200', b'{"jsonrpc":"2.0","id":1,"error":{"code":-1}}')]:
            with self.subTest(status=status, body=body): self.exercise(body, status, valid=False)

    def test_direct_key_echo_not_persisted(self):
        self.exercise(URL.encode(), b'401', valid=False)

    def test_json_escaped_key_echo_not_persisted(self):
        escaped = ''.join('\\u%04x' % ord(c) for c in KEY)
        self.exercise(('{"error":"'+escaped+'"}').encode(), b'401', valid=False)

    def test_timeout_and_unexpected_exception_do_not_expose_credentials(self):
        for exc in [subprocess.TimeoutExpired(['curl'], 35, output=URL), ValueError(URL)]:
            with self.subTest(kind=type(exc).__name__): self.exercise(b'', exception=exc, valid=False)

    def test_wrong_network_rejected(self):
        self.exercise(b'{"jsonrpc":"2.0","id":1,"result":"0xa"}', valid=False)


if __name__ == '__main__': unittest.main()
