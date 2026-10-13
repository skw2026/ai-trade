"""No-price causal capacity audit of the frozen 2024 full-population design.

This is not a return screen. Rules are copied from the pre-price proposal;
unknown boundaries are never zero and future cluster members cannot revise an
entry decision. The market-data contract remains a separate prerequisite.
"""
from collections import Counter
import json
import os
from pathlib import Path

import aave_cloud_local as local
x, a = local.x, local.a
START = 1704067200
MID = 1719792000
END = 1735689600
GAP = 3600
PROPOSAL = Path('.artifacts/aave-complete-input-20261012/economic-design-proposal.json')
PROPOSAL_SHA = 'd78abfc02b8e7b462bed7d345618df760c69dd061f426f4d7b9b1ca05ea33aaa'


def opportunities(blocks):
    """Accept a complete ordered population, not a positive-event subsample."""
    prior = None
    seen = set()
    clusters = []
    for block in blocks:
        timestamp = block['time']
        a.require(type(timestamp) is int and START <= timestamp < END,
                  'CAPACITY_BLOCK_TIME')
        a.require(prior is None or timestamp > prior, 'CAPACITY_BLOCK_ORDER')
        a.require(block['transactions'], 'CAPACITY_EMPTY_BLOCK')
        for transaction in block['transactions']:
            tx = transaction['hash']
            a.require(tx not in seen, 'CAPACITY_DUPLICATE_TRANSACTION')
            seen.add(tx)
            amount = transaction['amount']
            a.require(amount is None or type(amount) is int, 'CAPACITY_AMOUNT_TYPE')
            a.require((transaction['status'] == 'WETH_BOUNDARY_UNKNOWN' and amount is None)
                      or (transaction['status'] == 'WETH_BOUNDARY_VERIFIED' and type(amount) is int),
                      'CAPACITY_UNKNOWN_SEMANTICS')
        if prior is None or timestamp-prior > GAP:
            clusters.append([])
        clusters[-1].append(block)
        prior = timestamp
    result = []
    for cluster in clusters:
        first = cluster[0]['time']
        entry = 60*((first+60)//60+1)
        known = [tx for b in cluster if b['time']+60 < entry for tx in b['transactions']]
        a.require(known, 'CAPACITY_EMPTY_CAUSAL_BUNDLE')
        amount = None if any(tx['amount'] is None for tx in known) else sum(tx['amount'] for tx in known)
        exclusion = 'LEFT_YEAR_BOUNDARY' if first < START+GAP else 'RIGHT_YEAR_BOUNDARY' if entry+300 >= END else None
        result.append(dict(first=first, entry=entry, exit=entry+300,
            exclusion=exclusion, bundle_transactions=[tx['hash'] for tx in known],
            bundle_net_sale_wei=amount,
            candidate_opportunity=exclusion is None and amount is not None and amount > 0,
            signal='UNKNOWN' if amount is None else 'POSITIVE' if amount > 0 else 'NEGATIVE' if amount < 0 else 'ZERO',
            cluster_last=cluster[-1]['time'], cluster_blocks=len(cluster)))
    return result


def summarize(blocks, rows):
    eligible = [v for v in rows if v['exclusion'] is None]
    candidate = [v for v in rows if v['candidate_opportunity']]
    halves = Counter('H1' if v['first'] < MID else 'H2' for v in candidate)
    counts = {'H1': halves['H1'], 'H2': halves['H2']}
    return dict(raw_blocks=len(blocks), raw_transactions=sum(len(b['transactions']) for b in blocks),
        raw_event_days=len({b['time']//86400 for b in blocks}), clusters=len(rows),
        boundary_exclusions=dict(Counter(v['exclusion'] for v in rows if v['exclusion'])),
        eligible_all_event_opportunities=len(eligible), eligible_signals=dict(Counter(v['signal'] for v in eligible)),
        eligible_signals_by_half={half: dict(Counter(v['signal'] for v in eligible
            if (v['first'] < MID) == (half == 'H1'))) for half in ('H1', 'H2')},
        candidate_opportunities=len(candidate), candidate_half_years=counts,
        candidate_days=len({v['first']//86400 for v in candidate}),
        candidate_complete_weeks=len({(v['entry']-START)//604800 for v in candidate if v['entry'] < START+52*604800}),
        calendar_complete_weeks=52,
        clock_assumption='BLOCK_TIME_PLUS_60_SECONDS_THEN_STRICT_NEXT_MINUTE_NOT_MEASURED_RECEIPT_LATENCY',
        proposal_minimum_capacity=all(counts.values()),
        status='CAUSAL_CAPACITY_ONLY_NOT_ECONOMIC_ADMISSION',
        prices=0, economic_attempts=0, independent_confirmation='NOT_ADMITTED',
        profitability='NOT_EVALUATED', learning_gain='NOT_EVALUATED', g4='NOT_ACHIEVED')


def main():
    os.umask(0o077)
    a.require(a.now() < x.DEADLINE, 'CAPACITY_STAGE_DEADLINE')
    a.require(a.digest(PROPOSAL.read_bytes()) == PROPOSAL_SHA, 'CAPACITY_PROPOSAL_CHANGED')
    a.require(json.loads((x.ROOT/'validation-state.json').read_text())['status'] == 'RUNNING',
              'CAPACITY_RUNNING_GATE_REQUIRED')
    seed, _ = local.seed()
    restoration = json.loads((x.ROOT/'restoration.json').read_text())
    a.require(restoration['status'] == 'RESTORED_NATIVE_AND_WETH_BOUNDARIES_COMPLETE_NOT_ECONOMIC_ADMISSION',
              'CAPACITY_FULL_RESTORATION_REQUIRED')
    native = (x.ROOT/'restored-native.json').read_bytes()
    a.require(a.digest(native) == restoration['cipher_receipt']['plain_sha256'], 'CAPACITY_NATIVE_HASH')
    cache, _ = x.audit_bundle(json.loads(native), seed)
    flows_raw = (x.ROOT/'flows.json').read_bytes()
    flows = json.loads(flows_raw)
    groups, _ = x.native_plan(seed['events'])
    a.require(set(flows) == {tx for transactions in groups.values() for tx in transactions}, 'CAPACITY_FLOW_SET')
    blocks = []
    for number, transactions in sorted(groups.items(), key=lambda item: int(item[0], 16)):
        block = cache[x.key('eth_getBlockByNumber', [number, False])]
        members = []
        for tx in transactions:
            flow = flows[tx]
            a.require(flow['transaction_hash'] == tx and flow['block_hash'] == block['hash']
                      and flow['reserve_identity']['block_number'] == number
                      and flow['trade_action'] is None and flow['signal_admitted'] is False,
                      'CAPACITY_FLOW_IDENTITY')
            members.append(dict(hash=tx, status=flow['status'], amount=flow['weth_dex_net_sale_wei']))
        blocks.append(dict(number=number, time=int(block['timestamp'], 16), transactions=members))
    rows = opportunities(blocks)
    report = summarize(blocks, rows)
    report.update(source_sha256=a.digest(Path(__file__).read_bytes()),
        proposal_sha256=a.digest(PROPOSAL.read_bytes()), native_sha256=a.digest(native),
        flows_sha256=a.digest(flows_raw), protected_files=local.verify_protected())
    a.save(x.ROOT/'causal-capacity-private.json', dict(summary=report, opportunities=rows))
    a.save(x.ROOT/'causal-capacity-summary.json', report)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
