"""Independent Decimal journal and risk bounds for the sole S2 development attempt."""
from decimal import Decimal as D, ROUND_FLOOR
import json
import math

import eth_queue_development_inputs as inp
from mvp_reference_inputs import strict_json
from run_bounded_learning import need, save, sha


def exact_path(rows, data, c, multiplier, control=False):
    initial = D(str(c['initial_capital']))
    balances = [initial, initial]
    peaks = [initial, initial]
    max_dd = [D(0), D(0)]
    maximum_notional = D(0)
    fee, slip = (D(str(c[k]))*multiplier/10000 for k in ('fee_bps_per_side', 'slippage_bps_per_side'))
    limit, cap = D(str(c['maximum_drawdown'])), D(str(c['maximum_notional_fraction']))
    totals = {k: D(0) for k in ('gross', 'fees', 'slippage', 'funding_lo', 'funding_hi')}
    position = purchase = D(0)
    daily = []; fills = active_days = 0; stop = None; marked = [initial, initial]
    last_at = rows[0]['entry_ms']

    def record_clock(t, phase, low, high, close):
        nonlocal maximum_notional, stop, marked, last_at
        lower_nav = [balances[i]+position*(high-purchase) for i in range(2)]
        upper_nav = [balances[i]+position*(low-purchase) for i in range(2)]
        end_upper = balances[1]+position*(close-purchase)
        definite_peak = max(peaks[0], upper_nav[0])
        possible_peak = max(peaks[1], upper_nav[1])
        certain_loss = max(D(0), (peaks[0]-lower_nav[1])/peaks[0],
                           (definite_peak-end_upper)/definite_peak)
        possible_loss = max(D(0), (possible_peak-lower_nav[0])/possible_peak)
        maximum_notional = max(maximum_notional, -position*high/initial)
        max_dd[0] = max(max_dd[0], certain_loss)
        max_dd[1] = max(max_dd[1], possible_loss)
        peaks[:] = [definite_peak, possible_peak]
        marked = [lower_nav[0], upper_nav[1]]; last_at = t
        status = ('REJECT_REFERENCE_RISK' if certain_loss >= limit or maximum_notional > cap else
                  'INSUFFICIENT_RISK_ORDER' if possible_loss >= limit else None)
        if status:
            stop = dict(decision=status, at_ms=t, phase=phase, equity_low=float(marked[0]),
                        equity_high=float(marked[1]), close_equity_high=float(end_upper), no_stop_fill_invented=True)
        return status is not None

    for day in rows:
        before = list(balances); before_components = dict(totals)
        entry, end = day['entry_ms'], day['exit_ms']
        active = control or day['active']
        mid = D(data['trade'][entry][0])
        if active:
            purchase = mid*(1-slip)
            step = D(c['quantity_step'])
            units = (initial*D(str(c['entry_notional_fraction']))/(purchase*step)).to_integral_value(rounding=ROUND_FLOOR)
            position = -units*step
            debit = -position*purchase*fee
            balances = [x-debit for x in balances]
            totals['fees'] += debit; fills += 1; active_days += 1
        tick = entry
        while tick < end:
            op, hi, lo, close = map(D, data['mark'][tick])
            if record_clock(tick, 'open', op, op, op): break
            if position and tick in data['funding']:
                transfers = sorted((-position*D(data['funding'][tick])*p for p in (lo, hi)))
                balances = [x+y for x, y in zip(balances, transfers)]
                totals['funding_lo'] += transfers[0]; totals['funding_hi'] += transfers[1]
                if record_clock(tick, 'funding', op, op, op): break
            if record_clock(tick, 'ohlc', lo, hi, close): break
            tick += inp.HOUR
        if stop: break
        mark = D(data['mark'][end][0]); end_mid = D(data['trade'][end][0])
        if record_clock(end, 'exit_mark', mark, mark, mark): break
        if active:
            sale = end_mid*(1+slip)
            pnl = position*(sale-purchase)
            debit = -position*sale*fee
            balances = [x+pnl-debit for x in balances]
            raw_pnl = position*(end_mid-mid)
            totals['gross'] += raw_pnl; totals['fees'] += debit
            totals['slippage'] += raw_pnl-pnl
            fills += 1; position = purchase = D(0)
        if record_clock(end, 'exit', mark, mark, mark): break
        daily.append(dict(entry_ms=entry, exit_ms=end, active=active, group=day['group'],
            net_lo=float(balances[0]-before[0]), net_hi=float(balances[1]-before[1]),
            cash_lo=float(balances[0]), cash_hi=float(balances[1]),
            **{k: float(totals[k]-before_components[k]) for k in totals}))
    return dict(cash_lo=float(balances[0]), cash_hi=float(balances[1]),
        **{k: float(v) for k, v in totals.items()}, complete=stop is None, stop=stop, fills=fills,
        qty=float(position), entry_fill=float(purchase), active_days=active_days, completed_days=len(daily),
        last_at_ms=last_at, daily=daily, drawdown_lo=float(max_dd[0]), drawdown_hi=float(max_dd[1]),
        maximum_notional_fraction=float(maximum_notional), marked_equity_lo=float(marked[0]),
        marked_equity_hi=float(marked[1]))


