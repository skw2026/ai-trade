"""Read-only attribution of the frozen failed delay path, never another replay."""
from datetime import datetime, timezone
from decimal import Decimal as D
import json

from sparse_reversal_2024_inputs import OUT, START, END, guard, need, read, save, sha
from audit_offline_policy_correction import read_domain, audit_action, sign


def utc(ts):
    return datetime.fromtimestamp(ts/1000, timezone.utc).isoformat()


def main():
    guard()
    original = read(OUT/'result.json')
    need(original['decision']['status'] == 'REJECT_DELAY_RISK', 'FIXED_RESULT_REQUIRED')
    for p, h in original['evidence'].items(): need(sha(p) == h, 'ORIGINAL_EVIDENCE_CHANGED')
    domain = read_domain(OUT/'replay.csv', START, END)
    with (OUT/'delay.jsonl').open() as f: events = [json.loads(line) for line in f]
    table = []; actual = D(0); queued = D(0); first = None; transitions = 0; previous = 0
    for e in events:
        if e['kind'] != 'BAR': continue
        ts = e['timestamp']; sample = domain[(ts-START)//300000]
        x = sample['features']; decision = audit_action(x, [0,-4], sign(actual))
        need(float(decision) == e['decision_target'] and float(queued) == e['target'], 'QUEUE_BINDING')
        if decision and first is None: first = ts
        if first is not None and len(table) < 12:
            table.append(dict(utc=utc(ts), actual_previous_target=float(actual),
                previously_queued_target=float(queued), new_decision=float(decision),
                mu=-4*x[1], price_open=sample['row']['open'],
                fills=e['wallets'][0]['fills'], position_after=e['wallets'][0]['qty']))
        if first is not None and e['target'] != previous: transitions += 1
        previous = e['target']; actual = D(str(e['wallets'][0]['last_target']))
        queued = decision
    groups = read(OUT/'delay-groups.json')
    normal = original['paths']['normal']; delay = original['paths']['delay']
    # An algebraic counterexample, no market data or economic ledger:
    # strong first signal, then constant positive edge below the entry hurdle.
    actual = 0; queued = D(0); algebra = []
    for mu in (.002,.0005,.0005,.0005,.0005,.0005):
        new = audit_action((0,-mu/4), [0,-4], actual)
        applied = queued; queued = new; actual = sign(applied)
        algebra.append(dict(mu=mu, decision=float(new), executed_target=float(applied)))
    need([r['executed_target'] for r in algebra] == [0,2500,0,2500,0,2500], 'OSCILLATION_COUNTEREXAMPLE')
    need(audit_action((0,-.0005/4), [0,-4], 0) == 0 and
         audit_action((0,-.0005/4), [0,-4], 1) == 2500, 'HYSTERESIS_FACT')
    net = delay['economics'][1]['net']; fees = delay['economics'][1]['fees']
    slippage = delay['economics'][1]['slippage']; funding = delay['economics'][1]['funding']
    result = dict(status='DELAY_FEEDBACK_OSCILLATION_CONFIRMED', result_sha256=sha(OUT/'result.json'),
        first_decision_utc=utc(first), stop_utc=utc(delay['stop_at_ms']),
        normal_legacy_rejection_utc=utc(normal['first_legacy_withdrawal_ms']),
        control_stop_utc=utc(original['paths']['control']['stop_at_ms']),
        first_twelve_decisions=table, target_transitions=transitions,
        one_bar_episodes=sum(e['bars'] == 1 for e in groups['episodes']),
        total_episodes=groups['episode_count'], stress_cost_fraction_of_loss=(fees+slippage+funding)/abs(net),
        algebraic_counterexample=algebra, market_replays=0, new_market_requests=0,
        root_cause='State-dependent hold/flat decisions use pre-execution state while stale targets remain queued.',
        limitation='This is delayed-feedback fragility; it does not isolate a pure five-minute price slippage effect.',
        proposed_semantics_not_executed='Separate strategy intent, pending target and actual position before any new experiment.')
    save(OUT/'delay-diagnosis.json', result)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__': main()
