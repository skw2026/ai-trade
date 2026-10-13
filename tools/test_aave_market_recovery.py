import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import aave_market_recovery as r
import run_aave_recovered_economics as runner


def config():
    return json.loads(r.CONTRACT.read_text())


def request():
    return dict(kind='trade', start=60000, end=179999, limit=1000, url='https://api.bybit.com/synthetic')


def wire(code=0, headers=None, http=200, data=None):
    obj = dict(retCode=code, time=10000000, result=dict(category='linear', symbol='ETHUSDT', list=[
        [str(t), '100', '101', '99', '100', '1', '100'] for t in (120000, 60000)])) if data is None else data
    head = 'HTTP/2 %d\r\nContent-Type: application/json\r\n' % http
    head += ''.join(k+': '+v+'\r\n' for k, v in (headers or {}).items())
    return (head+'\r\n'+json.dumps(obj)+'\n%03d' % http).encode()


def quotas(remaining='0', reset='10001000'):
    return {'X-Bapi-Limit': '10', 'X-Bapi-Limit-Status': remaining, 'X-Bapi-Limit-Reset-Timestamp': reset}


class ProtocolTests(unittest.TestCase):
    def interpret(self, raw, attempt=1, rc=0, received=10000050):
        return r.interpret(raw, rc, request(), config(), attempt, received)

    def test_success_no_quota_headers_is_valid_not_quota_proof(self):
        result = self.interpret(wire())
        self.assertEqual(result['status'], 'VALID')
        self.assertEqual(result['rows'], 2)
        self.assertEqual(result['wait_seconds'], 0)

    def test_rfc_token_punctuation_in_header_names_is_legal(self):
        for name in ('ret_code', "!#$%&'*+.^_`|~0123Az-"):
            self.assertEqual(self.interpret(wire(headers={name: '0'}))['status'], 'VALID')
        for name in ('bad name', 'bad/name', 'bad(name)', 'bad@name', 'bad\\name'):
            with self.assertRaisesRegex(ValueError, 'HEADER_FIELD'):
                self.interpret(wire(headers={name: '0'}))

    def test_complete_header_body_identity_with_interim_response(self):
        raw = b'HTTP/1.1 100 Continue\r\n\r\n'+wire(headers=quotas('8'))
        status, headers, head, body = r.unpack(raw, config())
        self.assertEqual(status, 200)
        self.assertEqual(headers['x-bapi-limit-status'], '8')
        self.assertEqual(head+body+b'\n200', raw)
        self.assertEqual(self.interpret(raw)['header_sha256'], r.digest(head))

    def test_server_errors_have_fixed_finite_delays(self):
        for attempt, expected in ((1, 2), (2, 5)):
            value = self.interpret(wire(10016), attempt)
            self.assertEqual(value['status'], 'RECOVERABLE_NOT_VALID')
            self.assertEqual(value['wait_seconds'], expected)
        with self.assertRaisesRegex(ValueError, 'PAGE_RECOVERY_EXHAUSTED'):
            self.interpret(wire(10016), 3)

    def test_rate_limit_needs_complete_future_reset(self):
        result = self.interpret(wire(10006, quotas()))
        self.assertEqual(result['wait_seconds'], 2)
        self.assertEqual(result['status'], 'RECOVERABLE_NOT_VALID')
        with self.assertRaisesRegex(ValueError, 'WITHOUT_RESET'): self.interpret(wire(10006))

    def test_success_exhausted_quota_waits_before_next_page(self):
        result = self.interpret(wire(0, quotas()))
        self.assertEqual(result['status'], 'VALID')
        self.assertEqual(result['wait_seconds'], 2)

    def test_rate_limit_bad_remaining_or_stale_reset_stops(self):
        for h in (quotas('1'), quotas('-1'), quotas('11'), quotas(reset='10000000'),
                  quotas(reset='10070000'), {'X-Bapi-Limit': '10'}, quotas(reset='nan')):
            with self.subTest(headers=h), self.assertRaises(ValueError): self.interpret(wire(10006, h))
        with self.assertRaisesRegex(ValueError, 'NOT_FRESH'):
            self.interpret(wire(10006, quotas()), received=11000000)

    def test_duplicate_critical_header_is_not_accepted(self):
        raw = wire(10006, quotas()).replace(b'Content-Type:', b'x-bapi-limit: 10\r\nContent-Type:')
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_QUOTA'): self.interpret(raw)

    def test_unknown_errors_are_not_recovery_cases(self):
        cases = [(wire(10001), 0), (wire(http=403), 0), (wire(http=429), 0), (wire(http=503), 0), (wire(), 28)]
        for raw, rc in cases:
            with self.subTest(rc=rc), self.assertRaises(ValueError): self.interpret(raw, rc=rc)

    def test_data_corruption_never_becomes_transient(self):
        obj = json.loads(r.unpack(wire(), config())[3])
        obj['result']['list'].pop()
        with self.assertRaisesRegex(ValueError, 'MINUTE_GRID'): self.interpret(wire(data=obj))
        obj['retCode'] = True
        with self.assertRaisesRegex(ValueError, 'CODE_TYPE'): self.interpret(wire(data=obj))

    def test_header_and_body_limits_and_status_identity(self):
        for raw in (b'junk', wire()+b'x', wire().replace(b'\n200', b'\n201'),
                    wire(headers={'Long': 'a'*config()['maximum_header_bytes']})):
            with self.assertRaises(ValueError): self.interpret(raw)
        c = config(); c['maximum_body_bytes'] = 10
        with self.assertRaises(ValueError): r.unpack(wire(), c)


