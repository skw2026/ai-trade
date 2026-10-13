from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import aave_cloud_local as l
from test_aave_authenticated_rpc import URL, KEY


class LocalControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = patch.object(l, 'ROOT', self.root); p.start(); self.addCleanup(p.stop)

    def test_gh_uses_stdin_and_never_exposes_stderr(self):
        result = subprocess.CompletedProcess([], 0, b'{}', URL.encode())
        with patch.object(l.subprocess, 'run', return_value=result) as run:
            self.assertEqual(l.gh(['secret', 'set', 'SYNTHETIC'], URL.encode()), b'{}')
        self.assertNotIn(KEY, repr(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['input'], URL.encode())
        with patch.object(l.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', URL.encode())):
            with self.assertRaisesRegex(ValueError, '^GITHUB_OPERATION_FAILED$'): l.gh(['synthetic'])

    def test_configure_records_ownership_and_stdin_only(self):
        names = [l.x.PREFIX+'RPC']+[l.x.PREFIX+'SEED_%02d' % i for i in range(10)]
        responses = [{'total_count': 0, 'secrets': []}, {'total_count': 11, 'secrets': [{'name': n} for n in names]}]
        with patch.object(l, 'seed', return_value=({}, {'seed_sha256': 'fixed'})), \
             patch.object(l, 'api', side_effect=responses), patch.object(l, 'read_credential', return_value=URL), \
             patch.object(l.base64, 'b64encode', return_value=b'x'*394304), \
             patch.object(l, 'gh', return_value=b'') as gh, redirect_stdout(io.StringIO()) as stream:
            l.configure()
        self.assertEqual(gh.call_count, 11)
        for call in gh.call_args_list: self.assertNotIn(KEY, repr(call.args[0]))
        self.assertEqual(gh.call_args_list[0].args[1], URL.encode())
        self.assertNotIn(KEY, stream.getvalue())
        self.assertEqual(len(list(self.root.glob('secret-created-*.json'))), 11)
        self.assertEqual(json.loads((self.root/'configured.json').read_text())['names'], names)

    def test_existing_secret_never_overwritten(self):
        existing = {'total_count': 1, 'secrets': [{'name': l.x.PREFIX+'RPC'}]}
        with patch.object(l, 'seed', return_value=({}, {})), patch.object(l, 'api', return_value=existing), \
             patch.object(l, 'gh') as gh:
            with self.assertRaisesRegex(ValueError, 'COLLISION'): l.configure()
            gh.assert_not_called()
        self.assertFalse((self.root/'secret-reservation.json').exists())

    def test_prior_partial_configuration_is_not_retried(self):
        (self.root/'secret-reservation.json').write_text('{}')
        with patch.object(l, 'seed', return_value=({}, {})), patch.object(l, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'ONCE_ONLY'): l.configure()
            api.assert_not_called()

    def test_cleanup_requires_bound_terminal_run(self):
        (self.root/'dispatched.json').write_text(json.dumps({'sha': 'a'*40}))
        for run in [dict(head_sha='b'*40, status='completed', run_attempt=1),
                    dict(head_sha='a'*40, status='in_progress', run_attempt=1),
                    dict(head_sha='a'*40, status='completed', run_attempt=2)]:
            with patch.object(l, 'api', return_value=run), patch.object(l, 'gh') as gh:
                with self.assertRaisesRegex(ValueError, 'TERMINAL'): l.cleanup(123)
                gh.assert_not_called()

    def test_cleanup_only_own_named_secrets(self):
        (self.root/'dispatched.json').write_text(json.dumps({'sha': 'a'*40}))
        (self.root/'secret-reservation.json').write_text(json.dumps({'names': ['UNRELATED']}))
        (self.root/'secret-created-00.json').write_text(json.dumps({'name': 'UNRELATED'}))
        run = dict(head_sha='a'*40, status='completed', run_attempt=1)
        with patch.object(l, 'api', return_value=run), patch.object(l, 'gh') as gh:
            with self.assertRaisesRegex(ValueError, 'OWNERSHIP'): l.cleanup(123)
            gh.assert_not_called()


if __name__ == '__main__': unittest.main()
