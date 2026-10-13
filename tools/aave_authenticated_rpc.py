"""Credential-safe, read-only Alchemy transport. No retries or redirects.

The key is read locally, never passed in argv, persisted, or printed. These
helpers do not grant research admission or change any preceding failure gate.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import time

import aave_complete_input as a
import aave_complete_catalog as c
import aave_reserve_identity as q
import aave_complete_receipts as r

PUBLIC_ENDPOINT = 'https://eth-mainnet.g.alchemy.com/v2/[REDACTED]'
URL_PATTERN = r'https://eth-mainnet\.g\.alchemy\.com/v2/([A-Za-z0-9_-]{16,256})'
CONNECT_TIMEOUT_SECONDS = 15


def parse_config(text):
    values = []
    for line in text.splitlines():
        match = re.fullmatch(r'\s*(?:export\s+)?ALCHEMY_RPC_URL\s*=\s*(.*?)\s*', line)
        if match:
            value = match[1]
            if len(value) >= 2 and value[0] in ('\"', "'") and value[-1] == value[0]:
                value = value[1:-1]
            values.append(value)
    a.require(len(values) == 1, 'EXACTLY_ONE_RPC_CONFIG_REQUIRED')
    match = re.fullmatch(URL_PATTERN, values[0])
    a.require(match is not None, 'RPC_HOST_OR_KEY_FORMAT')
    a.require(match[1].lower() not in ('your_api_key_here', 'replace_with_key_here'), 'PLACEHOLDER_KEY')
    return values[0], match[1]


def validate_request(method, params):
    a.require(isinstance(params, list), 'RPC_PARAMS_LIST')
    if method == 'eth_chainId':
        a.require(params == [], 'CHAIN_PARAMS')
    elif method == 'eth_getTransactionReceipt':
        a.require(len(params) == 1 and a.hx(params[0], 64), 'RECEIPT_PARAMS')
    elif method == 'eth_getBlockByNumber':
        a.require(len(params) == 2 and a.hx(params[0]) and int(params[0], 16) > 0
                  and params[1] is False, 'HISTORICAL_BLOCK_ONLY')
    elif method == 'eth_call':
        a.require(len(params) == 2 and a.hx(params[1]) and int(params[1], 16) > 0
                  and params == q.request(params[1]), 'HISTORICAL_RESERVE_ONLY')
    else:
        raise ValueError('READ_ONLY_METHOD_ALLOWLIST')


def cached(folder, method, params, validator):
    if not folder.exists():
        return None
    record = json.loads((folder/'record.json').read_text())
    request = (folder/'request.json').read_bytes()
    raw = (folder/'response.json').read_bytes()
    a.require(record['status'] == 'VALID' and record['endpoint'] == PUBLIC_ENDPOINT,
              'AUTH_CACHE_NOT_VALID')
    a.require(a.digest(request) == record['request_sha256'] and
              json.loads(request) == r.request_identity(method, params) and
              a.digest(raw) == record['response_sha256'], 'AUTH_CACHE_CHANGED')
    value = r.envelope(json.loads(raw))
    validator(value)
    return value


class Client:
    def __init__(self, config=Path('.env.local'), *, config_text=None):
        if config_text is None:
            a.require(config.is_file() and not config.is_symlink() and
                      config.stat().st_size <= 65536, 'LOCAL_CREDENTIAL_FILE')
            config_text = config.read_text()
        self._url, self._key = parse_config(config_text)
        self._last_send = 0.0

    def __repr__(self):
        return 'AlchemyReadOnlyClient([REDACTED])'

    def fetch(self, folder, method, params, validator, before_send=lambda: None):
        validate_request(method, params)
        a.require(not folder.exists(), 'ONCE_ONLY_NO_IMPLICIT_RETRY')
        folder.mkdir(mode=0o700)
        a.save(folder/'request.json', r.request_identity(method, params))
        record = {'endpoint': PUBLIC_ENDPOINT, 'method': method, 'status': 'FAILED',
                  'started_utc': a.now().isoformat(), 'attempted_rpc': False,
                  'request_sha256': a.digest((folder/'request.json').read_bytes()),
                  'source_sha256': a.digest(Path(__file__).read_bytes()), 'retries': 0}
        try:
            time.sleep(max(0.0, 1.0-(time.monotonic()-self._last_send)))
            before_send()
            record['request_sent_utc'] = a.now().isoformat()
            # --disable must be first: ignore user curlrc. URL never in argv.
            argv = ['curl', '--disable', '--silent', '--proto', '=https',
                    '--connect-timeout', str(CONNECT_TIMEOUT_SECONDS), '--max-time', '30', '--max-filesize',
                    str(a.MAX_BODY), '--retry', '0', '--header', 'Content-Type: application/json',
                    '--data-binary', '@'+str(folder/'request.json'),
                    '--write-out', '\n%{http_code}', '--config', '-']
            self._last_send = time.monotonic()
            record['attempted_rpc'] = True
            result = subprocess.run(argv, input=('url = "'+self._url+'"\n').encode(),
                                    capture_output=True, timeout=35)
            record['curl_returncode'] = result.returncode
            body, separator, status = result.stdout.rpartition(b'\n')
            record['http_status'] = status.decode() if re.fullmatch(rb'[0-9]{3}', status) else 'UNKNOWN'
            a.require(separator and len(body) <= a.MAX_BODY, 'RESPONSE_SIZE_OR_FRAMING')
            record.update(response_sha256=a.digest(body), response_bytes=len(body))
            # Do not persist key echoes, including JSON unicode escaping. Error
            # messages and curl stderr are untrusted and never printed/stored.
            a.require(self._key.encode() not in body, 'CREDENTIAL_ECHO_SUPPRESSED')
            try:
                decoded = json.loads(body)
            except (ValueError, UnicodeError):
                decoded = None
            a.require(self._key not in json.dumps(decoded, ensure_ascii=False),
                      'ENCODED_CREDENTIAL_ECHO_SUPPRESSED')
            with (folder/'response.json').open('xb') as handle:
                handle.write(body)
            a.require(result.returncode == 0 and status == b'200', 'RPC_TRANSPORT_OR_HTTP')
            value = r.envelope(decoded)
            validator(value)
            record['status'] = 'VALID'
            return value
        except Exception as exc:
            # Even unexpected exception messages may contain a URL. Persist
            # only the type and allowlisted constant reasons, never repr(exc).
            message = str(exc)
            record['error_type'] = type(exc).__name__
            record['reason'] = message if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', message) else 'UNEXPECTED_ERROR'
            raise RuntimeError(record['reason']) from None
        finally:
            record['ended_utc'] = a.now().isoformat()
            a.save(folder/'record.json', record)
            print(json.dumps(record, sort_keys=True), flush=True)