class BudgetTests(unittest.TestCase):
    def test_same_original_total_and_active_budget(self):
        c = config()
        self.assertEqual(c['active_seconds']+c['prior_active_seconds_charged'], 28800)
        self.assertEqual(c['existing_attempts']+c['maximum_new_attempts'], 1438)
        self.assertLessEqual(1438, 1500)

    def test_each_page_and_total_reservations_bounded(self):
        b = r.Budget(config())
        for _ in range(3): b.reserve(0)
        with self.assertRaisesRegex(ValueError, 'PAGE_ATTEMPT'): b.reserve(0)
        for i in range(647): b.reserve(i+1)
        with self.assertRaisesRegex(ValueError, 'TOTAL_ATTEMPT'): b.reserve(9999)
        self.assertEqual(b.attempts, 650)

    def test_error_and_wait_budget(self):
        b = r.Budget(config())
        for _ in range(14): b.account(dict(status='RECOVERABLE_NOT_VALID', wait_seconds=2), 10)
        with self.assertRaisesRegex(ValueError, 'KNOWN_ERROR'): b.account(dict(status='RECOVERABLE_NOT_VALID', wait_seconds=2), 10)
        b = r.Budget(config())
        for _ in range(2): b.account(dict(status='VALID', wait_seconds=60), 10)
        with self.assertRaisesRegex(ValueError, 'WAIT_BUDGET'): b.account(dict(status='VALID', wait_seconds=1), 10)

    def test_byte_reservation_before_request(self):
        b = r.Budget(config()); b.bytes = config()['maximum_new_raw_bytes']
        with self.assertRaisesRegex(ValueError, 'RAW_BUDGET'): b.reserve(0)
        self.assertEqual(b.attempts, 0)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'attempts').mkdir()
        self.frozen = dict(missing=[0, 1], requests=[request(), request()])
        (self.root/'freeze.json').write_text(json.dumps(self.frozen))
        for target, value in [('ROOT', self.root), ('guard', lambda: config()), ('preserve', lambda: 1), ('wait_until', lambda _: None)]:
            patcher = patch.object(r, target, value); patcher.start(); self.addCleanup(patcher.stop)

    def collect(self, responses):
        replies = [subprocess.CompletedProcess([], rc, raw, b'') for rc, raw in responses]
        with patch.object(r.subprocess, 'run', side_effect=replies) as run, redirect_stdout(io.StringIO()):
            r.collect()
        return run

    def test_known_server_failure_then_complete_manifest_and_ledger(self):
        run = self.collect([(0, wire(10016)), (0, wire()), (0, wire())])
        self.assertEqual(run.call_count, 3)
        manifest = json.loads((self.root/'collection.json').read_text())
        self.assertEqual(manifest['known_errors'], 1)
        self.assertEqual(manifest['new_attempts'], 3)
        self.assertEqual(len(r.verify_new_ledger(config(), self.frozen, manifest)), 2)
        self.assertEqual(r.old.ROOT, Path('.artifacts/aave-economic-screen-20261013'))
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--retry')+1], '0')
        self.assertNotIn('--insecure', argv)
        self.assertNotIn('--location', argv)
        with self.assertRaisesRegex(ValueError, 'ALREADY_STARTED'): r.collect()

    def test_wire_tamper_detected_even_for_retained_failed_attempt(self):
        self.collect([(0, wire(10016)), (0, wire()), (0, wire())])
        p = self.root/'attempts/0000/response.bin'; p.write_bytes(p.read_bytes()+b' ')
        manifest = json.loads((self.root/'collection.json').read_text())
        with self.assertRaisesRegex(ValueError, 'WIRE_CHANGED'): r.verify_new_ledger(config(), self.frozen, manifest)

    def test_rate_limit_without_headers_stops_and_retains_response(self):
        with self.assertRaisesRegex(ValueError, 'WITHOUT_RESET'):
            self.collect([(0, wire(10006))])
        self.assertFalse((self.root/'collection.json').exists())
        rec = json.loads((self.root/'attempts/0000/record.json').read_text())
        self.assertEqual(rec['status'], 'FAILED')
        self.assertEqual(rec['received_bytes'], len(wire(10006)))

    def test_transport_timeout_does_not_retry(self):
        with patch.object(r.subprocess, 'run', side_effect=subprocess.TimeoutExpired('curl', 35)) as run:
            with self.assertRaisesRegex(ValueError, 'TRANSPORT'): r.collect()
        self.assertEqual(run.call_count, 1)
        self.assertTrue((self.root/'attempts/0000/record.json').exists())

    def test_retry_budget_exhaustion_does_not_fetch_second_page(self):
        with self.assertRaisesRegex(ValueError, 'PAGE_RECOVERY_EXHAUSTED'):
            self.collect([(0, wire(10016))]*3)
        self.assertEqual(len(list((self.root/'attempts').iterdir())), 3)
        self.assertFalse((self.root/'collection.json').exists())

    def test_manifest_cannot_omit_an_error_record(self):
        self.collect([(0, wire(10016)), (0, wire()), (0, wire())])
        manifest = json.loads((self.root/'collection.json').read_text())
        manifest['attempt_records'].pop(str(self.root/'attempts/0000/record.json'))
        with self.assertRaisesRegex(ValueError, 'LEDGER_COVERAGE'): r.verify_new_ledger(config(), self.frozen, manifest)

    def test_rate_reset_recovery_and_success_quota_are_replayed(self):
        with patch.object(r.time, 'time', return_value=10000.05):
            self.collect([(0, wire(10006, quotas())), (0, wire(0, quotas())), (0, wire())])
        manifest = json.loads((self.root/'collection.json').read_text())
        self.assertEqual(manifest['known_errors'], 1)
        self.assertEqual(manifest['recovery_wait_seconds'], 4)
        self.assertEqual(len(r.verify_new_ledger(config(), self.frozen, manifest)), 2)

    def test_one_reviewed_first_wire_repair_keeps_original_failure(self):
        raw = wire(headers={'ret_code': '0'})
        with patch.object(r, 'interpret', side_effect=ValueError('HEADER_FIELD')):
            with self.assertRaisesRegex(ValueError, 'HEADER_FIELD'): self.collect([(0, raw)])
        folder = self.root/'attempts/0000'
        before = (folder/'record.json').read_bytes()
        correction = dict(first_record_sha256=r.sha(folder/'record.json'), first_wire_sha256=r.digest(raw))
        (self.root/'header-correction.json').write_text(json.dumps(correction))
        with patch.object(r, 'approved_correction', return_value=correction):
            run = self.collect([(0, wire())])
            self.assertEqual(run.call_count, 1)
            self.assertEqual((folder/'record.json').read_bytes(), before)
            manifest = json.loads((self.root/'collection.json').read_text())
            self.assertEqual(manifest['new_attempts'], 2)
            self.assertEqual(len(r.verify_new_ledger(config(), self.frozen, manifest)), 2)
            with self.assertRaisesRegex(ValueError, 'ALREADY_USED'): r.collect()


