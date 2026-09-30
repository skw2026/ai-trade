"""Independent Decimal reconstruction of already consumed development paths."""
from decimal import Decimal as D
import json
from pathlib import Path

from audit_execution_net_learning import Ledger, close
from audit_offline_policy_correction import read_domain, audit_action, sign, audit_selection, lower_bound
from run_bounded_learning import need, sha, save

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'.artifacts/offline-diagnostic-safety-20260930'
MODE = 'DEVELOPMENT_DIAGNOSTIC_ONLY'


def audit_trace(path, domain, summary):
    need(sha(path) == summary['trace_sha256'], 'TRACE_CHANGED')
    books = [Ledger(False), Ledger(True)]
    reason = 'COMPLETE'
    stop_at = exit_at = first_diagnostic = None
    pending = latched = False
    active = count = triggered = 0
    nets, streak, exposure = [D(0)]*4, [0]*4, [0]*3
    weekly, week_start = [], [D(10000)]*2
    with Path(path).open() as source:
        events = (json.loads(line) for line in source)
        for i, sample in enumerate(domain,1):
            row = sample['row']
            ts, mark = int(row['timestamp']), D(row['mark_close'])
            if pending:
                event = next(events)
                need(event['kind'] == 'EXIT_OPEN' and event['timestamp'] == ts, 'EXIT_TIME')
                for b in books:
                    b.fund(D(row['mark_open']),D(row['funding_rate_per_interval']))
                    b.trade(D(0),D(row['open']),D(row['mark_open']))
                    b.last, b.liquid = D(0), b.cash
                pending, latched, exit_at = False, True, ts
                need(event['reason'] == reason, 'EXIT_REASON')
                need(len(event['wallets']) == 2, 'BOOK_COUNT')
                for b,s in zip(books,event['wallets']): b.check(s,mark)
            elif not latched:
                event = next(events)
                need(event['kind'] == 'BAR' and event['timestamp'] == ts, 'ACTIVE_TIME')
                target = audit_action(sample['features'],summary['coefficients'],sign(books[0].last))
                close(event['target'],target,'causal_action')
                for b in books: b.fund(D(row['mark_open']),D(row['funding_rate_per_interval']))
                opening = any(b.dd >= D('.08') for b in books)
                need(opening == event['opening_risk'], 'OPENING_RISK')
                receipts = [b.step(row,target,funded=True,halt=opening) for b in books]
                need(len(event['receipts']) == len(event['wallets']) == 2, 'BOOK_COUNT')
                for actual,expected in zip(event['receipts'],receipts): close(actual,expected,'net_receipt')
                active += 1
                assessment = None
                if any(b.dd >= D('.08') for b in books):
                    reason, stop_at, pending = 'REFERENCE_RISK_STOP', ts+300000, True
                else:
                    count += 1
                    nets[sample['bucket']] += receipts[0]
                    nets[3] += receipts[0]
                    if count == 240:
                        streak = [n+1 if p < 0 else 0 for n,p in zip(streak,nets)]
                        trigger = any(n >= 2 for n in streak)
                        triggered += int(trigger)
                        if trigger and first_diagnostic is None: first_diagnostic = ts+300000
                        assessment = dict(mode=MODE,streak=streak[:],would_withdraw=trigger,withdrawn=False)
                        need(event['safety'] is not None, 'MISSING_DIAGNOSTIC')
                        need(len(event['safety']['net_by_decision_bucket_and_total']) == 4, 'DIAGNOSTIC_WIDTH')
                        for actual,expected in zip(event['safety']['net_by_decision_bucket_and_total'],nets):
                            close(actual,expected,'diagnostic_net')
                        nets, count = [D(0)]*4, 0
                if assessment is None:
                    need(event['safety'] is None, 'UNEXPECTED_DIAGNOSTIC')
                else:
                    need(all(event['safety'][k] == v for k,v in assessment.items()), 'DIAGNOSTIC_STATE')
                need(event['reason'] == reason, 'STOP_REASON')
                for b,s in zip(books,event['wallets']): b.check(s,mark)
            exposure[sign(books[0].q)+1] += 1
            if i % 2016 == 0:
                values = [b.liquid for b in books]
                weekly.append([v-w for v,w in zip(values,week_start)])
                week_start = values
        terminal = next(events)
        need(terminal['kind'] == 'TERMINAL' and next(events,None) is None, 'TERMINAL_OR_EXTRA_EVENTS')
        need(terminal['result'] == {k:v for k,v in summary.items() if k not in ('trace_file','trace_sha256')},
             'SUMMARY_CHANGED')
    row, mark = domain[-1]['row'], D(domain[-1]['row']['mark_close'])
    if not latched:
        for b in books:
            b.trade(D(0),D(row['price']),mark)
            b.last, b.liquid = D(0), b.cash
        if any(b.dd >= D('.08') for b in books):
            if stop_at is None: stop_at = int(row['timestamp'])+300000
            reason = 'REFERENCE_RISK_STOP'
        exit_at = int(row['timestamp'])+300000
    if len(domain) % 2016 == 0:
        for k,b in enumerate(books): weekly[-1][k] += b.cash-week_start[k]
        week_start = [b.cash for b in books]
    legacy = ('REJECT_LEGACY_SAFETY' if first_diagnostic is not None else
              'REJECT_REFERENCE_RISK' if reason != 'COMPLETE' else
              'PATH_CONSTRAINTS_ONLY_NOT_QUALIFICATION')
    need(summary['safety_mode'] == MODE and summary['qualification'] is False, 'NO_QUALIFICATION')
    need(len(summary['wallets']) == len(summary['partial_week']) == 2 and
         all(len(w) == 2 for w in summary['weekly']), 'SUMMARY_BOOK_COUNT')
    need(summary['first_legacy_withdrawal_ms'] == first_diagnostic and
         summary['diagnostic_triggered_windows'] == triggered and summary['legacy_path_status'] == legacy,
         'LEGACY_DIAGNOSTIC_BINDING')
    need(summary['domain_bars'] == len(domain) and summary['active_bars'] == active, 'COVERAGE')
    need(summary['reason'] == reason and summary['stop_at_ms'] == stop_at and
         summary['exit_at_ms'] == exit_at and summary['terminal_flat'], 'TERMINAL_STATE')
    need(summary['exposure_short_flat_long'] == exposure, 'EXPOSURE')
    need(len(summary['weekly']) == len(weekly), 'WEEK_COUNT')
    for pair,expected in zip(summary['weekly'],weekly):
        for actual,value in zip(pair,expected): close(actual,value,'weekly')
    for k,b in enumerate(books):
        b.check(summary['wallets'][k],mark)
        close(summary['partial_week'][k],b.cash-week_start[k],'partial_week')
        close(sum(w[k] for w in summary['weekly'])+summary['partial_week'][k],b.cash-10000,'pnl_sum')
        need(b.q == 0, 'OPEN_TERMINAL_POSITION')
    close(summary['objective'],min(b.cash for b in books)-10000,'objective')
    return dict(active_bars=active,domain_bars=len(domain),reason=reason,
                first_legacy_withdrawal_ms=first_diagnostic,diagnostic_triggered_windows=triggered,
                legacy_path_status=legacy,
                economics=[dict(net=float(b.cash-10000),gross=float(b.cash-10000+b.fees+b.slip+b.funding),
                     fees=float(b.fees),slippage=float(b.slip),funding=float(b.funding),fills=b.fills)
                     for b in books])


