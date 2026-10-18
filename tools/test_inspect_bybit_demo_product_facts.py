import copy
import hashlib
import hmac
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import inspect_bybit_demo_product_facts as facts


def fixture():
    coin = {'walletBalance': '91827.3456', 'locked': '0', 'borrowAmount': '0',
            'spotBorrow': '0', 'totalOrderIM': '0', 'totalPositionIM': '0', 'bonus': '0',
            'marginCollateral': True, 'collateralSwitch': True}
    return {'account': {'marginMode': 'ISOLATED_MARGIN', 'unifiedMarginStatus': 6,
                        'uid': 'private-uid', 'unexpected': 'do-not-print'},
            'wallet': {'list': [{'accountType': 'UNIFIED', 'coin': [
                dict(coin, coin='BTC'), dict(coin, coin='USDT')]}]},
            'collateral': {'list': [{'currency': 'BTC', 'marginCollateral': True,
                                     'collateralSwitch': True, 'collateralRatio': '0.99'}]}}


class Fake:
    def __init__(self, data=None, error_at=None):
        self.data = data or fixture()
        self.calls = []
        self.error_at = error_at

    def get_fixed(self, item):
        self.calls.append(item)
        if item[0] == self.error_at:
            return facts.base.encode({'retCode': 10001, 'retMsg': 'private-sensitive-error', 'result': {}})
        return facts.base.encode({'retCode': 0, 'result': self.data[item[0]], 'time': facts.base.now_ms()})