def compare(got, exact):
    if isinstance(exact, dict):
        for key, value in exact.items():
            need(key in got, 'MISSING_AUDIT_FIELD:'+key)
            compare(got[key], value)
    elif isinstance(exact, list):
        need(len(got) == len(exact), 'AUDIT_LIST_LENGTH')
        for a, b in zip(got, exact): compare(a, b)
    elif isinstance(exact, float):
        need(isinstance(got, (float, int)) and math.isfinite(got) and
             math.isclose(got, exact, rel_tol=1e-11, abs_tol=1e-8), 'DECIMAL_MISMATCH:'+str((got, exact)))
    else:
        need(got == exact, 'AUDIT_VALUE:'+str((got, exact)))


def audit_decision(result, c):
    p = result['paths']
    a, b, x, y = [p[n] for n in ('candidate_base', 'candidate_stress', 'control_base', 'control_stress')]
    def bound(values):
        n = len(values)
        if n < 26: return None
        values = [D(str(v)) for v in values]
        mean = sum(values)/n
        centered = [v-mean for v in values]
        variance = sum(v*v for v in centered)/n
        for lag in range(1, 5):
            covariance = sum(centered[i]*centered[i-lag] for i in range(lag, n))/n
            variance += 2*(D(1)-D(lag)/5)*covariance
        return float(mean-D('1.6448536269514722')*(max(D(0), variance)/n).sqrt())
    if any(v['stop'] and v['stop']['decision'] == 'REJECT_REFERENCE_RISK' for v in (a, b)):
        expected = 'REJECT_REFERENCE_RISK'
    elif any(v['stop'] for v in (a, b)):
        expected = 'INSUFFICIENT_RISK_ORDER'
    elif min(a['cash_lo'], b['cash_lo']) <= c['initial_capital']:
        expected = 'REJECT_NET_EDGE'
    elif not x['complete'] or not y['complete']:
        expected = 'INSUFFICIENT_CONTROL_PATH'
    else:
        split = inp.ms(c['split_utc'])
        sub = [sum(D(str(r['net_lo'])) for r in b['daily'] if (r['entry_ms'] < split) == (k == 0)) for k in (0, 1)]
        grouped = {}
        for row in b['daily']:
            if row['group'] is not None:
                key = str(row['group']); grouped[key] = grouped.get(key, D(0))+D(str(row['net_lo']))
        residual = sum(grouped.values())-max(grouped.values(), default=D(0))
        paired = [a['cash_lo']-x['cash_hi'], b['cash_lo']-y['cash_hi']]
        blocks = []; delta = []
        for offset in range(0, len(b['daily'])//7*7, 7):
            blocks.append(sum(D(str(r['net_lo'])) for r in b['daily'][offset:offset+7]))
            delta.append(sum(D(str(u['net_lo']))-D(str(v['net_hi']))
                             for u, v in zip(b['daily'][offset:offset+7], y['daily'][offset:offset+7])))
        lows = {'stress_lower': bound(blocks), 'paired_stress_lower': bound(delta)}
        compare(result['weekly_hac'], lows)
        diagnostics = dict(subperiod_stress_lower_net=list(map(float, sub)),
            leave_largest_group_lower_net=float(residual), paired_lower_net=paired,
            complete_7_day_blocks=len(blocks))
        compare(result['diagnostics'], diagnostics)
        compare({str(k): v for k, v in result['diagnostics']['separated_group_lower_net'].items()},
                {k: float(v) for k, v in grouped.items()})
        expected = ('REJECT_PAIRED_EDGE' if min(paired) <= 0 else
                    'REJECT_SUBPERIOD_STABILITY' if min(sub) <= 0 else
                    'INSUFFICIENT_CONCENTRATION' if residual <= 0 else
                    'INSUFFICIENT_STATISTICAL_SUPPORT' if not all(v is not None and v > 0 for v in lows.values()) else
                    'RETAIN_DEVELOPMENT_ONLY_NOT_CONFIRMED')
    need(result['decision'] == expected, 'INDEPENDENT_DECISION:'+expected)
    return expected


def audit():
    # Import for archived input loading only, never call its simulation.
    import eth_queue_development_screen as screen
    c = screen.check_economic()
    data = screen.market()
    schedule = strict_json((inp.RUN/'schedule.json').read_bytes())
    result = strict_json((inp.RUN/'result.json').read_bytes())
    need(result['economic_freeze_sha256'] == sha(inp.RUN/'economic-freeze.json'), 'RESULT_FREEZE')
    reports = {}
    for name, path in result['paths'].items():
        exact = exact_path(schedule['rows'], data, c, path['multiplier'], path['control'])
        compare(path, exact)
        reports[name] = dict(cash_lo=exact['cash_lo'], cash_hi=exact['cash_hi'],
                            completed_days=exact['completed_days'], stop=exact['stop'])
    audit_decision(result, c)
    need(not result['qualification'] and not result['independent_confirmation_admitted'] and
         result['training_vectors'] == 0 and result['economic_attempts'] == 1, 'AUTHORITY')
    save(inp.RUN/'audit.json', dict(status='DECIMAL_JOURNAL_AND_RISK_MATCH', paths=reports,
        result_sha256=sha(inp.RUN/'result.json'), protected_files=inp.preserve(),
        independent_confirmation_admitted=False))
    print(json.dumps(dict(status='DECIMAL_JOURNAL_AND_RISK_MATCH', paths=reports,
                         result_decision=result['decision'], independent_confirmation_admitted=False)))


if __name__ == '__main__': audit()
