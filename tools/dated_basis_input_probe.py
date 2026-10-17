"""Fixed public COIN-M input controls; no PnL, authentication or trading.

An HTTP receipt is not input admission. Current symbols never authenticate a
historical ruleset. No redirects, automatic retries or alternate hosts.
"""
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import subprocess

URLS = {
    'delivery': 'https://dapi.binance.com/futures/data/delivery-price?pair=BTCUSD',
    'exchange': 'https://dapi.binance.com/dapi/v1/exchangeInfo',
}
TARGETS = {
    'BTCUSD_' + date: int(dt.datetime.strptime('2023' + date[2:], '%Y%m%d')
        .replace(hour=8, tzinfo=dt.timezone.utc).timestamp() * 1000)
    for date in ('230331', '230630', '230929', '231229')
}
MAX_BYTES = 2097152


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def positive(value):
    require(type(value) in (str, int, Decimal), 'PRICE_TYPE')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('PRICE_PARSE') from exc
    require(number.is_finite() and number > 0, 'PRICE_POSITIVE_FINITE')
    return str(number)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def parse(kind, raw):
    require(kind in URLS, 'UNKNOWN_CONTROL')
    require(len(raw) <= MAX_BYTES, 'BODY_SIZE')
    obj = json.loads(raw, parse_float=Decimal, object_pairs_hook=unique_object)
    if isinstance(obj, dict) and 'code' in obj:
        require(type(obj['code']) is int and obj['code'] < 0, 'API_ERROR_SCHEMA')
        return dict(status='API_ERROR_NOT_INPUT', code=obj['code'])
    if kind == 'delivery':
        require(isinstance(obj, list), 'DELIVERY_LIST_REQUIRED')
        records = {}
        for row in obj:
            require(isinstance(row, dict), 'DELIVERY_ROW')
            timestamp = row.get('deliveryTime')
            require(type(timestamp) is int and 946684800000 <= timestamp < 4102444800000,
                    'DELIVERY_TIMESTAMP_MS')
            require(timestamp not in records, 'DUPLICATE_DELIVERY')
            records[timestamp] = positive(row.get('deliveryPrice'))
        matches = {symbol: records[t] for symbol, t in TARGETS.items() if t in records}
        return dict(status='DELIVERY_ONLY_NOT_ADMITTED', records=len(records),
                    earliest_ms=min(records) if records else None,
                    latest_ms=max(records) if records else None,
                    target_matches=matches,
                    missing_targets=sorted(set(TARGETS)-set(matches)))
    require(isinstance(obj, dict) and isinstance(obj.get('symbols'), list), 'EXCHANGE_SCHEMA')
    symbols = {}
    for row in obj['symbols']:
        require(isinstance(row, dict) and isinstance(row.get('symbol'), str), 'SYMBOL_SCHEMA')
        require(row['symbol'] not in symbols, 'DUPLICATE_SYMBOL')
        symbols[row['symbol']] = row
    matches = {name: symbols[name] for name in TARGETS if name in symbols}
    # Do not interpret maintMarginPercent/requiredMarginPercent: docs say ignore.
    return dict(status='CURRENT_METADATA_ONLY_NOT_ADMITTED', symbols=len(symbols),
                target_symbols_found=sorted(matches),
                historical_identity_proven=False, historical_margin_proven=False)


def fetch(kind, folder):
    require(kind in URLS, 'NONPUBLIC_OR_UNAPPROVED_ENDPOINT')
    os.umask(0o077)
    folder = Path(folder)
    folder.mkdir(mode=0o700)  # Existing attempt cannot be overwritten/retried.
    save(folder/'request.json', dict(method='GET', url=URLS[kind],
         started_utc=dt.datetime.now(dt.timezone.utc).isoformat()))
    command = ['curl', '--disable', '--silent', '--show-error', '--proto', '=https',
               '--connect-timeout', '10', '--max-time', '25', '--max-filesize', str(MAX_BYTES),
               '--output', str(folder/'body'), '--write-out', '%{http_code}', URLS[kind]]
    try:
        result = subprocess.run(command, capture_output=True, timeout=30)
        rc, stdout, stderr = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        rc, stdout, stderr = -1, exc.stdout or b'', exc.stderr or b''
    raw = (folder/'body').read_bytes() if (folder/'body').exists() else b''
    http = stdout.decode('ascii', errors='replace').strip()
    receipt = dict(returncode=rc, http=http, body_bytes=len(raw), body_sha256=digest(raw),
                   stderr_sha256=digest(stderr), finished_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    save(folder/'receipt.json', receipt)
    require(rc == 0, 'TRANSPORT_FAILURE_'+str(rc))
    require(len(raw) <= MAX_BYTES, 'BODY_SIZE')
    if http in ('403', '451'):
        return dict(status='ACCESS_DENIED_STOP_HOST', http=http)
    if http in ('400', '404'):
        return dict(status='ENDPOINT_OR_REQUEST_NOT_AVAILABLE', http=http)
    require(http == '200', 'UNEXPECTED_HTTP_'+http)
    return parse(kind, raw)
