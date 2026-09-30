"""Read-only independent receipt/selection audit, not another learning attempt."""
from collections import deque
import csv
from decimal import Decimal as D
import json
import math
from pathlib import Path
import statistics

from audit_execution_net_learning import Ledger, close
from run_bounded_learning import need, sha, save

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'.artifacts/offline-policy-correction-20260930'


def read_domain(path, start, end):
    """Independent causal features and the frozen native safety bucket formula."""
    history = deque(maxlen=288)
    previous_price = None
    ewma = absolute = 0.
    bucket = 1
    count = 0
    output = []
    alpha = 1-.8**60
    with Path(path).open() as source:
        for r in csv.DictReader(source):
            ts = int(r['timestamp'])
            if ts < start-288*300000:
                continue
            need(ts < end, 'AUDIT_DOMAIN_END')
            if ts >= start:
                need(len(history) == 288, 'AUDIT_CONTEXT')
                x = tuple(max(-.01,min(.01,(float(history[-1]['price'])/float(history[-h]['open'])-1)/h))
                          for h in (12,288))
                output.append(dict(row=r,features=x,bucket=bucket))
            price = float(r['price'])
            count += 1
            if previous_price is not None:
                ret = (price-previous_price)/previous_price
                ewma = (1-alpha)*ewma+alpha*ret
                absolute = (1-alpha)*absolute+alpha*abs(ret)
                bucket = 1 if count < 20 else (
                    2 if abs(ret) >= .003 or absolute >= .0018 else 0 if abs(ewma) >= .0008 else 1)
            previous_price = price
            history.append(r)
            if ts+300000 == end:
                break
    need(len(output) == (end-start)//300000, 'AUDIT_DOMAIN_COVERAGE')
    return output


def audit_action(x, vector, old):
    if x is None:
        return D(0)
    mu = x[0]*vector[0]+x[1]*vector[1]
    scores = {d:d*mu-.00065*(abs(d-old)+abs(d)) for d in (-1,0,1)}
    order = list(dict.fromkeys([old,0,-1,1]))
    return D(2500*max(order,key=scores.get))


def sign(value):
    return 1 if value > D('1e-8') else -1 if value < D('-1e-8') else 0


def audit_trace(path, domain, summary):
    need(sha(path) == summary['trace_sha256'], 'TRACE_CHANGED')
    books = [Ledger(False),Ledger(True)]
    reason = 'COMPLETE'
    stop_at = exit_at = None
    pending = latched = False
    active = count = 0
    nets = [D(0)]*4
    streak = [0]*4
    exposure = [0]*3
    weekly = []
    week_start = [D(10000)]*2
    with Path(path).open() as source:
        events = (json.loads(line) for line in source)
        for i,sample in enumerate(domain,1):
            row = sample['row']
            ts = int(row['timestamp'])
            mark = D(row['mark_close'])
            if pending:
                event = next(events)
                need(event['kind'] == 'EXIT_OPEN' and event['timestamp'] == ts, 'EXIT_TIME')
                for b in books:
                    b.fund(D(row['mark_open']),D(row['funding_rate_per_interval']))
                    b.trade(D(0),D(row['open']),D(row['mark_open']))
                    b.last = D(0)
                    b.liquid = b.cash
                pending = False
                latched = True
                exit_at = ts
                if any(b.dd >= D('.08') for b in books):
                    reason = 'REFERENCE_RISK_STOP'
                need(event['reason'] == reason, 'EXIT_REASON')
                for b,s in zip(books,event['wallets']):
                    b.check(s,mark)
            elif not latched:
                event = next(events)
                need(event['kind'] == 'BAR' and event['timestamp'] == ts, 'ACTIVE_TIME')
                target = audit_action(sample['features'],summary['coefficients'],sign(books[0].last))
                close(event['target'],target,'causal_action')
                for b in books:
                    b.fund(D(row['mark_open']),D(row['funding_rate_per_interval']))
                opening = any(b.dd >= D('.08') for b in books)
                need(opening == event['opening_risk'], 'OPENING_RISK')
                receipts = [b.step(row,target,funded=True,halt=opening) for b in books]
                for observed,expected in zip(event['receipts'],receipts):
                    close(observed,expected,'net_receipt')
                active += 1
                assessment = None
                if any(b.dd >= D('.08') for b in books):
                    reason = 'REFERENCE_RISK_STOP'
                    stop_at = ts+300000
                    pending = True
                else:
                    count += 1
                    nets[sample['bucket']] += receipts[0]
                    nets[3] += receipts[0]
                    if count == 240:
                        for k in range(4):
                            streak[k] = streak[k]+1 if nets[k] < 0 else 0
                        withdrawal = any(n >= 2 for n in streak)
                        assessment = dict(net_by_decision_bucket_and_total=nets[:],
                                          streak=streak[:],withdrawn=withdrawal)
                        count = 0
                        nets = [D(0)]*4
                        if withdrawal:
                            reason = 'SAFETY_WITHDRAWAL'
                            stop_at = ts+300000
                            pending = True
                if assessment is None:
                    need(event['safety'] is None, 'UNEXPECTED_SAFETY')
                else:
                    need(event['safety']['streak'] == assessment['streak'] and
                         event['safety']['withdrawn'] == assessment['withdrawn'], 'SAFETY_LATCH')
                    for actual,expected in zip(event['safety']['net_by_decision_bucket_and_total'],
                                               assessment['net_by_decision_bucket_and_total']):
                        close(actual,expected,'safety_net')
                need(event['reason'] == reason, 'STOP_REASON')
                for b,s in zip(books,event['wallets']):
                    b.check(s,mark)
            exposure[sign(books[0].q)+1] += 1
            if i % 2016 == 0:
                values = [b.liquid for b in books]
                weekly.append([v-w for v,w in zip(values,week_start)])
                week_start = values
        terminal = next(events)
        need(terminal['kind'] == 'TERMINAL' and next(events,None) is None, 'TERMINAL_OR_EXTRA_EVENTS')
        expected_summary = {k:v for k,v in summary.items() if k not in ('trace_file','trace_sha256')}
        need(terminal['result'] == expected_summary, 'SUMMARY_CHANGED')
    row = domain[-1]['row']
    mark = D(row['mark_close'])
    if not latched:
        for b in books:
            b.trade(D(0),D(row['price']),mark)
            b.last = D(0)
            b.liquid = b.cash
        if any(b.dd >= D('.08') for b in books):
            if stop_at is None:
                stop_at = int(row['timestamp'])+300000
            reason = 'REFERENCE_RISK_STOP'
        exit_at = int(row['timestamp'])+300000
    if len(domain) % 2016 == 0:
        for k,b in enumerate(books):
            weekly[-1][k] += b.cash-week_start[k]
        week_start = [b.cash for b in books]
    need(summary['domain_bars'] == len(domain) and summary['active_bars'] == active, 'COVERAGE_COUNTS')
    need(summary['reason'] == reason and summary['stop_at_ms'] == stop_at and
         summary['exit_at_ms'] == exit_at and summary['terminal_flat'], 'TERMINAL_STATE')
    need(summary['exposure_short_flat_long'] == exposure, 'EXPOSURE')
    need(len(summary['weekly']) == len(weekly), 'WEEK_COUNT')
    for pair,expected in zip(summary['weekly'],weekly):
        for actual,value in zip(pair,expected):
            close(actual,value,'weekly')
    for k,b in enumerate(books):
        b.check(summary['wallets'][k],mark)
        close(summary['partial_week'][k],b.cash-week_start[k],'partial_week')
        close(sum(w[k] for w in summary['weekly'])+summary['partial_week'][k],b.cash-10000,'pnl_sum')
        need(b.q == 0, 'OPEN_TERMINAL_POSITION')
    close(summary['objective'],min(b.cash for b in books)-10000,'objective')
    return dict(active_bars=active, domain_bars=len(domain), reason=reason,
                economics=[dict(net=float(b.cash-10000),gross=float(b.cash-10000+b.fees+b.slip+b.funding),
                                fees=float(b.fees),slippage=float(b.slip),funding=float(b.funding),fills=b.fills)
                           for b in books])


def audit_selection(model):
    grid = (-4,-2,-1,0,1,2,4)
    results = {tuple(t['coefficients']):t['result'] for t in model['trials']}
    need(all(t['coefficients'] == t['result']['coefficients'] for t in model['trials']), 'TRIAL_MODEL_BINDING')
    need(len(results) == len(model['trials']) == model['evaluations'] <= 25, 'SEARCH_COUNT')
    seen = []
    def value(v):
        if v not in seen:
            seen.append(v)
        need(v in results, 'MISSING_TRIAL')
        return results[v]['objective']
    vector = (1,0)
    value(vector)
    steps = []
    for sweep in range(2):
        for axis in range(2):
            old = vector
            best = vector
            for v in grid:
                test = list(old)
                test[axis] = v
                test = tuple(test)
                if value(test) > value(best):
                    best = test
            vector = best
            steps.append(dict(sweep=sweep,axis=axis,before=list(old),after=list(best),objective=value(best)))
    need(seen == [tuple(t['coefficients']) for t in model['trials']], 'SEARCH_ORDER_OR_EXTRA_TRIAL')
    need(model['steps'] == steps and tuple(model['coefficients']) == vector and
         model['winner'] == results[vector] and model['changed'] == (vector != (1,0)), 'TRAIN_ONLY_SELECTION')


def lower_bound(values):
    if len(values) < 26:
        return None
    n = len(values)
    mean = statistics.mean(values)
    residual = [v-mean for v in values]
    variance = sum(v*v for v in residual)/n
    for lag in range(1,5):
        variance += 2*(1-lag/5)*sum(residual[i]*residual[i-lag] for i in range(lag,n))/n
    return mean-1.6448536269514722*math.sqrt(max(variance,0)/n)


def audit():
    c = json.loads((ROOT/'docs/plans/2026-09-30-offline-policy-correction.contract.json').read_text())
    frozen = json.loads((RUN/'execution-freeze.json').read_text())
    result = json.loads((RUN/'result.json').read_text())
    model = json.loads((RUN/'frozen-model.json').read_text())
    started = json.loads((RUN/'evaluation-start.json').read_text())
    need(sha(ROOT/'docs/plans/2026-09-30-offline-policy-correction.contract.json') == frozen['contract_sha256']
         and sha(ROOT/'docs/plans/2026-09-30-offline-policy-correction.md') == frozen['plan_sha256'], 'CONTRACT_IDENTITY')
    need(sha(ROOT/'.artifacts/execution-net-learning-20260930/build/bounded_learning_driver') == frozen['native_sha256'], 'NATIVE_IDENTITY')
    need(sha(ROOT/c['input_path']) == frozen['input_sha256'], 'INPUT_IDENTITY')
    need(sha(RUN/'execution-freeze.json') == result['execution_freeze_sha256'], 'FREEZE_IDENTITY')
    need(all(sha(ROOT/p) == h for p,h in frozen['sources'].items()), 'SOURCE_IDENTITY')
    need(sha(RUN/'frozen-model.json') == result['model_sha256'] == started['frozen_model_sha256'], 'MODEL_IDENTITY')
    need(model['frozen_at'] <= started['started'] <= result['completed'], 'FREEZE_ORDER')
    need(result['coefficients'] == model['coefficients'] == result['learned']['coefficients'] and
         result['fixed']['coefficients'] == [1,0], 'EVALUATION_MODEL_BINDING')
    need(model['training_end_exclusive_ms'] == started['start_ms'] == c['evaluation_start_ms'] and
         started['end_ms'] == c['end_ms'] and model['scope'] == 'DEVELOPMENT_TRAIN_ONLY', 'DOMAIN_BINDING')
    need(result['market_attempts'] == 1 and result['training_vectors'] == model['evaluations'] and
         result['model_changed'] == model['changed'], 'BUDGET_AND_CHANGE')
    need(result['confirmation_runs'] == 0 and result['candidate_status'] == 'NO_QUALIFIED_CANDIDATE', 'NO_AUTHORITY')
    train = read_domain(ROOT/c['input_path'],c['train_start_ms'],c['evaluation_start_ms'])
    trials = [audit_trace(RUN/t['result']['trace_file'],train,t['result']) for t in model['trials']]
    audit_selection(model)
    evaluation = read_domain(ROOT/c['input_path'],c['evaluation_start_ms'],c['end_ms'])
    fixed = audit_trace(RUN/result['fixed']['trace_file'],evaluation,result['fixed'])
    learned = audit_trace(RUN/result['learned']['trace_file'],evaluation,result['learned'])
    weeks = result['learned']['weekly']
    lowers = dict(stress=lower_bound([w[1] for w in weeks]),
                  paired_base=lower_bound([a[0]-b[0] for a,b in zip(weeks,result['fixed']['weekly'])]),
                  paired_stress=lower_bound([a[1]-b[1] for a,b in zip(weeks,result['fixed']['weekly'])]))
    need(result['hac_lower'] == lowers, 'STATISTICS')
    if model['winner']['reason'] != 'COMPLETE' or learned['reason'] != 'COMPLETE':
        decision = 'NO_GO_SAFETY_OR_REFERENCE_RISK'
    elif not model['changed'] or min(e['net'] for e in learned['economics']) <= 0:
        decision = 'NO_GO_NO_ABSOLUTE_LEARNING_EDGE'
    elif not all(v is not None and v > 0 for v in lowers.values()):
        decision = 'INSUFFICIENT_DEVELOPMENT_EVIDENCE'
    else:
        decision = 'DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE'
    need(decision == result['verdict'], 'VERDICT')
    return dict(schema='offline_policy_independent_audit_v1',status='PASS_EXISTING_RECEIPTS_ONLY',
                training_paths=len(trials),decimal_books=2*(len(trials)+2),
                training=trials,fixed=fixed,learned=learned,verdict=decision,
                confirmation_runs=0,qualification=False,result_sha256=sha(RUN/'result.json'))


if __name__ == '__main__':
    report = audit()
    destination = RUN/'independent-audit.json'
    if destination.exists():
        need(json.loads(destination.read_text()) == report, 'AUDIT_CHANGED')
    else:
        save(destination,report)
    print(json.dumps(report,allow_nan=False))
