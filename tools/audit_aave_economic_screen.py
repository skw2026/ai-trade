"""Independent Decimal reference journals and interval-HAC adjudication."""
from decimal import Decimal as D, ROUND_FLOOR, localcontext
import math
import json


def ensure(ok, reason):
    if not ok:
        raise ValueError(reason)


def exact_schedule(opportunities, data):
    """Rebuild the real causal schedule without trusting model output."""
    result = []
    for index, event in enumerate(opportunities):
        if event['exclusion'] is not None:
            continue
        first, entry, end = [event[key]*1000 for key in ('first', 'entry', 'exit')]
        anchor = first-first % 60000
        if anchor == first:
            anchor -= 60000
        prices = [D(data['trade'][anchor-offset*60000][3]) for offset in range(61, 0, -1)]
        ensure(anchor < first and entry > first+60000 and end-entry == 300000,
               'AUDIT_CAUSAL_CLOCK')
        logs = [math.log(float(right)/float(left)) for left, right in zip(prices, prices[1:])]
        result.append(dict(group=index, first_ms=first, entry_ms=entry, exit_ms=end,
            candidate=event['candidate_opportunity'], signal=event['signal'],
            baseline=prices[-1] < prices[-6], pre_event_return=float(prices[-1]/prices[-6]-1),
            prior_60minute_realized_volatility=math.sqrt(math.fsum(v*v for v in logs)),
            utc_hour=(first//1000//3600) % 24))
    ensure(all(a['exit_ms']+5000 < b['entry_ms'] for a, b in zip(result, result[1:])),
           'AUDIT_EVENT_OVERLAP')
    return result


def exact_path(rows, data, c, multiplier, arm):
    initial = D(str(c['initial_capital']))
    balances = [initial, initial]
    peaks = [initial, initial]
    drawdowns = [D(0), D(0)]
    max_notional = D(0)
    totals = dict.fromkeys(('gross', 'fees', 'slippage', 'funding_lo', 'funding_hi'), D(0))
    fee = D(str(c['fee_bps_per_side']))*multiplier/10000
    slip = D(str(c['slippage_bps_per_side']))*multiplier/10000
    step = D(c['quantity_step'])
    qty = fill = D(0)
    stop = None
    journal = []
    fills = 0

    def observe(cash, position, purchase, ohlc, t, phase):
        nonlocal max_notional, stop
        op, high, low, close = ohlc
        low_equities = [cash[i]+position*(high-purchase) for i in (0, 1)]
        high_equities = [cash[i]+position*(low-purchase) for i in (0, 1)]
        end_upper = cash[1]+position*(close-purchase)
        guaranteed_peak = max(peaks[0], high_equities[0])
        possible_peak = max(peaks[1], high_equities[1])
        lower_dd = max(D(0), (peaks[0]-low_equities[1])/peaks[0], (guaranteed_peak-end_upper)/guaranteed_peak)
        upper_dd = max(D(0), (possible_peak-low_equities[0])/possible_peak)
        drawdowns[0] = max(drawdowns[0], lower_dd); drawdowns[1] = max(drawdowns[1], upper_dd)
        peaks[:] = [guaranteed_peak, possible_peak]
        max_notional = max(max_notional, -position*high/initial)
        decision = ('REJECT_REFERENCE_RISK' if lower_dd >= D(str(c['maximum_drawdown'])) or max_notional > D(str(c['maximum_notional_fraction']))
                    else 'INSUFFICIENT_RISK_ORDER' if upper_dd >= D(str(c['maximum_drawdown'])) else None)
        if decision:
            stop = dict(decision=decision, at_ms=t, phase=phase, equity_lo=float(low_equities[0]),
                        equity_hi=float(high_equities[1]), no_stop_fill_invented=True)
        return stop is not None

    for row in rows:
        start, end = row['entry_ms'], row['exit_ms']
        prior = list(balances)
        mid = D(data['trade'][start][0])
        fill = mid*(1-slip)
        units = (initial*D(str(c['entry_notional_fraction']))/fill/step).to_integral_value(rounding=ROUND_FLOOR)
        qty = -units*step if row[arm] else D(0)
        item = dict(group=row['group'], entry_ms=start, exit_ms=end, active=bool(qty), quantity=float(qty),
                    zero_rounded=row[arm] and qty == 0)
        if not qty:
            fill = D(0)
            journal.append(dict(item, complete=True, net_lo=0.0, net_hi=0.0, cash_lo=float(balances[0]),
                cash_hi=float(balances[1]), gross=0.0, fees=0.0, slippage=0.0, funding_lo=0.0, funding_hi=0.0))
            continue
        entry_fee = -qty*fill*fee
        post_entry = [v-entry_fee for v in prior]
        fs = []
        for t in sorted(data['funding']):
            if t < start-5000 or t > end+5000:
                continue
            ticks = range((t-5000)//60000*60000, (t+5000)//60000*60000+1, 60000)
            marks = [list(map(D, data['mark'][tick])) for tick in ticks]
            lo, hi = min(p[2] for p in marks), max(p[1] for p in marks)
            payments = [-qty*D(data['funding'][t])*p for p in (lo, hi)]
            if abs(t-start) <= 5000 or abs(t-end) <= 5000:
                payments += [D(0)]
            fs.append((max(start, t-5000), min(end, t+5000), min(payments), max(payments)))
        fills += 1
        tick = start
        while tick < end:
            balance = list(post_entry)
            for earliest, latest, lo, hi in fs:
                if latest < tick:
                    balance[0] += lo; balance[1] += hi
                elif earliest <= tick+59999:
                    balance[0] += min(D(0), lo); balance[1] += max(D(0), hi)
            if observe(balance, qty, fill, list(map(D, data['mark'][tick])), tick, 'held_minute'):
                balances = balance
                totals['fees'] += entry_fee
                totals['funding_lo'] += balance[0]-post_entry[0]
                totals['funding_hi'] += balance[1]-post_entry[1]
                break
            tick += 60000
        if stop:
            journal.append(dict(item, complete=False, net_lo=None, net_hi=None,
                                cash_lo=float(balances[0]), cash_hi=float(balances[1])))
            break
        funds = [sum((v[i] for v in fs), D(0)) for i in (2, 3)]
        funded = [post_entry[i]+funds[i] for i in (0, 1)]
        mark = D(data['mark'][end][0])
        if observe(funded, qty, fill, [mark]*4, end, 'exit_mark'):
            balances = funded
            totals['fees'] += entry_fee
            totals['funding_lo'] += funds[0]; totals['funding_hi'] += funds[1]
            journal.append(dict(item, complete=False, net_lo=None, net_hi=None,
                                cash_lo=float(balances[0]), cash_hi=float(balances[1])))
            break
        last = D(data['trade'][end][0])
        cover = last*(1+slip)
        pnl, gross = qty*(cover-fill), qty*(last-mid)
        fees = entry_fee-qty*cover*fee
        balances = [prior[i]+pnl-fees+funds[i] for i in (0, 1)]
        totals['gross'] += gross; totals['fees'] += fees
        totals['slippage'] += gross-pnl
        totals['funding_lo'] += funds[0]; totals['funding_hi'] += funds[1]
        fills += 1
        journal.append(dict(item, complete=True, net_lo=float(balances[0]-prior[0]), net_hi=float(balances[1]-prior[1]),
            cash_lo=float(balances[0]), cash_hi=float(balances[1]), gross=float(gross), fees=float(fees),
            slippage=float(gross-pnl), funding_lo=float(funds[0]), funding_hi=float(funds[1])))
        qty = fill = D(0)
        if observe(balances, qty, fill, [mark]*4, end, 'exit_cash'):
            break
    return dict(arm=arm, multiplier=multiplier, complete=stop is None, stop=stop, cash_lo=float(balances[0]),
        cash_hi=float(balances[1]), drawdown_lo=float(drawdowns[0]), drawdown_hi=float(drawdowns[1]),
        maximum_notional_fraction=float(max_notional), fills=fills, quantity=float(qty), entry_fill=float(fill),
        journal=journal, **{k: float(v) for k, v in totals.items()})


def compare(actual, expected):
    if isinstance(expected, dict):
        for k, v in expected.items():
            ensure(k in actual, 'AUDIT_FIELD_MISSING')
            compare(actual[k], v)
    elif isinstance(expected, list):
        ensure(len(actual) == len(expected), 'AUDIT_LENGTH')
        for left, right in zip(actual, expected): compare(left, right)
    elif isinstance(expected, float):
        ensure(type(actual) in (int, float) and math.isfinite(actual) and
               math.isclose(actual, expected, abs_tol=1e-8, rel_tol=1e-11), 'AUDIT_NUMBER_MISMATCH')
    else:
        ensure(actual == expected, 'AUDIT_VALUE_MISMATCH')


def exact_hac(intervals):
    with localcontext() as context:
        context.prec = 50
        n = len(intervals)
        lower, upper = zip(*[(D(str(l)), D(str(u))) for l, u in intervals])
        middle = [(l+u)/2 for l, u in zip(lower, upper)]
        center = [v-sum(middle)/n for v in middle]
        quadratic = sum(center[i]*center[j]*max(D(0), 1-D(abs(i-j))/5)
                        for i in range(n) for j in range(n))
        radius_squared = sum(((u-l)/2)**2 for l, u in zip(lower, upper))
        se = (max(D(0), quadratic).sqrt()+(5*radius_squared).sqrt())/n
        return dict(mean_lo=float(sum(lower)/n), mean_hi=float(sum(upper)/n),
            standard_error_upper=float(se), lower=float(sum(lower)/n-D('1.6448536269514722')*se), weeks=n)


def decision(paths, c):
    candidate = [paths['candidate_base'], paths['candidate_stress']]
    if any(v['stop'] and v['stop']['decision'] == 'REJECT_REFERENCE_RISK' for v in candidate):
        return 'REJECT_REFERENCE_RISK', {}
    if any(v['stop'] for v in candidate):
        return 'INSUFFICIENT_RISK_ORDER', {}
    if any(not v['complete'] for v in paths.values()):
        return 'INSUFFICIENT_CONTROL_PATH', {}
    stress, control = paths['candidate_stress'], paths['baseline_stress']
    half = [sum(D(str(v['net_lo'])) for v in stress['journal'] if (v['entry_ms'] < c['split_ms']) == (h == 0)) for h in (0, 1)]
    residual = sum(D(str(v['net_lo'])) for v in stress['journal'])-max([D(0)]+[D(str(v['net_lo'])) for v in stress['journal']])
    paired = D(str(stress['cash_lo']))-D(str(control['cash_hi']))
    weeks = [[[D(0), D(0)] for _ in range(52)] for _ in range(2)]
    for left, right in zip(stress['journal'], control['journal']):
        index = (left['entry_ms']-c['evaluation_start_ms'])//604800000
        if index < 52:
            lo, hi = (D(str(left[k])) for k in ('net_lo', 'net_hi'))
            weeks[0][index][0] += lo; weeks[0][index][1] += hi
            weeks[1][index][0] += lo-D(str(right['net_hi']))
            weeks[1][index][1] += hi-D(str(right['net_lo']))
    statistics = dict(candidate_stress=exact_hac(weeks[0]), paired_stress=exact_hac(weeks[1]))
    outcome = ('REJECT_NET_EDGE' if any(v['cash_lo'] <= c['initial_capital'] for v in candidate) else
        'REJECT_PAIRED_EDGE' if paired <= 0 else 'REJECT_SUBPERIOD_STABILITY' if min(half) <= 0 else
        'INSUFFICIENT_CONCENTRATION' if residual <= 0 else
        'INSUFFICIENT_STATISTICAL_SUPPORT' if any(v['lower'] <= 0 for v in statistics.values()) else
        'RETAIN_DEVELOPMENT_ONLY_NOT_CONFIRMED')
    return outcome, dict(weekly_hac=statistics, half_year_stress_lower=list(map(float, half)),
                        leave_largest_group_stress_lower=float(residual), paired_stress_lower=float(paired))


def main():
    import aave_market_input as inp
    c = inp.guard()
    data, opportunities, _ = inp.market()
    result = json.loads((inp.ROOT/'result.json').read_text())
    reservation = json.loads((inp.ROOT/'economic-attempt.json').read_text())
    ensure(reservation['freeze_sha256'] == inp.sha(inp.ROOT/'freeze.json')
           and result['freeze_sha256'] == reservation['freeze_sha256'], 'AUDIT_ATTEMPT_BINDING')
    schedule = exact_schedule(opportunities, data)
    compare(result['schedule'], schedule)
    ensure(set(result['paths']) == {'candidate_base', 'candidate_stress', 'baseline_base', 'baseline_stress'},
           'AUDIT_FOUR_PATHS_REQUIRED')
    exact = {}
    for name, path in result['paths'].items():
        arm, cost = name.split('_')
        multiplier = 1 if cost == 'base' else c['stress_multiplier']
        ensure(path['arm'] == arm and path['multiplier'] == multiplier, 'AUDIT_PATH_IDENTITY')
        exact[name] = exact_path(schedule, data, c, multiplier, arm)
        compare(path, exact[name])
    verdict, derived = decision(exact, c)
    ensure(result['assessment']['decision'] == verdict, 'AUDIT_DECISION_MISMATCH')
    compare(result['assessment'], derived)
    ensure(result['assessment']['economic_attempts'] == 1 and result['assessment']['qualification'] is False
           and result['assessment']['independent_confirmation_admitted'] is False, 'AUDIT_AUTHORITY')
    report = dict(status='INDEPENDENT_DECIMAL_CASH_RISK_AND_HAC_PASS', decision=verdict,
        result_sha256=inp.sha(inp.ROOT/'result.json'), protected_files=inp.preserve(),
        paths={k: dict(cash_lo=v['cash_lo'], cash_hi=v['cash_hi'], fills=v['fills'], complete=v['complete']) for k, v in exact.items()},
        economic_attempts=1, g4='NOT_ACHIEVED')
    inp.save(inp.ROOT/'economic-audit.json', report)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__': main()
