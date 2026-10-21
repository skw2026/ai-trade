"""Independent queue causality and Decimal accounts; no model replay or tuning."""
from decimal import Decimal as D
import json
from pathlib import Path

from audit_execution_net_learning import Ledger, close
from audit_offline_policy_correction import audit_action, sign
from audit_frozen_history_transfer import MODE
from run_bounded_learning import need, sha


def audit_delay(path, domain, summary):
    need(sha(path) == summary['trace_sha256'] and summary['coefficients'] == [0,-4], 'TRACE_OR_VECTOR')
    books = [Ledger(False), Ledger(True)]
    reason = 'COMPLETE'; stop_at = exit_at = first = None
    pending = latched = False; active = count = triggered = 0
    nets, streak, exposure = [D(0)]*4, [0]*4, [0]*3
    weekly, week_start, queue = [], [D(10000)]*2, D(0)
    with Path(path).open() as source:
        events = (json.loads(line) for line in source)
        for i, sample in enumerate(domain, 1):
            r = sample['row']; ts = int(r['timestamp']); mark = D(r['mark_close'])
            if pending:
                e = next(events)
                need(e['kind'] == 'EXIT_OPEN' and e['timestamp'] == ts, 'RISK_EXIT_TIME')
                for b in books:
                    b.fund(D(r['mark_open']), D(r['funding_rate_per_interval']))
                    b.trade(D(0), D(r['open']), D(r['mark_open'])); b.last, b.liquid = D(0), b.cash
                pending, latched, exit_at, queue = False, True, ts, D(0)
                if any(b.dd >= D('.08') for b in books): reason = 'REFERENCE_RISK_STOP'
                need(e['reason'] == reason and len(e['wallets']) == 2, 'EXIT_STATE')
                for b, s in zip(books, e['wallets']): b.check(s, mark)
            elif not latched:
                e = next(events)
                need(e['kind'] == 'BAR' and e['timestamp'] == ts, 'BAR_IDENTITY')
                new = audit_action(sample['features'], [0,-4], sign(books[0].last))
                close(e['decision_target'], new, 'new_signal')
                target, queue = queue, new
                close(e['target'], target, 'queued_action')
                for b in books: b.fund(D(r['mark_open']), D(r['funding_rate_per_interval']))
                opening = any(b.dd >= D('.08') for b in books)
                need(opening == e['opening_risk'], 'OPENING_RISK')
                receipts = [b.step(r, target, funded=True, halt=opening) for b in books]
                need(len(e['receipts']) == len(e['wallets']) == 2, 'BOOK_COUNT')
                for a, v in zip(e['receipts'], receipts): close(a, v, 'net_receipt')
                active += 1; assessment = None
                if any(b.dd >= D('.08') for b in books):
                    reason, stop_at, pending, queue = 'REFERENCE_RISK_STOP', ts+300000, True, D(0)
                else:
                    count += 1; nets[sample['bucket']] += receipts[0]; nets[3] += receipts[0]
                    if count == 240:
                        streak = [n+1 if v < 0 else 0 for n, v in zip(streak, nets)]
                        trigger = any(n >= 2 for n in streak); triggered += int(trigger)
                        if trigger and first is None: first = ts+300000
                        assessment = dict(mode=MODE, streak=streak[:], would_withdraw=trigger, withdrawn=False)
                        need(e['safety'] is not None and len(e['safety']['net_by_decision_bucket_and_total']) == 4,
                             'DIAGNOSTIC_SHAPE')
                        for a, v in zip(e['safety']['net_by_decision_bucket_and_total'], nets): close(a, v, 'diagnostic_net')
                        nets, count = [D(0)]*4, 0
                if assessment is None: need(e['safety'] is None, 'EXTRA_DIAGNOSTIC')
                else: need(all(e['safety'][k] == v for k, v in assessment.items()), 'DIAGNOSTIC_STATE')
                need(e['reason'] == reason, 'REASON')
                for b, s in zip(books, e['wallets']): b.check(s, mark)
            exposure[sign(books[0].q)+1] += 1
            if i % 2016 == 0:
                values = [b.liquid for b in books]
                weekly.append([v-w for v, w in zip(values, week_start)]); week_start = values
        terminal = next(events)
        need(terminal['kind'] == 'TERMINAL' and next(events, None) is None, 'TERMINAL_EVENT')
        need(terminal['result'] == {k:v for k,v in summary.items() if k not in ('trace_file','trace_sha256')},
             'TERMINAL_BINDING')
    r = domain[-1]['row']; mark = D(r['mark_close'])
    if not latched:
        for b in books:
            b.trade(D(0), D(r['price']), mark); b.last, b.liquid = D(0), b.cash
        if any(b.dd >= D('.08') for b in books):
            if stop_at is None: stop_at = int(r['timestamp'])+300000
            reason = 'REFERENCE_RISK_STOP'
        exit_at = int(r['timestamp'])+300000
    if len(domain) % 2016 == 0:
        for k, b in enumerate(books): weekly[-1][k] += b.cash-week_start[k]
        week_start = [b.cash for b in books]
    legacy = ('REJECT_LEGACY_SAFETY' if first is not None else
              'REJECT_REFERENCE_RISK' if reason != 'COMPLETE' else 'PATH_CONSTRAINTS_ONLY_NOT_QUALIFICATION')
    need(summary['safety_mode'] == MODE and summary['qualification'] is False, 'QUALIFICATION')
    need(summary['first_legacy_withdrawal_ms'] == first and summary['diagnostic_triggered_windows'] == triggered and
         summary['legacy_path_status'] == legacy, 'LEGACY_DIAGNOSTIC')
    need(summary['domain_bars'] == len(domain) and summary['active_bars'] == active, 'COVERAGE')
    need(summary['reason'] == reason and summary['stop_at_ms'] == stop_at and summary['exit_at_ms'] == exit_at and
         summary['terminal_flat'], 'TERMINAL_STATE')
    need(summary['exposure_short_flat_long'] == exposure and len(summary['weekly']) == len(weekly), 'BLOCKS')
    need(len(summary['wallets']) == len(summary['partial_week']) == 2 and
         all(len(w) == 2 for w in summary['weekly']), 'SUMMARY_SHAPE')
    for pair, expected in zip(summary['weekly'], weekly):
        for a, v in zip(pair, expected): close(a, v, 'weekly')
    for k, b in enumerate(books):
        b.check(summary['wallets'][k], mark)
        close(summary['partial_week'][k], b.cash-week_start[k], 'partial_week')
        close(sum(w[k] for w in summary['weekly'])+summary['partial_week'][k], b.cash-10000, 'pnl_sum')
        need(b.q == 0, 'TERMINAL_NOT_FLAT')
    close(summary['objective'], min(b.cash for b in books)-10000, 'objective')
    return dict(status='QUEUE_AND_DECIMAL_ACCOUNTS_VERIFIED', active_bars=active,
                domain_bars=len(domain), reason=reason, economics=[dict(net=float(b.cash-10000),
                gross=float(b.cash-10000+b.fees+b.slip+b.funding), fees=float(b.fees),
                slippage=float(b.slip), funding=float(b.funding), fills=b.fills) for b in books])
