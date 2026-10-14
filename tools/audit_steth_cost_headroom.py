"""Separate raw-ABI/rational audit; never calls the screen's decoder or model."""
from fractions import Fraction
import hashlib
import json
from pathlib import Path

ROOT = Path('.artifacts/cost-covered-mechanisms-20261014/steth-headroom')
CONTRACT = Path('docs/plans/2026-10-14-steth-cost-headroom.json')


def require(condition, label):
    if not condition:
        raise ValueError(label)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    def unique(pairs):
        obj = {}
        for k, v in pairs:
            require(k not in obj, 'AUDIT_DUPLICATE_KEY')
            obj[k] = v
        return obj
    return json.loads(path.read_text(), object_pairs_hook=unique)


def check_numbers(result, outputs, input_wei, basic, stress):
    gross = [Fraction((q-input_wei)*10000, input_wei) for q in outputs]
    expected = dict(samples=len(outputs), positive_gross_samples=sum(g > 0 for g in gross),
                    reference_cost_covered=sum(g > basic for g in gross),
                    stress_cost_covered=sum(g > stress for g in gross), economic_attempts=1,
                    qualified_candidates=0, g4=False, full_strategy_pnl=None, sharpe=None, maximum_drawdown=None)
    for k, v in expected.items():
        require(result[k] == v and type(result[k]) is type(v), 'AUDIT_VALUE_'+k)
    exact = dict(gross_min_bps=min(gross), gross_max_bps=max(gross),
                 maximum_reference_headroom_bps=max(gross)-basic,
                 maximum_stress_headroom_bps=max(gross)-stress)
    for k, v in exact.items():
        require(Fraction(result[k]) == v, 'AUDIT_EXACT_'+k)
    # Only the non-decision descriptive mean can be recurring in base10.
    require(abs(Fraction(result['gross_mean_bps'])-sum(gross)/len(gross)) <= Fraction(1, 10**60), 'AUDIT_MEAN')
    label = ('REJECT_SAMPLED_REFERENCE_COST_COVERAGE' if expected['reference_cost_covered'] == 0 else
             'INSUFFICIENT_STRESS_COST_HEADROOM' if expected['stress_cost_covered'] == 0 else
             'COST_HEADROOM_PRESENT_EXIT_MODEL_NOT_ADMITTED')
    require(result['decision'] == label, 'AUDIT_ADJUDICATION')


def execute():
    gate = read(ROOT.parent/'validation-state.json')
    require(gate['status'] == 'RUNNING', 'AUDIT_REQUIRES_GATE')
    c, freeze = read(CONTRACT), read(ROOT/'freeze.json')
    require(freeze['inputs'][str(CONTRACT)] == sha(CONTRACT), 'AUDIT_CONTRACT_BINDING')
    for name, expected in freeze['inputs'].items():
        require(sha(Path(name)) == expected, 'AUDIT_SOURCE_CHANGED')
    require({p.name for p in (ROOT/'operations').iterdir()} == {'chain'} | {str(i) for i in range(52)}, 'AUDIT_OPERATION_SET')
    outputs, timestamps, code_hashes = [], [], []
    lo, hi = c['first_block_inclusive'], c['last_block_exclusive']
    for name in ['chain']+[str(i) for i in range(52)]:
        folder = ROOT/'operations'/name
        record, request, response = (read(folder/f) for f in ('receipt.json', 'request.json', 'response.json'))
        require(record['request_sha256'] == sha(folder/'request.json') and
                record['response_sha256'] == sha(folder/'response.json'), 'AUDIT_WIRE_HASH')
        require(record['http'] == '200' and record['curl_returncode'] == 0 and record['retries'] == 0, 'AUDIT_TRANSPORT')
        size = 1 if name == 'chain' else 6
        require(isinstance(response, list) and len(response) == size, 'AUDIT_RESPONSE_SIZE')
        require(all(type(r.get('id')) is int and r.get('jsonrpc') == '2.0' and 'error' not in r and 'result' in r
                    for r in response), 'AUDIT_ENVELOPE')
        by_id = {r['id']: r['result'] for r in response}
        require(set(by_id) == set(range(1, size+1)), 'AUDIT_RESPONSE_IDS')
        if name == 'chain':
            require(request == [dict(jsonrpc='2.0', id=1, method='eth_chainId', params=[])] and by_id[1] == '0x1', 'AUDIT_CHAIN')
            continue
        block = lo+((2*int(name)+1)*(hi-lo))//104
        target = hex(block)
        expected_specs = [('eth_getBlockByNumber', [target, False]), ('eth_getCode', [c['pool'], target])]
        data = ('0xc6610657'+'0'*64, '0xc6610657'+'0'*63+'1', '0x313ce567',
                '0x5e0d443f'+'0'*64+'0'*63+'1'+format(10**18, '064x'))
        for i, calldata in enumerate(data):
            expected_specs.append(('eth_call', [dict(to=c['coin1'] if i == 2 else c['pool'], data=calldata), target]))
        require(request == [dict(jsonrpc='2.0', id=i, method=m, params=p) for i, (m, p) in enumerate(expected_specs, 1)], 'AUDIT_REQUEST_IDENTITY')
        require(by_id[1]['number'] == target, 'AUDIT_BLOCK_NUMBER')
        timestamp = int(by_id[1]['timestamp'], 16)
        require(1704067200 <= timestamp < 1735689600, 'AUDIT_YEAR')
        timestamps.append(timestamp)
        require(by_id[2].startswith('0x') and len(by_id[2]) > 2, 'AUDIT_BYTECODE')
        code_hashes.append(hashlib.sha256(bytes.fromhex(by_id[2][2:])).hexdigest())
        words = []
        for i in (3, 4, 5, 6):
            value = by_id[i]
            require(isinstance(value, str) and value.startswith('0x') and len(value) == 66, 'AUDIT_ABI_WORD')
            words.append(int.from_bytes(bytes.fromhex(value[2:]), 'big'))
        require(words[:3] == [int(c['coin0'], 16), int(c['coin1'], 16), 18] and 0 < words[3] < 2**128, 'AUDIT_COINS_DECIMALS_QUOTE')
        outputs.append(words[3])
    require(timestamps == sorted(set(timestamps)) and len(set(code_hashes)) == 1, 'AUDIT_GRID_CODE')
    check_numbers(read(ROOT/'result.json'), outputs, 10**18, Fraction(c['reference_fee_bps']), Fraction(c['stress_fee_bps']))
    report = dict(status='INDEPENDENT_RAW_ABI_AND_ALL_REPORTED_ARITHMETIC_AGREE',
                  inputs=52, rpc_methods=313, result_sha256=sha(ROOT/'result.json'),
                  audit_source_sha256=sha(Path(__file__)), profitability_established=False)
    with (ROOT/'independent-audit.json').open('x') as out:
        json.dump(report, out, indent=2, sort_keys=True)
        out.write('\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    execute()
