"""Independent Decimal replay of intents, executions, funding and marked risk.

Does not import the strategy engine, its pricing, ranking, risk or accounting.
"""
from collections import defaultdict, deque
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from fractions import Fraction
import math
import time

D = lambda v: Decimal(str(v))
SYMBOLS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
STEPS = tuple(map(D, ('.001', '.01', '.1')))
TICKS = tuple(map(D, ('.1', '.01', '.01')))
MINUTE, DAY, WEEK = 60000, 86400000, 604800000
FIRST, LAST = 1704672000000, 1735516800000


def check(ok, why):
    if not ok:
        raise ValueError(why)


def near(a, b, tol='0.000001'):
    return abs(D(a)-D(b)) <= D(tol)


def intent(daily, t, horizon):
    c = [D(daily[s][str(t)]) for s in SYMBOLS]
    old = [D(daily[s][str(t-horizon*DAY)]) for s in SYMBOLS]
    ratios = [Fraction(a)/Fraction(b) for a, b in zip(c, old)]
    q = [D(0)]*3
    tied = len(set(ratios)) < 3
    if not tied:
        low, high = ratios.index(min(ratios)), ratios.index(max(ratios))
        for i, sign in ((low, -1), (high, 1)):
            units = Fraction(1250)/Fraction(c[i])/Fraction(STEPS[i])
            q[i] = sign*(units.numerator//units.denominator)*STEPS[i]
        if any(q[i] == 0 or abs(q[i])*c[i] < 5 for i in (low, high)):
            q = [D(0)]*3
    return dict(t=t, horizon=horizon, closes=list(map(str, c)), prior=list(map(str, old)),
                quantities=list(map(str, q)), tied=tied)


def audit_book(archive, snap, index, deadline=None):
    data = snap['books'][index]
    stress, delay = bool(index), snap['delay']
    check(data['stress'] == stress and data['delay'] == delay, 'BOOK_IDENTITY')
    events = defaultdict(list); last = -1
    for e in data['events']:
        check(type(e['minute']) is int and last <= e['minute'] < snap['minute'], 'EVENT_CLOCK')
        last = e['minute']; events[last].append(e)
    cash = D(10000); q = [D(0)]*3
    leg_cash = [D(0)]*3; collateral = [D(0)]*3
    fees = funding = slippage = gross_cash = turnover = D(0)
    fee, slip = D('.0011' if stress else '.00055'), D('.0004' if stress else '.0002')
    peak = D(10000); drawdown = D(0)
    pending = deque(); exiting = False; exit_deadline = None
    stopped = None; insufficient = None; complete = False
    cycle = None; cycles = []; failures = 0; fills = 0
    daily = [[snap['start'], 10000.]]; daily_cash = [[snap['start'], '10000']]; signals = []
    last_sample = D(10000); window = D(0); window_count = 0
    neg = pos = 0; alert = False; alert_checks = []; first_alert = None
    active = set(); position_minutes = 0; gross_integral = net_integral = btc_integral = D(0)
    max_gross = max_net = D(0); risk_times = []; execution_failure = None

    def liquid(prices):
        return cash+sum(qty*D(p)-abs(qty)*(D(p)*(fee+slip)+tick)
                        for qty, p, tick in zip(q, prices, TICKS))

    def risk(mark):
        nonlocal peak, drawdown
        good = [b[1] if qty >= 0 else b[2] for qty, b in zip(q, mark)]
        bad = [b[2] if qty >= 0 else b[1] for qty, b in zip(q, mark)]
        peak = max(peak, liquid(good))
        drawdown = max(drawdown, (peak-liquid(bad))/peak)
        reasons = []
        if drawdown >= D('.08'):
            reasons.append('RESEARCH_DRAWDOWN_8_PERCENT')
        if sum(abs(qty)*D(b[1]) for qty, b in zip(q, mark)) > 5000:
            reasons.append('GROSS_EXPOSURE_CAP')
        for i, qty in enumerate(q):
            if qty:
                cushion = collateral[i]+leg_cash[i]+qty*D(bad[i])
                if cushion <= 0:
                    reasons.append('REFERENCE_COLLATERAL_EXHAUSTED')
                if cushion/(abs(qty)*D(bad[i])) < D('.08'):
                    reasons.append('REFERENCE_CUSHION_UNDER_8_PERCENT')
        return reasons

    last_value = D(10000)
    for m in range(snap['minute']):
        if m % 1440 == 0 and deadline is not None:
            check(time.monotonic() < deadline, 'AUDIT_COMPUTE_BUDGET')
        t = snap['start']+m*MINUTE
        bars, marks = archive.bars(t, 'trade'), archive.bars(t, 'mark')
        here = events[m]
        entries = [e for e in here if e['kind'] == 'entry_requested']
        expected = None
        if FIRST <= t < LAST and (t-FIRST) % WEEK == 0 and stopped is None:
            expected = intent(archive.daily, t, snap['horizon'])
            signals.append(expected)
        should_enter = expected is not None and any(map(D, expected['quantities']))
        check(len(entries) == int(should_enter), 'MISSING_OR_EXTRA_WEEKLY_ENTRY')
        if entries:
            check(entries[0]['intent'] == expected and not any(q) and not pending and cycle is None,
                  'CAUSAL_INTENT_AND_NO_OVERLAP')
            amounts = list(map(D, expected['quantities']))
            active_legs = [i for i, v in enumerate(amounts) if v]
            pending.extend((m+1+delay+j, i, amounts[i], 'entry') for j, i in enumerate(active_legs))
            cycle = dict(start=m, cash=cash, fees=fees, funding=funding, slippage=slippage,
                         quantities=expected['quantities'])
            complete = False
        weekly_exit = (FIRST <= t < LAST and (t-FIRST) % WEEK == WEEK-10*MINUTE and
                       (any(q) or pending) and not exiting)
        check(sum(e['kind'] == 'exit_requested' and e['reason'] == 'WEEK_END' for e in here) ==
              int(bool(weekly_exit)), 'MISSING_OR_EXTRA_WEEKLY_EXIT')
        # Settlement uses pre-fill quantity, even if an entry/exit is requested at this clock.
        rates = archive.rates(t)
        fund_events = [e for e in here if e['kind'] == 'funding']
        check([e['leg'] for e in fund_events] == [i for i in sorted(rates) if q[i]],
              'FUNDING_EVENT_COVERAGE')
        for e in fund_events:
            i = e['leg']
            check(D(e['rate']) == D(rates[i]) and D(e['mark']) == D(marks[i][0]), 'FUNDING_SOURCE')
            amt = q[i]*D(marks[i][0])*D(rates[i])
            cash -= amt; leg_cash[i] -= amt; funding += amt
            check(near(amt, e['amount']) and near(cash, e['cash']), 'FUNDING_LEDGER')
        ignored = stopped is not None and not any(q) and not pending
        before = [] if ignored else risk(marks)
        partial = False; stop_here = []
        for e in here:
            kind = e['kind']
            if kind in ('entry_requested', 'funding'):
                continue
            if kind == 'exit_requested':
                check(not exiting and e['deadline'] == m+5, 'EXIT_DEADLINE')
                if e['reason'] == 'PARTIAL_OR_FAILED_ENTRY':
                    check(partial, 'UNJUSTIFIED_PARTIAL_EXIT')
                elif e['reason'] == 'WEEK_END':
                    check(weekly_exit, 'UNJUSTIFIED_WEEKLY_EXIT')
                else:
                    check(stopped is not None, 'UNJUSTIFIED_RISK_EXIT')
                pending.clear(); exiting = True; exit_deadline = m+5
                if any(q):
                    pending.append((m+1+delay, next(i for i, v in enumerate(q) if v), None, 'exit'))
            elif kind in ('fill', 'no_fill'):
                check(pending and pending[0][0] == m, 'UNSCHEDULED_FILL')
                due, i, requested, phase = pending.popleft()
                if phase == 'exit':
                    requested = -q[i]
                check(e['leg'] == i and e['phase'] == phase and D(e['requested']) == requested,
                      'ORDER_LEG_OR_QUANTITY')
                cap = (D(bars[i][4])*D('.01')/STEPS[i]).to_integral_value(rounding=ROUND_FLOOR)*STEPS[i]
                amount = min(abs(requested), cap)
                raw = D(bars[i][1 if requested > 0 else 2] if stress else bars[i][0])
                px = (raw*(1+slip if requested > 0 else 1-slip)/TICKS[i]).to_integral_value(
                    rounding=ROUND_CEILING if requested > 0 else ROUND_FLOOR)*TICKS[i]
                if amount*px < 5:
                    amount = D(0)
                check((kind == 'fill') == bool(amount), 'CAPACITY_OR_MIN_NOTIONAL')
                if amount:
                    delta = amount if requested > 0 else -amount
                    paid = amount*px*fee
                    check(D(e['delta']) == delta and D(e['price']) == px and near(e['fee'], paid),
                          'EXECUTION_PRICE_OR_SIZE')
                    if phase == 'entry':
                        check(q[i] == 0, 'ENTRY_TOPUP')
                        collateral[i] = amount*px
                    else:
                        check(q[i]*delta < 0 and amount <= abs(q[i]), 'EXIT_RISK_INCREASE')
                    q[i] += delta; cash -= delta*px+paid; leg_cash[i] -= delta*px+paid
                    fees += paid; slippage += delta*(px-D(bars[i][0]))
                    gross_cash -= delta*D(bars[i][0]); turnover += amount*px; fills += 1
                    check(near(cash, e['cash']), 'FILL_CASH')
                if phase == 'entry':
                    if amount != abs(requested):
                        partial = True; failures += 1
                    elif not pending:
                        complete = True
                elif any(q):
                    nxt = i if q[i] else next(j for j, v in enumerate(q) if v)
                    pending.append((m+1, nxt, None, 'exit'))
            elif kind == 'flat':
                check(not any(q) and near(e['cash'], cash) and e['completed'] == complete, 'FALSE_FLAT')
                if cycle:
                    cycles.append(dict(start=cycle['start'], end=m, completed=complete,
                        quantities=cycle['quantities'], net=str(cash-cycle['cash']),
                        fees=str(fees-cycle['fees']), funding=str(funding-cycle['funding']),
                        slippage=str(slippage-cycle['slippage'])))
                cycle = None; complete = False; pending.clear(); exiting = False; exit_deadline = None
                leg_cash = [D(0)]*3; collateral = [D(0)]*3
            elif kind == 'stop':
                check(stopped is None, 'STOP_RESET')
                stopped = dict(minute=m, reason=e['reason']); stop_here.append(e['reason'])
            elif kind == 'insufficient':
                check(exiting and m > exit_deadline and any(q) and
                      e['reason'] == 'RESIDUAL_EXPOSURE_UNCLOSED', 'FALSE_RESIDUAL_FAILURE')
                insufficient = e['reason']
                execution_failure = dict(minute=m, reason=e['reason'])
            else:
                raise ValueError('UNKNOWN_EVENT:'+kind)
        after = [] if ignored else risk(marks)
        reasons = before+after
        if reasons:
            check(stopped is not None, 'MISSED_HARD_RISK')
            if not risk_times:
                risk_times.append(dict(minute=m, reasons=reasons))
        for reason in stop_here:
            if reason not in ('PEER_PATH_REJECTED', 'PERFORMANCE_REVIEW_FAILED'):
                check(reason in reasons, 'FALSE_HARD_STOP')
        check(not pending or pending[0][0] > m or insufficient, 'MISSED_ORDER')
        if partial:
            check(any(e['kind'] == 'exit_requested' and e['reason'] == 'PARTIAL_OR_FAILED_ENTRY'
                      for e in here), 'PARTIAL_ENTRY_NOT_ABORTED')
        last_value = liquid([b[3] for b in marks])
        peak = max(peak, last_value); drawdown = max(drawdown, (peak-last_value)/peak)
        if (m+1) % 5 == 0:
            if not stopped and not insufficient:
                window += last_value-last_sample; window_count += 1
                if window_count == 240:
                    neg = neg+1 if window < 0 else 0
                    pos = pos+1 if window > 0 else 0
                    if neg >= 2:
                        alert = True
                        if first_alert is None:
                            first_alert = m+1
                    # Independently reconstructed input/accounting/execution passed at this clock.
                    reviewed = True if alert else None
                    if alert and pos >= 2:
                        alert = False
                    alert_checks.append(dict(minute=m+1, net=float(window), negative=neg,
                                             positive=pos, active=alert, review_passed=reviewed))
                    window = D(0); window_count = 0
            last_sample = last_value
        if any(q):
            active.add((t-FIRST)//WEEK); position_minutes += 1
            exposures = [v*D(b[3]) for v, b in zip(q, marks)]
            gross, net = sum(map(abs, exposures)), sum(exposures)
            gross_integral += gross; net_integral += net; btc_integral += exposures[0]
            max_gross = max(max_gross, gross); max_net = max(max_net, abs(net))
        if (t+MINUTE) % DAY == 0:
            daily.append([t+MINUTE, float(last_value)])
            daily_cash.append([t+MINUTE, str(cash) if not any(q) else None])
    if snap['start']+snap['minute']*MINUTE == snap['end'] and (any(q) or pending):
        insufficient = insufficient or 'TERMINAL_RESIDUAL'
    for k, value in dict(cash=cash, fees=fees, funding=funding, slippage=slippage,
                         gross=gross_cash, turnover=turnover).items():
        check(near(data[k], value), 'TERMINAL_'+k)
    for k, value in dict(q=q, leg_cash=leg_cash, collateral=collateral).items():
        check(list(map(D, data[k])) == value, 'TERMINAL_'+k)
    check(near(data['peak'], peak) and near(data['dd'], drawdown, '1e-9'), 'RISK_RECONSTRUCTION')
    check(data['stopped'] == stopped and data['insufficient'] == insufficient, 'STOP_OR_INSUFFICIENT')
    check(data['failed_entries'] == failures and len(data['cycles']) == len(cycles), 'CYCLE_COUNT')
    for actual, expected in zip(data['cycles'], cycles):
        check(all(actual[k] == expected[k] for k in ('start', 'end', 'completed', 'quantities')) and
              all(near(actual[k], expected[k]) for k in ('net', 'fees', 'funding', 'slippage')),
              'CYCLE_ATTRIBUTION')
    check(data['exiting'] == exiting and data['deadline'] == exit_deadline and
          data['complete_entry'] == complete, 'EXECUTION_CHECKPOINT')
    check(data['queue'] == [[m, i, None if v is None else str(v), p] for m, i, v, p in pending],
          'PENDING_CHECKPOINT')
    check((data['cycle'] is None) == (cycle is None), 'ACTIVE_CYCLE')
    if cycle:
        check(data['cycle']['start'] == cycle['start'] and
              data['cycle']['quantities'] == cycle['quantities'] and
              all(near(data['cycle'][k], cycle[k]) for k in ('cash', 'fees', 'funding', 'slippage')),
              'CYCLE_CHECKPOINT')
    check(data['active_weeks'] == sorted(active) and data['position_minutes'] == position_minutes,
          'ACTIVITY_ACCOUNTING')
    for k, value in dict(gross_integral=gross_integral, net_integral=net_integral,
                         btc_integral=btc_integral, max_gross=max_gross, max_abs_net=max_net).items():
        check(near(data[k], value, '.01'), 'EXPOSURE_'+k)
    check(signals == snap['signals'], 'COMPLETE_SIGNAL_TRACE')
    check(len(daily) == len(snap['daily']) and
          all(a[0] == b[0] and near(a[1], b[index+1]) for a, b in zip(daily, snap['daily'])),
          'DAILY_TRACE')
    check(len(daily_cash) == len(snap['daily_cash']) and
          all(x[0] == y[0] and ((x[1] is None and y[index+1] is None) or
              (x[1] is not None and y[index+1] is not None and near(x[1], y[index+1], '1e-12')))
              for x, y in zip(daily_cash, snap['daily_cash'])), 'EXACT_FLAT_CASH_BOUNDARIES')
    actual_checks = data['alert']['checks']
    check(len(alert_checks) == len(actual_checks) and
          first_alert == data['alert']['first_legacy_withdrawal'], 'ALERT_COUNT')
    for a, b in zip(alert_checks, actual_checks):
        check(all(a[k] == b[k] for k in a if k != 'net') and near(a['net'], b['net']), 'ALERT_TRACE')
    check(data['alert']['count'] == window_count and near(data['alert']['total'], window) and
          data['alert']['negative'] == neg and data['alert']['positive'] == pos and
          data['alert']['active'] == alert and near(data['last_sample'], last_sample), 'ALERT_CHECKPOINT')
    check(near(data['last_value'], last_value), 'LAST_VALUE')
    return dict(status='INDEPENDENT_INTENT_DECIMAL_EXECUTION_RISK_PASS',
                cash=str(cash), net=str(last_value-10000), fees=str(fees), funding=str(funding),
                slippage=str(slippage), gross_cash=str(gross_cash), turnover=str(turnover),
                drawdown=str(drawdown), fills=fills, cycles=len(cycles),
                stopped=stopped, first_hard_risk=risk_times[0] if risk_times else None,
                insufficient=insufficient, first_execution_failure=execution_failure,
                residual=list(map(str, q)))


def audit(archive, snap, deadline=None):
    audited = [audit_book(archive, snap, i, deadline) for i in (0, 1)]
    validate_stop_links(audited)
    return audited


def validate_stop_links(audited):
    """A group stop needs an independently reconstructed hard or execution cause."""
    for i, item in enumerate(audited):
        if item['stopped'] and item['stopped']['reason'] == 'PEER_PATH_REJECTED':
            other = audited[1-i]
            minute = item['stopped']['minute']
            hard_peer = (other['stopped'] is not None and other['stopped']['minute'] == minute and
                         other['stopped']['reason'] != 'PEER_PATH_REJECTED')
            execution_cause = any(v.get('first_execution_failure') is not None and
                                  v['first_execution_failure']['minute'] == minute and
                                  v['first_execution_failure']['reason'] == v.get('insufficient') ==
                                  'RESIDUAL_EXPOSURE_UNCLOSED' and any(map(D, v['residual']))
                                  for v in (item, other))
            check(hard_peer or execution_cause, 'LINKED_STOP')


def selection(prefixes, actual):
    eligible = {}
    for h, s in prefixes.items():
        if s['start']+s['minute']*60000 != 1719792000000:
            continue
        if all(b['stopped'] is None and b['insufficient'] is None and
               not any(map(D, b['q'])) and not b['queue'] for b in s['books']):
            eligible[h] = min(D(b['cash']) for b in s['books'])
    chosen = min(eligible, key=lambda h: (-eligible[h], h)) if eligible else None
    check(chosen == actual, 'INDEPENDENT_SELECTION')
    return dict(status='EXACT_SELECTION_PASS', chosen=chosen, eligible_scores={str(h): str(v-10000) for h, v in eligible.items()})


def decision(paths, selected, actual):
    """Recompute endpoint gates without importing the primary verdict code."""
    end, half, last = 1735689600000, 1719792000000, 1735516800000
    check(actual['g4'] is False, 'NO_G4_FROM_DEVELOPMENT')
    if selected is None:
        check(actual['candidate'] is None and not actual['learning_claim'], 'NO_SELECTION_NO_CANDIDATE')
        risk = all(any(b['stopped'] for b in s['books']) for name, s in paths.items() if name.startswith('prefix-'))
        check(actual['status'] == ('NO_GO_RISK' if risk else 'INPUT_OR_EXECUTION_INSUFFICIENT'), 'NO_SELECTION_STATUS')
        return dict(status='INDEPENDENT_DECISION_PASS', candidate=None)
    checks = {}; h2 = {}
    for h in sorted({7, selected}):
        reasons = set()
        all_books = []
        values = {}
        for mode in ('normal', 'delay'):
            s = paths[f'{mode}-{h}']; all_books.extend(s['books'])
            if s['start']+s['minute']*60000 != end:
                reasons.add('INPUT_OR_EXECUTION_INSUFFICIENT')
            calendar = {v[0]: v[1:] for v in s['daily_cash']}
            points = [half+j*604800000 for j in range(27)]+[end]
            if not all(t in calendar and None not in calendar[t] for t in points):
                values[mode] = None; reasons.add('INPUT_OR_EXECUTION_INSUFFICIENT')
                continue
            rows = []
            for i, b in enumerate(s['books']):
                w = [D(calendar[y][i])-D(calendar[x][i]) for x, y in zip(points[:26], points[1:27])]
                pnl = D(calendar[end][i])-D(calendar[half][i])
                full = D(b['cash'])-10000
                expected = actual['checks'][str(h)][mode][i]
                check(list(map(D, expected['weekly'])) == w and D(expected['h2_net']) == pnl and
                      D(expected['full_net']) == full, 'REPORT_NET_WEEKLY_ARITHMETIC')
                check(list(map(D, expected['half13_net'])) == [sum(w[:13]), sum(w[13:])] and
                      D(expected['tail']) == D(calendar[end][i])-D(calendar[last][i]) and
                      D(expected['without_best_week']) == pnl-max(w), 'REPORT_CONCENTRATION_ARITHMETIC')
                positive = sum(max(D(0), x) for x in w)
                share = max(w)/positive if positive else None
                check((expected['largest_positive_share'] is None if share is None else
                       D(expected['largest_positive_share']) == share), 'REPORT_SHARE')
                count = sum(bool(c['completed']) and half <= s['start']+c['start']*60000 < last for c in b['cycles'])
                active = len({j for j in b['active_weeks'] if 25 <= j < 51})
                check(expected['completed_cycles'] == count and expected['active_weeks'] == active, 'REPORT_ACTIVITY')
                for out, field in (('gross', 'gross'), ('fees', 'fees'), ('slippage', 'slippage'),
                                   ('signed_funding_cost', 'funding'), ('turnover', 'turnover')):
                    check(D(expected[out]) == D(b[field]), 'REPORT_COSTS')
                rows.append(dict(full=full, pnl=pnl, w=w, share=share, count=count, active=active))
            values[mode] = rows
        if any(b['stopped'] for b in all_books):
            reasons.add('NO_GO_RISK')
        if any(b['insufficient'] or any(map(D, b['q'])) or b['queue'] for b in all_books):
            reasons.add('INPUT_OR_EXECUTION_INSUFFICIENT')
        n, d = values.get('normal'), values.get('delay')
        if n is not None and d is not None:
            if not (all(r['full'] > 0 for r in n) and n[0]['pnl'] > 0 and n[1]['pnl'] >= 100):
                reasons.add('ABSOLUTE_NET_HURDLE')
            if any(r['count'] < 20 or r['active'] < 20 for r in n):
                reasons.add('INSUFFICIENT_DIVERSITY')
            p = n[1]
            if not (sum(p['w'][:13]) > 0 and sum(p['w'][13:]) > 0 and p['share'] is not None and
                    p['share'] <= D('.35') and p['pnl']-max(p['w']) > 0):
                reasons.add('CONCENTRATION_OR_HALF_STABILITY')
            if not all(r['full'] > 0 and r['pnl'] > 0 for r in d):
                reasons.add('LATENCY_NET_HURDLE')
        checks[h] = reasons
        h2[h] = [r['pnl'] for r in n] if n is not None else None
    chosen = None
    if selected != 7:
        inc = [x-y for x, y in zip(h2[selected], h2[7])] if h2[selected] is not None and h2[7] is not None else None
        check(actual['increments'] == (list(map(str, inc)) if inc is not None else None), 'REPORT_INCREMENT')
        if inc is None or not all(v >= 25 for v in inc):
            checks[selected].add('LEARNING_INCREMENT_HURDLE')
        if not checks[selected]:
            chosen = selected
    if chosen is None and not checks[7]:
        chosen = 7
    failures = set().union(*checks.values())
    check(actual['candidate'] == chosen and actual['learning_claim'] == (chosen is not None and chosen != 7),
          'INDEPENDENT_FINAL_CHOICE')
    check(set(actual['failures']) == failures and
          all(set(actual['checks'][str(h)]['failures']) == v for h, v in checks.items()), 'ALL_FAILURES_RETAINED')
    if chosen is not None:
        status = 'FIXED_DEVELOPMENT_SUPPORTED' if chosen == 7 else 'LEARNED_DEVELOPMENT_SUPPORTED'
    else:
        status = next((x for x in ('INPUT_OR_EXECUTION_INSUFFICIENT', 'NO_GO_RISK', 'INSUFFICIENT_DIVERSITY')
                       if x in failures), 'NO_GO_ECONOMIC')
    check(actual['status'] == status, 'INDEPENDENT_STATUS')
    return dict(status='INDEPENDENT_DECISION_PASS', candidate=chosen, failures=sorted(failures))