class OrchestrationTests(unittest.TestCase):
    def test_old_attempt_prevents_new_economic_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); previous = root/'old'; previous.mkdir()
            (previous/'economic-attempt.json').write_text('{}')
            (root/'market-audit.json').write_text(json.dumps(dict(status='FULL_FIXED_MARKET_ADMITTED', collection_sha256='fixed')))
            with patch.object(r, 'ROOT', root), patch.object(r.old, 'ROOT', previous), \
                 patch.object(r, 'guard'), patch.object(r, 'preserve'), patch.object(r, 'sha', return_value='fixed'), \
                 patch.object(r, 'market') as market:
                with self.assertRaisesRegex(ValueError, 'ONE_TOTAL_ECONOMIC'): runner.execute()
                market.assert_not_called()
            self.assertFalse((root/'economic-attempt.json').exists())

    def test_one_synthetic_attempt_and_independent_negative_adjudication(self):
        c = json.loads(r.old.CONTRACT.read_text())
        events, data = [], dict(trade={}, mark={}, funding={})
        for i in range(492):
            first = c['evaluation_start_ms']//1000+7201+i*7200
            entry = (first+60)//60*60+60
            end = entry+300
            events.append(dict(first=first, entry=entry, exit=end, exclusion=None,
                candidate_opportunity=i < 275, signal='POSITIVE' if i < 275 else 'UNKNOWN'))
            anchor = (first*1000-1)//60000*60000
            for t in list(range(anchor-61*60000, anchor, 60000))+[entry*1000, end*1000]:
                data['trade'][t] = ['100']*4
            for t in range((entry-60)*1000, end*1000+1, 60000): data['mark'][t] = ['100']*4
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); previous = root/'old'; previous.mkdir()
            (root/'freeze.json').write_text('{}')
            (root/'collection.json').write_text('{}')
            (root/'market-audit.json').write_text(json.dumps(dict(status='FULL_FIXED_MARKET_ADMITTED', collection_sha256=r.sha(root/'collection.json'))))
            with patch.object(r, 'ROOT', root), patch.object(r.old, 'ROOT', previous), \
                 patch.object(r, 'guard'), patch.object(r, 'preserve', return_value=1), \
                 patch.object(r, 'market', return_value=(data, events, {})), redirect_stdout(io.StringIO()):
                runner.execute()
                runner.verify()
                with self.assertRaisesRegex(ValueError, 'ONE_TOTAL_ECONOMIC'): runner.execute()
            result = json.loads((root/'result.json').read_text())
            report = json.loads((root/'economic-audit.json').read_text())
            self.assertEqual(result['assessment']['decision'], 'REJECT_REFERENCE_RISK')
            self.assertEqual(report['decision'], result['assessment']['decision'])
            self.assertEqual(report['g4'], 'NOT_ACHIEVED')
            self.assertFalse((previous/'economic-attempt.json').exists())


if __name__ == '__main__': unittest.main()