def audit():
    contract_path = ROOT/'docs/plans/2026-09-30-offline-diagnostic-safety.contract.json'
    c = json.loads(contract_path.read_text())
    read = lambda name: json.loads((RUN/name).read_text())
    frozen, result, model, started = (read(n) for n in
        ('execution-freeze.json','result.json','frozen-model.json','evaluation-start.json'))
    need(sha(contract_path) == frozen['contract_sha256'] and
         sha(ROOT/'docs/plans/2026-09-30-offline-diagnostic-safety.md') == frozen['plan_sha256'], 'CONTRACT_IDENTITY')
    need(all(sha(ROOT/p) == h for p,h in read('baseline.json')['preserved'].items()), 'OLD_EVIDENCE_CHANGED')
    need(all(sha(ROOT/p) == h for p,h in frozen['sources'].items()), 'SOURCE_IDENTITY')
    need(sha(ROOT/'.artifacts/execution-net-learning-20260930/build/bounded_learning_driver') == frozen['native_sha256'], 'NATIVE_IDENTITY')
    need(sha(ROOT/c['input_path']) == frozen['input_sha256'], 'INPUT_IDENTITY')
    need(sha(RUN/'execution-freeze.json') == result['execution_freeze_sha256'] ==
         read('market-attempt.json')['execution_freeze_sha256'], 'FREEZE_IDENTITY')
    need(sha(RUN/'frozen-model.json') == result['model_sha256'] == started['frozen_model_sha256'], 'MODEL_IDENTITY')
    need(frozen['frozen_at'] <= read('market-attempt.json')['started'] <= model['frozen_at'] <=
         started['started'] <= result['completed'], 'FREEZE_ORDER')
    need(result['coefficients'] == model['coefficients'] == result['learned']['coefficients'] and
         result['fixed']['coefficients'] == [1,0], 'EVALUATION_MODEL_BINDING')
    need(model['training_end_exclusive_ms'] == started['start_ms'] == c['evaluation_start_ms'] and
         started['end_ms'] == c['end_ms'] and model['scope'] == 'DEVELOPMENT_TRAIN_ONLY', 'DOMAIN_BINDING')
    need(result['market_attempts'] == 1 and result['training_vectors'] == model['evaluations'] and
         result['model_changed'] == model['changed'] and result['compute_seconds'] <= 1800, 'BUDGET_AND_CHANGE')
    need(result['confirmation_runs'] == 0 and result['confirmation_interval'] is None and
         result['scope'] == 'DEVELOPMENT_ONLY' and result['safety_mode'] == MODE and
         model['input_sha256'] == frozen['input_sha256'] and model['confirmation_runs'] == 0 and
         result['candidate_status'] == 'NO_QUALIFIED_CANDIDATE' and result['qualification'] is False and
         all(result[k] == 0 for k in ('network_requests','account_access','deployment','push_attempts')), 'NO_AUTHORITY')
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
        decision = 'NO_GO_REFERENCE_RISK'
    elif not model['changed'] or min(e['net'] for e in learned['economics']) <= 0:
        decision = 'NO_GO_NO_ABSOLUTE_LEARNING_EDGE'
    elif not all(v is not None and v > 0 for v in lowers.values()):
        decision = 'INSUFFICIENT_DEVELOPMENT_EVIDENCE'
    else:
        decision = 'DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE'
    need(decision == result['verdict'], 'VERDICT')
    return dict(schema='offline_diagnostic_safety_audit_v1',status='PASS_EXISTING_RECEIPTS_ONLY',
                training_paths=len(trials),decimal_books=2*(len(trials)+2),training=trials,
                fixed=fixed,learned=learned,verdict=decision,confirmation_runs=0,
                qualification=False,result_sha256=sha(RUN/'result.json'))


if __name__ == '__main__':
    report = audit()
    destination = RUN/'independent-audit.json'
    if destination.exists(): need(json.loads(destination.read_text()) == report, 'AUDIT_CHANGED')
    else: save(destination,report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('training','fixed','learned')}))
