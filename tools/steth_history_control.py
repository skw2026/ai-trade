"""Four fixed metadata reads for the new stETH mechanism; no market/PnL or signing."""
import json
import os
from pathlib import Path
import re
import subprocess

import cost_covered_intake as base
from aave_authenticated_rpc import parse_config

FOLDER = base.ROOT/'steth-control'
QUEUE = '0x889edc2edab5f40e902b864ad4d7ade8e412f9b1'
BLOCK = hex(20000000)
REQUESTS = [
    {'jsonrpc': '2.0', 'id': 1, 'method': 'eth_chainId', 'params': []},
    {'jsonrpc': '2.0', 'id': 2, 'method': 'eth_getBlockByNumber', 'params': [BLOCK, False]},
    {'jsonrpc': '2.0', 'id': 3, 'method': 'eth_call', 'params': [{'to': QUEUE, 'data': '0x19c2b4c3'}, BLOCK]},
    {'jsonrpc': '2.0', 'id': 4, 'method': 'eth_call', 'params': [{'to': QUEUE, 'data': '0x4f069a13'}, BLOCK]},
]


def validate(value):
    base.require(isinstance(value, list) and len(value) == 4, 'FOUR_RESULTS_REQUIRED')
    by_id = {}
    for row in value:
        base.require(isinstance(row, dict) and row.get('jsonrpc') == '2.0' and
                     type(row.get('id')) is int and row['id'] in (1, 2, 3, 4) and
                     row['id'] not in by_id and 'error' not in row and 'result' in row, 'RPC_ENVELOPE')
        by_id[row['id']] = row['result']
    base.require(by_id[1] == '0x1', 'ETHEREUM_MAINNET_REQUIRED')
    block = by_id[2]
    base.require(isinstance(block, dict) and block.get('number') == BLOCK and
                 re.fullmatch('0x[0-9a-f]{64}', block.get('hash', '')) is not None, 'BLOCK_IDENTITY')
    base.require(re.fullmatch('0x[0-9a-f]+', block.get('timestamp', '')) is not None, 'BLOCK_TIME')
    base.require(1704067200 <= int(block['timestamp'], 16) < 1735689600, 'BLOCK_NOT_IN_2024')
    base.require(all(isinstance(by_id[i], str) and re.fullmatch('0x[0-9a-f]{64}', by_id[i])
                     for i in (3, 4)), 'UINT256_RESULTS')
    last, finalized = (int(by_id[i], 16) for i in (3, 4))
    base.require(0 < finalized <= last, 'QUEUE_ORDER')
    return dict(status='FIXED_HISTORICAL_STATE_ACCESS_ONLY', block=20000000,
                block_hash=block['hash'], block_timestamp=int(block['timestamp'], 16),
                last_request_id=last, last_finalized_request_id=finalized,
                economic_attempts=0, whole_history_admitted=False)


def execute():
    os.umask(0o077)
    c = base.guard()
    base.preserve()
    base.require(not FOLDER.exists(), 'ONE_CONTROL_BATCH_ONLY')
    config = Path('.env.local')
    base.require(config.is_file() and not config.is_symlink() and config.stat().st_size <= 65536,
                 'LOCAL_CONFIG_REQUIRED')
    url, key = parse_config(config.read_text())
    FOLDER.mkdir(mode=0o700)
    base.save(FOLDER/'request.json', REQUESTS)
    record = dict(status='FAILED', methods=4, attempted=False, started_utc=base.stamp(),
                  source_sha256=base.sha(__file__), request_sha256=base.sha(FOLDER/'request.json'),
                  credential_scope='LOCAL_EXISTING_READ_ONLY_NO_TRANSFER', retries=0)
    try:
        argv = ['curl', '--disable', '--silent', '--proto', '=https', '--connect-timeout', '15',
                '--max-time', '30', '--max-filesize', str(c['maximum_response_bytes']), '--retry', '0',
                '--header', 'Content-Type: application/json', '--data-binary', '@'+str(FOLDER/'request.json'),
                '--write-out', '\n%{http_code}', '--config', '-']
        record['attempted'] = True
        done = subprocess.run(argv, input=('url = "'+url+'"\n').encode(), capture_output=True, timeout=35)
        raw, sep, http = done.stdout.rpartition(b'\n')
        record.update(curl_returncode=done.returncode,
                      http=http.decode() if re.fullmatch(rb'[0-9]{3}', http) else 'UNKNOWN',
                      response_bytes=len(raw), response_sha256=base.digest(raw))
        base.require(sep and len(raw) <= c['maximum_response_bytes'], 'RESPONSE_FRAMING')
        base.require(key.encode() not in raw, 'KEY_ECHO')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = None
        base.require(key not in json.dumps(value, ensure_ascii=False), 'ENCODED_KEY_ECHO')
        with (FOLDER/'response.json').open('xb') as out:
            out.write(raw)
        base.require(done.returncode == 0 and http == b'200', 'RPC_TRANSPORT_OR_HTTP')
        result = validate(value)
        base.save(FOLDER/'result.json', result)
        record['status'] = 'VALID_CONTROL_ONLY'
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        record['error_type'] = type(exc).__name__
        reason = str(exc)
        record['reason'] = reason if re.fullmatch('[A-Z][A-Z0-9_]{0,95}', reason) else 'UNEXPECTED_ERROR'
        raise RuntimeError(record['reason']) from None
    finally:
        record['finished_utc'] = base.stamp()
        base.save(FOLDER/'receipt.json', record)
        print(json.dumps(record, sort_keys=True), flush=True)


if __name__ == '__main__':
    execute()
