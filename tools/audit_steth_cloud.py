"""Independent ABI and rational audit of the recovered 52-point input grid."""
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re

import audit_steth_cost_headroom as a

ROOT = Path('.artifacts/steth-cloud-input-20261014')
CONTRACT = Path('docs/plans/2026-10-14-steth-cost-headroom.json')


def raw_outputs(records, c):
    a.require(len(records) == 52 and [r['index'] for r in records] == list(range(52)), 'AUDIT_FIXED_SET')
    outputs, times, codes, decoded = [], [], [], []
    for i, record in enumerate(records):
        block = c['first_block_inclusive'] + (2*i+1)*(c['last_block_exclusive']-c['first_block_inclusive'])//104
        target = hex(block)
        specs = [('eth_getBlockByNumber', [target, False]), ('eth_getCode', [c['pool'], target])]
        data = ['0xc6610657'+'0'*64, '0xc6610657'+'0'*63+'1', '0x313ce567',
                '0x5e0d443f'+'0'*64+'0'*63+'1'+format(10**18, '064x')]
        for pos, calldata in enumerate(data):
            specs.append(('eth_call', [dict(to=c['coin1'] if pos == 2 else c['pool'], data=calldata), target]))
        a.require(record['request'] == [dict(jsonrpc='2.0', id=k, method=m, params=p)
                                      for k, (m, p) in enumerate(specs, 1)], 'AUDIT_REQUEST_BINDING')
        raw = record['response'].encode()
        a.require(hashlib.sha256(raw).hexdigest() == record['response_sha256']
                  and len(raw) == record['response_bytes'], 'AUDIT_RAW_HASH')
        response = json.loads(raw)
        a.require(isinstance(response, list) and len(response) == 6 and all(type(r.get('id')) is int
                  and r.get('jsonrpc') == '2.0' and 'result' in r and 'error' not in r for r in response), 'AUDIT_ENVELOPE')
        values = {r['id']: r['result'] for r in response}
        a.require(set(values) == set(range(1, 7)), 'AUDIT_IDS')
        header = values[1]
        a.require(header['number'] == target and re.fullmatch('0x[0-9a-f]{64}', header['hash']), 'AUDIT_BLOCK')
        timestamp = int(header['timestamp'], 16)
        a.require(1704067200 <= timestamp < 1735689600, 'AUDIT_YEAR')
        a.require(re.fullmatch('0x(?:[0-9a-f]{2})+', values[2]), 'AUDIT_CODE')
        code = hashlib.sha256(bytes.fromhex(values[2][2:])).hexdigest()
        words = []
        for k in (3, 4, 5, 6):
            a.require(re.fullmatch('0x[0-9a-f]{64}', values[k]), 'AUDIT_WORD')
            words.append(int.from_bytes(bytes.fromhex(values[k][2:]), 'big'))
        a.require(words[:3] == [int(c['coin0'], 16), int(c['coin1'], 16), 18]
                  and 0 < words[3] < 2**128, 'AUDIT_COINS_OR_QUOTE')
        times.append(timestamp); codes.append(code); outputs.append(words[3])
        decoded.append(dict(block=block, block_hash=header['hash'], timestamp=timestamp,
                            code_sha256=code, output_steth_wei=words[3]))
    a.require(times == sorted(set(times)) and len(set(codes)) == 1, 'AUDIT_GRID_OR_CODE')
    return outputs, decoded


def execute():
    a.require(a.read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'AUDIT_RUNNING_GATE')
    seed = a.read(ROOT/'seed.json')
    for path, digest in seed['sources'].items():
        a.require(a.sha(Path(path)) == digest, 'AUDIT_FROZEN_SOURCE')
    protected = a.read(ROOT/'protected.json')
    for path, digest in protected.items():
        a.require(a.sha(Path(path)) == digest, 'AUDIT_OLD_EVIDENCE')
    inputs = a.read(ROOT/'inputs.json')
    attempt = a.read(ROOT/'economic-attempt.json')
    a.require(attempt['attempt'] == 1 and attempt['inputs_sha256'] == a.sha(ROOT/'inputs.json')
              and attempt['contract_sha256'] == a.sha(CONTRACT), 'AUDIT_ATTEMPT_BINDING')
    a.require(inputs['cloud_bundle'] == a.sha(ROOT/'restored.json'), 'AUDIT_BUNDLE_BINDING')
    bundle = a.read(ROOT/'restored.json')
    cloud = {r['index']: r for r in bundle['records']}
    for index, record in enumerate(inputs['records']):
        if index < 8:
            folder = Path('.artifacts/cost-covered-mechanisms-20261014/steth-headroom/operations')/str(index)
            original = a.read(folder/'receipt.json')
            a.require(record['source'] == str(folder) and record['source_receipt_sha256'] == a.sha(folder/'receipt.json')
                      and record['response_sha256'] == a.sha(folder/'response.json') == original['response_sha256']
                      and record['request'] == a.read(folder/'request.json'), 'AUDIT_ORIGINAL_PROVENANCE')
        else:
            a.require(all(record[k] == v for k, v in cloud[index].items()), 'AUDIT_CLOUD_PROVENANCE')
    c = a.read(CONTRACT)
    outputs, decoded = raw_outputs(inputs['records'], c)
    a.require(decoded == inputs['rows'], 'AUDIT_DECODED_BINDING')
    a.check_numbers(a.read(ROOT/'result.json'), outputs, 10**18, Fraction(c['reference_fee_bps']), Fraction(c['stress_fee_bps']))
    report = dict(status='INDEPENDENT_RAW_ABI_AND_RATIONAL_AUDIT_AGREE', samples=52,
        protected_files=len(protected), result_sha256=a.sha(ROOT/'result.json'),
        profitability_established=False, g4=False)
    with (ROOT/'audit.json').open('x') as out:
        json.dump(report, out, sort_keys=True, indent=2)
        out.write('\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    execute()
