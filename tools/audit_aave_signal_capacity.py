"""Independent no-price reconstruction from indexed event times and bound flows."""
from bisect import bisect_left
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path('.artifacts/aave-cloud-input-20261013')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconstruct(events, flows):
    by_time = defaultdict(set)
    for event in events:
        by_time[int(event['timeStamp'], 16)].add(event['transactionHash'])
    times = sorted(by_time)
    starts = [i for i, t in enumerate(times) if i == 0 or t-times[i-1] > 3600]
    result = []
    for index in starts:
        first = times[index]
        delayed = first+60
        entry = delayed+(60-delayed % 60)
        cutoff_index = bisect_left(times, entry-60)
        transactions = set().union(*(by_time[t] for t in times[index:cutoff_index]))
        values = [flows[tx]['weth_dex_net_sale_wei'] for tx in transactions]
        amount = None if None in values else sum(values)
        excluded = 'LEFT_YEAR_BOUNDARY' if first < 1704070800 else 'RIGHT_YEAR_BOUNDARY' if entry+300 >= 1735689600 else None
        result.append(dict(first=first, entry=entry, exit=entry+300,
            bundle_transactions=sorted(transactions), bundle_net_sale_wei=amount,
            signal='UNKNOWN' if amount is None else 'POSITIVE' if amount > 0 else 'NEGATIVE' if amount < 0 else 'ZERO',
            exclusion=excluded, candidate_opportunity=excluded is None and amount is not None and amount > 0))
    return result


def main():
    require(json.loads((ROOT/'validation-state.json').read_text())['status'] == 'RUNNING', 'AUDIT_GATE_REQUIRED')
    seed = json.loads((ROOT/'seed.json').read_text())
    evidence = json.loads((ROOT/'causal-capacity-private.json').read_text())
    report = evidence['summary']
    require(sha(ROOT/'flows.json') == report['flows_sha256'], 'AUDIT_FLOW_HASH')
    require(sha(ROOT/'restored-native.json') == report['native_sha256'], 'AUDIT_NATIVE_HASH')
    require(sha(Path('tools/aave_signal_capacity.py')) == report['source_sha256'], 'AUDIT_SOURCE_HASH')
    require(hashlib.sha256(json.dumps(seed['events'], sort_keys=True).encode()).hexdigest()
            == 'a5c58aadbafdc2e760bb80f963e838a594729622ed150d3edf6b154e25a30ba7', 'AUDIT_FIXED_EVENTS')
    flows = json.loads((ROOT/'flows.json').read_text())
    reconstructed = reconstruct(seed['events'], flows)
    require(len(reconstructed) == len(evidence['opportunities']) == report['clusters'], 'AUDIT_CLUSTER_COUNT')
    for independent, primary in zip(reconstructed, evidence['opportunities']):
        for key, expected in independent.items():
            actual = sorted(primary[key]) if key == 'bundle_transactions' else primary[key]
            require(actual == expected, 'AUDIT_CAUSAL_DECISION_MISMATCH')
    candidate = [v for v in reconstructed if v['candidate_opportunity']]
    halves = Counter('H1' if dt.datetime.fromtimestamp(v['first'], dt.timezone.utc).month <= 6 else 'H2' for v in candidate)
    days = {dt.datetime.fromtimestamp(v['first'], dt.timezone.utc).date().isoformat() for v in candidate}
    weeks = {dt.datetime.fromtimestamp(v['entry'], dt.timezone.utc).isocalendar()[:2] for v in candidate
             if dt.datetime.fromtimestamp(v['entry'], dt.timezone.utc).isocalendar().year == 2024}
    require(len(candidate) == report['candidate_opportunities'] and dict(halves) == report['candidate_half_years']
            and len(days) == report['candidate_days'] and len(weeks) == report['candidate_complete_weeks'],
            'AUDIT_CAPACITY_COUNTS')
    output = dict(status='INDEPENDENT_CAUSAL_CAPACITY_AUDIT_PASS', compared_opportunities=len(reconstructed),
        candidate_opportunities=len(candidate), candidate_half_years=dict(halves), candidate_days=len(days),
        candidate_complete_weeks=len(weeks), evidence_sha256=sha(ROOT/'causal-capacity-private.json'),
        source_sha256=sha(Path(__file__)), prices=0, economic_attempts=0)
    with (ROOT/'causal-capacity-audit.json').open('x') as handle:
        json.dump(output, handle, sort_keys=True, indent=2)
        handle.write('\n')
    print(json.dumps(output, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
