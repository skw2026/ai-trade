#!/usr/bin/env python3
"""Three fixed Demo GETs; private raw archive, amount-free facts, no actuator."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import pathlib
import signal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

import audit_bybit_readonly_evidence as base

SCHEMA = 'bybit_demo_product_facts_v1'
REQUESTS = (
    ('account', '/v5/account/info', {}),
    ('wallet', '/v5/account/wallet-balance', {'accountType': 'UNIFIED', 'coin': 'BTC,USDT'}),
    ('collateral', '/v5/account/collateral-info', {'currency': 'BTC'}),
)


@contextmanager
def request_deadline():
    """POSIX main-thread wall-clock bound, including DNS and response reads."""
    base.require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), 'EXISTING_TIMER_REJECTED')
    def expired(_signum, _frame):
        raise TimeoutError('REQUEST_DEADLINE')
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, 20)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class Transport(base.Transport):
    def __init__(self, pair):
        super().__init__('demo', pair)
        self.calls = 0

    def get_fixed(self, item):
        base.require(self.calls < len(REQUESTS) and item == REQUESTS[self.calls], 'REQUEST_ORDER_INVALID')
        self.calls += 1  # consume before IO; no retry even on failure
        _, path, params = item
        query = urlencode(params)
        key, secret = self.pair
        timestamp = str(base.now_ms())
        signature = hmac.new(secret.encode(), (timestamp + key + '5000' + query).encode(), hashlib.sha256).hexdigest()
        headers = {'X-BAPI-API-KEY': key, 'X-BAPI-SIGN': signature,
                   'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': '5000'}
        url = 'https://api-demo.bybit.com' + path + ('?' + query if query else '')
        try:
            with request_deadline():
                with self.opener.open(Request(url, headers=headers, method='GET'), timeout=20) as response:
                    base.require(response.status == 200, 'HTTP_STATUS_INVALID')
                    raw = response.read(base.MAX_BYTES + 1)
        except HTTPError as exc:
            raise ValueError('HTTP_' + str(exc.code)) from None
        except (URLError, TimeoutError, OSError):
            raise ValueError('TRANSPORT_ERROR') from None
        base.require(0 < len(raw) <= base.MAX_BYTES, 'RESPONSE_SIZE_INVALID')
        base.require(not any(v.encode() in raw for v in self.pair), 'CREDENTIAL_ECHO_REJECTED')
        return raw


def sign(value):
    if value is None or value == '':
        return 'UNKNOWN'
    n = base.number(value)
    return 'POSITIVE' if n > 0 else 'NEGATIVE' if n < 0 else 'ZERO'


def flag(value):
    base.require(value is None or type(value) is bool, 'BOOLEAN_INVALID')
    return 'UNKNOWN' if value is None else value


def validate_page(name, data):
    """Reject a failed page before consuming the next authenticated request."""
    if name == 'account':
        base.require(data.get('marginMode') in ('ISOLATED_MARGIN', 'REGULAR_MARGIN', 'PORTFOLIO_MARGIN'), 'MARGIN_MODE_INVALID')
        value = data.get('unifiedMarginStatus')
        base.require(type(value) is int and value in (1, 3, 4, 5, 6), 'UTA_STATUS_INVALID')
    elif name == 'wallet':
        rows = data.get('list')
        base.require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict), 'WALLET_COUNT_INVALID')
        base.require(rows[0].get('accountType') == 'UNIFIED', 'ACCOUNT_TYPE_INVALID')
        coins = rows[0].get('coin')
        base.require(isinstance(coins, list) and all(isinstance(x, dict) for x in coins), 'COINS_INVALID')
        base.require(all(x.get('coin') in ('BTC', 'USDT') for x in coins), 'COIN_IDENTITY_INVALID')
        base.require(len({x['coin'] for x in coins}) == len(coins), 'DUPLICATE_COIN')
        for row in coins:
            for key in ('walletBalance', 'locked', 'borrowAmount', 'spotBorrow', 'totalOrderIM', 'totalPositionIM', 'bonus'):
                sign(row.get(key))
            flag(row.get('marginCollateral')); flag(row.get('collateralSwitch'))
    elif name == 'collateral':
        rows = data.get('list')
        base.require(isinstance(rows, list) and len(rows) <= 1, 'COLLATERAL_ROWS_INVALID')
        base.require(all(isinstance(x, dict) and x.get('currency') == 'BTC' for x in rows), 'COLLATERAL_IDENTITY_INVALID')
        for row in rows:
            flag(row.get('marginCollateral')); flag(row.get('collateralSwitch'))
    else:
        raise ValueError('PAGE_NAME_INVALID')


def summarize(data):
    for name, _, _ in REQUESTS:
        validate_page(name, data[name])
    account = data['account']
    mode = account.get('marginMode')
    uta = account.get('unifiedMarginStatus')
    base.require(uta != 1, 'ACCOUNT_WALLET_MODE_MISMATCH')
    base.require(mode in ('ISOLATED_MARGIN', 'REGULAR_MARGIN', 'PORTFOLIO_MARGIN'), 'MARGIN_MODE_INVALID')
    base.require(type(uta) is int and uta in (1, 3, 4, 5, 6), 'UTA_STATUS_INVALID')
    wallets = data['wallet'].get('list')
    base.require(isinstance(wallets, list) and len(wallets) == 1, 'WALLET_COUNT_INVALID')
    wallet = wallets[0]
    base.require(wallet.get('accountType') == 'UNIFIED', 'ACCOUNT_TYPE_INVALID')
    coins = wallet.get('coin')
    base.require(isinstance(coins, list) and all(isinstance(x, dict) for x in coins), 'COINS_INVALID')
    base.require(all(x.get('coin') in ('BTC', 'USDT') for x in coins), 'COIN_IDENTITY_INVALID')
    base.require(len({x['coin'] for x in coins}) == len(coins), 'DUPLICATE_COIN')
    coin_map = {x['coin']: x for x in coins}
    coin_facts = {}
    for name in ('BTC', 'USDT'):
        row = coin_map.get(name, {})
        coin_facts[name] = {'present': name in coin_map,
                           'fields': {key: sign(row.get(key)) for key in
                                      ('walletBalance', 'locked', 'borrowAmount', 'spotBorrow',
                                       'totalOrderIM', 'totalPositionIM', 'bonus')},
                           'marginCollateral': flag(row.get('marginCollateral')),
                           'collateralSwitch': flag(row.get('collateralSwitch'))}
    rows = data['collateral'].get('list')
    base.require(isinstance(rows, list) and len(rows) <= 1, 'COLLATERAL_ROWS_INVALID')
    base.require(all(isinstance(x, dict) and x.get('currency') == 'BTC' for x in rows), 'COLLATERAL_IDENTITY_INVALID')
    collateral = rows[0] if rows else {}
    return {'schema_version': SCHEMA, 'status': 'READONLY_FACTS_OBSERVED', 'source': 'demo',
            'unifiedMarginStatus': uta, 'marginMode': mode, 'uta2_inverse_mode': uta in (5, 6),
            'coins': coin_facts, 'btc_collateral': {
                'present': bool(rows), 'marginCollateral': flag(collateral.get('marginCollateral')),
                'collateralSwitch': flag(collateral.get('collateralSwitch'))},
            'account_available_balance_applicable': uta in (3, 4, 5, 6) and mode != 'ISOLATED_MARGIN',
            'actual_fees': 'UNKNOWN', 'order_permission': 'UNKNOWN', 'trade_capacity': 'UNKNOWN',
            'snapshot_is_atomic': False, 'economic_qualification': False,
            'order_submission': False, 'account_mutation': False, 'balances_published': False}


def collect(root, transport):
    root = pathlib.Path(root)
    base.require(not root.is_symlink(), 'ARCHIVE_ROOT_SYMLINK')
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    data, records = {}, []
    for name, path, params in REQUESTS:
        sent = base.now_ms()
        raw = transport.get_fixed((name, path, params))
        received = base.now_ms()
        base.safe_file(root / (name + '.raw'), raw)
        record = {'name': name, 'method': 'GET', 'path': path, 'params': params,
                  'domain': 'https://api-demo.bybit.com', 'sent_ms': sent,
                  'received_ms': received, 'sha256': base.digest(raw)}
        base.safe_file(root / (name + '-receipt.json'), base.encode(record))
        records.append(record)
        payload = base.decode(raw)
        data[name] = base.result(raw, allow_missing_time=name == 'account')
        validate_page(name, data[name])
        if name == 'wallet':
            base.require(data['account']['unifiedMarginStatus'] != 1, 'ACCOUNT_WALLET_MODE_MISMATCH')
        t = payload.get('time')
        base.require(t is None or sent - 60_000 <= t <= received + 60_000, 'SERVER_CLOCK_MISMATCH')
        record['server_time_missing'] = t is None
    report = summarize(data)
    manifest = {'schema_version': SCHEMA, 'requests': records,
                'collector_sha256': base.digest(pathlib.Path(__file__).read_bytes()),
                'base_sha256': base.digest(pathlib.Path(base.__file__).read_bytes())}
    encoded = base.encode(manifest)
    base.safe_file(root / 'manifest.json', encoded)
    report.update(manifest_sha256=base.digest(encoded), response_pages=len(records),
                  collector_sha256=manifest['collector_sha256'], base_sha256=manifest['base_sha256'],
                  account_response_time_missing=records[0]['server_time_missing'],
                  observed_ms=records[-1]['received_ms'])
    base.safe_file(root / 'summary.json', base.encode(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=pathlib.Path, required=True)
    parser.add_argument('--env-file', type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        pair = base.credentials(base.credential_env(args.env_file), True)
        report = collect(args.root, Transport(pair))
    except Exception as exc:
        report = {'schema_version': SCHEMA, 'status': 'INCOMPLETE', 'reason': base.error_code(exc),
                  'order_submission': False, 'account_mutation': False, 'balances_published': False}
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report['status'] == 'READONLY_FACTS_OBSERVED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