class Tests(unittest.TestCase):
    def test_fixed_plan_no_order_or_fee_or_funds(self):
        self.assertEqual(len(facts.REQUESTS), 3)
        self.assertEqual([x[1] for x in facts.REQUESTS],
                         ['/v5/account/info', '/v5/account/wallet-balance', '/v5/account/collateral-info'])
        self.assertEqual(facts.REQUESTS[1][2], {'accountType': 'UNIFIED', 'coin': 'BTC,USDT'})

    def test_summary_amount_free_no_permission_claim(self):
        out = facts.summarize(fixture())
        rendered = json.dumps(out)
        for secret in ('91827.3456', 'private-uid', 'do-not-print', '0.99'):
            self.assertNotIn(secret, rendered)
        self.assertTrue(out['uta2_inverse_mode'])
        self.assertFalse(out['account_available_balance_applicable'])
        self.assertEqual(out['actual_fees'], 'UNKNOWN')
        self.assertEqual(out['trade_capacity'], 'UNKNOWN')
        self.assertFalse(out['economic_qualification'])

    def test_all_modes_no_upgrade(self):
        for uta in (3, 4, 5, 6):
            for mode in ('ISOLATED_MARGIN', 'REGULAR_MARGIN', 'PORTFOLIO_MARGIN'):
                data = fixture(); data['account'].update(unifiedMarginStatus=uta, marginMode=mode)
                out = facts.summarize(data)
                self.assertEqual(out['uta2_inverse_mode'], uta in (5, 6))
                self.assertFalse(out['account_mutation'])

    def test_classic_unified_wallet_conflict_stops_at_wallet(self):
        data = fixture(); data['account'].update(unifiedMarginStatus=1, marginMode='REGULAR_MARGIN')
        with self.assertRaisesRegex(ValueError, 'ACCOUNT_WALLET_MODE_MISMATCH'):
            facts.summarize(data)
        with tempfile.TemporaryDirectory() as parent:
            fake = Fake(data)
            with self.assertRaisesRegex(ValueError, 'ACCOUNT_WALLET_MODE_MISMATCH'):
                facts.collect(pathlib.Path(parent) / 'capture', fake)
            self.assertEqual(len(fake.calls), 2)

    def test_missing_coin_and_empty_fields_unknown_not_zero(self):
        data = fixture(); data['wallet']['list'][0]['coin'] = [{'coin': 'USDT', 'locked': ''}]
        data['collateral']['list'] = []
        out = facts.summarize(data)
        self.assertFalse(out['coins']['BTC']['present'])
        self.assertEqual(out['coins']['BTC']['fields']['walletBalance'], 'UNKNOWN')
        self.assertEqual(out['coins']['USDT']['fields']['locked'], 'UNKNOWN')
        self.assertEqual(out['btc_collateral']['collateralSwitch'], 'UNKNOWN')

    def test_negative_balance_and_nonzero_liabilities(self):
        data = fixture(); row = data['wallet']['list'][0]['coin'][0]
        row.update(walletBalance='-1', borrowAmount='2', locked='0')
        out = facts.summarize(data)['coins']['BTC']['fields']
        self.assertEqual(out['walletBalance'], 'NEGATIVE')
        self.assertEqual(out['borrowAmount'], 'POSITIVE')
        self.assertEqual(out['locked'], 'ZERO')

    def test_malformed_identity_bool_decimal_fail_closed(self):
        changes = [
            lambda d: d['account'].update(unifiedMarginStatus=True),
            lambda d: d['account'].update(marginMode='other'),
            lambda d: d['wallet']['list'][0].update(accountType='FUND'),
            lambda d: d['wallet']['list'][0]['coin'].append(d['wallet']['list'][0]['coin'][0]),
            lambda d: d['wallet']['list'][0]['coin'][0].update(coin='ETH'),
            lambda d: d['wallet']['list'][0]['coin'][0].update(walletBalance='NaN'),
            lambda d: d['wallet']['list'][0]['coin'][0].update(collateralSwitch='true'),
            lambda d: d['collateral']['list'][0].update(currency='ETH')]
        for change in changes:
            with self.subTest(change=change):
                data = fixture(); change(data)
                with self.assertRaises(ValueError): facts.summarize(data)

    def test_three_requests_archive_permissions_and_hashes(self):
        with tempfile.TemporaryDirectory() as parent:
            root = pathlib.Path(parent) / 'capture'; fake = Fake()
            out = facts.collect(root, fake)
            self.assertEqual(fake.calls, list(facts.REQUESTS))
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for path in root.iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            manifest = facts.base.decode((root / 'manifest.json').read_bytes())
            self.assertEqual(out['manifest_sha256'], facts.base.digest((root / 'manifest.json').read_bytes()))
            for item in manifest['requests']:
                self.assertEqual(item['sha256'], facts.base.digest((root / (item['name'] + '.raw')).read_bytes()))
            with self.assertRaises(FileExistsError): facts.collect(root, Fake())

    def test_api_failure_retained_and_stops_no_retry(self):
        with tempfile.TemporaryDirectory() as parent:
            root = pathlib.Path(parent) / 'capture'; fake = Fake(error_at='wallet')
            with self.assertRaisesRegex(ValueError, 'API_ERROR_10001'): facts.collect(root, fake)
            self.assertEqual(len(fake.calls), 2)
            self.assertTrue((root / 'wallet.raw').exists())
            self.assertFalse((root / 'summary.json').exists())

    def test_account_identity_failure_stops_after_first_get(self):
        data = fixture(); data['account']['unifiedMarginStatus'] = True
        with tempfile.TemporaryDirectory() as parent:
            fake = Fake(data)
            with self.assertRaisesRegex(ValueError, 'UTA_STATUS_INVALID'):
                facts.collect(pathlib.Path(parent) / 'capture', fake)
            self.assertEqual(len(fake.calls), 1)

    def test_wallet_identity_failure_stops_before_collateral(self):
        data = fixture(); data['wallet']['list'][0]['accountType'] = 'CONTRACT'
        with tempfile.TemporaryDirectory() as parent:
            fake = Fake(data)
            with self.assertRaisesRegex(ValueError, 'ACCOUNT_TYPE_INVALID'):
                facts.collect(pathlib.Path(parent) / 'capture', fake)
            self.assertEqual(len(fake.calls), 2)

    def test_transport_get_demo_signature_and_single_sequence(self):
        pair = ('synthetic-key', 'synthetic-secret')
        t = facts.Transport(pair)
        raw = facts.base.encode({'retCode': 0, 'result': {}})
        class Reply:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return raw
        calls = []
        def open_(request, timeout):
            calls.append(request)
            self.assertEqual(request.get_method(), 'GET')
            self.assertTrue(request.full_url.startswith('https://api-demo.bybit.com/v5/account/'))
            self.assertEqual(timeout, 20)
            return Reply()
        t.opener.open = open_
        with patch.object(facts.base, 'now_ms', return_value=123456):
            for item in facts.REQUESTS: self.assertEqual(t.get_fixed(item), raw)
        expected = hmac.new(pair[1].encode(), ('123456'+pair[0]+'5000').encode(), hashlib.sha256).hexdigest()
        self.assertEqual(calls[0].get_header('X-bapi-sign'), expected)
        with self.assertRaisesRegex(ValueError, 'REQUEST_ORDER_INVALID'): t.get_fixed(facts.REQUESTS[0])

    def test_transport_failure_consumes_attempt_and_rejects_redirect(self):
        t = facts.Transport(('synthetic-key', 'synthetic-secret'))
        def fail(*args, **kwargs): raise OSError('private message')
        t.opener.open = fail
        with self.assertRaisesRegex(ValueError, '^TRANSPORT_ERROR$'): t.get_fixed(facts.REQUESTS[0])
        with self.assertRaisesRegex(ValueError, 'REQUEST_ORDER_INVALID'): t.get_fixed(facts.REQUESTS[0])
        with self.assertRaisesRegex(ValueError, 'REDIRECT_FORBIDDEN'):
            facts.base.NoRedirect().redirect_request(None, None, None, None, None, 'http://bad')

    def test_request_deadline_is_armed_and_restored(self):
        before = facts.signal.getsignal(facts.signal.SIGALRM)
        with self.assertRaises(TimeoutError):
            with facts.request_deadline():
                remaining, interval = facts.signal.getitimer(facts.signal.ITIMER_REAL)
                self.assertTrue(0 < remaining <= 20 and interval == 0)
                facts.signal.getsignal(facts.signal.SIGALRM)(None, None)
        self.assertEqual(facts.signal.getitimer(facts.signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(facts.signal.getsignal(facts.signal.SIGALRM), before)

    def test_future_clock_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as parent:
            root = pathlib.Path(parent) / 'capture'
            root.symlink_to(parent)
            with self.assertRaisesRegex(ValueError, 'ARCHIVE_ROOT_SYMLINK'): facts.collect(root, Fake())
        class Future(Fake):
            def get_fixed(self, item):
                p = facts.base.decode(super().get_fixed(item)); p['time'] += 120_000
                return facts.base.encode(p)
        with tempfile.TemporaryDirectory() as parent:
            with self.assertRaisesRegex(ValueError, 'SERVER_CLOCK_MISMATCH'):
                facts.collect(pathlib.Path(parent) / 'capture', Future())


if __name__ == '__main__':
    unittest.main()
