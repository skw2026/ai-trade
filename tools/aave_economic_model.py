"""Fixed reference cash/risk model; no network, optimization or trading API."""
from decimal import Decimal, ROUND_FLOOR
import math
import statistics
import time

MINUTE = 60000
Z = 1.6448536269514722


def require(value, message):
    if not value:
        raise ValueError(message)


def hac_box(intervals):
    require(len(intervals) >= 26, 'HAC_MINIMUM_WEEKS')
    require(all(math.isfinite(l) and math.isfinite(u) and l <= u for l, u in intervals), 'HAC_INTERVAL')
    n = len(intervals)
    mids = [(l+u)/2 for l, u in intervals]
    radii = [(u-l)/2 for l, u in intervals]
    mean = statistics.mean(mids)
    centered = [v-mean for v in mids]
    quadratic = math.fsum(v*v for v in centered)
    for lag in range(1, 5):
        quadratic += 2*(1-lag/5)*math.fsum(centered[i]*centered[i-lag] for i in range(lag, n))
    se = (math.sqrt(max(0, quadratic))+math.sqrt(5*math.fsum(v*v for v in radii)))/n
    return dict(mean_lo=statistics.mean(l for l, u in intervals), mean_hi=statistics.mean(u for l, u in intervals),
                standard_error_upper=se, lower=statistics.mean(l for l, u in intervals)-Z*se, weeks=n)


def size(price, c, multiplier):
    fill = Decimal(price)*(1-Decimal(str(c['slippage_bps_per_side']))*multiplier/10000)
    step = Decimal(c['quantity_step'])
    units = (Decimal(str(c['initial_capital']))*Decimal(str(c['entry_notional_fraction']))/fill/step).to_integral_value(rounding=ROUND_FLOOR)
    return -float(units*step)


def event_schedule(opportunities, data):
    rows = []
    for index, item in enumerate(opportunities):
        if item['exclusion'] is not None:
            continue
        first, entry, end = (item[k]*1000 for k in ('first', 'entry', 'exit'))
        anchor = (first-1)//MINUTE*MINUTE
        # anchor is a CLOSE boundary strictly before the event, never the
        # eventual close of the currently forming pre-event minute.
        before = float(data['trade'][anchor-MINUTE][3])
        previous = float(data['trade'][anchor-6*MINUTE][3])
        closes = [float(data['trade'][t][3]) for t in range(anchor-61*MINUTE, anchor, MINUTE)]
        returns = [math.log(v/u) for u, v in zip(closes, closes[1:])]
        rows.append(dict(group=index, first_ms=first, entry_ms=entry, exit_ms=end,
            candidate=item['candidate_opportunity'], baseline=before < previous,
            signal=item['signal'], pre_event_return=before/previous-1,
            prior_60minute_realized_volatility=math.sqrt(math.fsum(v*v for v in returns)),
            utc_hour=first//3600000 % 24))
    require(all(a['exit_ms']+5000 < b['entry_ms'] for a, b in zip(rows, rows[1:])), 'OVERLAPPING_EVENT_PATHS')
    return rows


def funding_events(data, quantity, start, end):
    result = []
    for timestamp, value in sorted(data['funding'].items()):
        if not start-5000 <= timestamp <= end+5000:
            continue
        prices = [data['mark'][t] for t in range((timestamp-5000)//MINUTE*MINUTE,
                                               (timestamp+5000)//MINUTE*MINUTE+1, MINUTE)]
        low = min(float(p[2]) for p in prices)
        high = max(float(p[1]) for p in prices)
        amounts = [-quantity*float(value)*low, -quantity*float(value)*high]
        optional = abs(timestamp-start) <= 5000 or abs(timestamp-end) <= 5000
        if optional:
            amounts.append(0.0)
        result.append(dict(time=timestamp, earliest=max(start, timestamp-5000),
            latest=min(end, timestamp+5000), lo=min(amounts), hi=max(amounts), optional=optional))
    return result


def cash_range(initial, funding, begin, end):
    lo, hi = initial
    for event in funding:
        if event['latest'] < begin:
            lo += event['lo']; hi += event['hi']
        elif event['earliest'] <= end:
            lo += min(0, event['lo']); hi += max(0, event['hi'])
    return lo, hi


def simulate(rows, data, c, multiplier, arm, deadline=math.inf):
    capital = float(c['initial_capital'])
    cash = [capital, capital]
    peak = [capital, capital]
    dd = [0.0, 0.0]
    max_notional = 0.0
    fee = c['fee_bps_per_side']*multiplier/10000
    slip = c['slippage_bps_per_side']*multiplier/10000
    totals = dict(gross=0.0, fees=0.0, slippage=0.0, funding_lo=0.0, funding_hi=0.0)
    journal = []
    stop = None
    position = entry_fill = 0.0
    fills = 0

    def risk(balance, quantity, fill, low, high, close, t, phase):
        nonlocal max_notional, stop
        lower_min = balance[0]+quantity*(high-fill)
        upper_min = balance[1]+quantity*(high-fill)
        lower_max = balance[0]+quantity*(low-fill)
        upper_max = balance[1]+quantity*(low-fill)
        upper_close = balance[1]+quantity*(close-fill)
        certain = max(0.0, 1-upper_min/peak[0], 1-upper_close/max(peak[0], lower_max))
        possible = max(0.0, 1-lower_min/max(peak[1], upper_max))
        dd[0] = max(dd[0], certain); dd[1] = max(dd[1], possible)
        peak[0] = max(peak[0], lower_max); peak[1] = max(peak[1], upper_max)
        max_notional = max(max_notional, abs(quantity)*high/capital)
        decision = ('REJECT_REFERENCE_RISK' if certain >= c['maximum_drawdown'] or max_notional > c['maximum_notional_fraction']
                    else 'INSUFFICIENT_RISK_ORDER' if possible >= c['maximum_drawdown'] else None)
        if decision:
            stop = dict(decision=decision, at_ms=t, phase=phase, equity_lo=lower_min, equity_hi=upper_max,
                        no_stop_fill_invented=True)
        return stop is not None

    for row in rows:
        require(time.monotonic() < deadline, 'ECONOMIC_COMPUTE_DEADLINE')
        before = list(cash)
        start, end = row['entry_ms'], row['exit_ms']
        active = row[arm]
        position = size(data['trade'][start][0], c, multiplier) if active else 0.0
        item = dict(group=row['group'], entry_ms=start, exit_ms=end, active=bool(position),
                    quantity=position, zero_rounded=active and position == 0)
        if not position:
            journal.append(dict(item, complete=True, net_lo=0.0, net_hi=0.0,
                                cash_lo=cash[0], cash_hi=cash[1], gross=0.0, fees=0.0,
                                slippage=0.0, funding_lo=0.0, funding_hi=0.0))
            continue
        mid = float(data['trade'][start][0])
        entry_fill = mid*(1-slip)
        entry_fee = -position*entry_fill*fee
        base = [v-entry_fee for v in before]
        fs = funding_events(data, position, start, end)
        fills += 1
        for t in range(start, end, MINUTE):
            balance = cash_range(base, fs, t, t+MINUTE-1)
            op, high, low, close = map(float, data['mark'][t])
            if risk(balance, position, entry_fill, low, high, close, t, 'held_minute'):
                cash = list(balance)
                totals['fees'] += entry_fee
                totals['funding_lo'] += balance[0]-base[0]
                totals['funding_hi'] += balance[1]-base[1]
                break
        if stop:
            journal.append(dict(item, complete=False, net_lo=None, net_hi=None, cash_lo=cash[0], cash_hi=cash[1]))
            break
        fund_lo = math.fsum(v['lo'] for v in fs)
        fund_hi = math.fsum(v['hi'] for v in fs)
        funded = [base[0]+fund_lo, base[1]+fund_hi]
        mark = float(data['mark'][end][0])
        if risk(funded, position, entry_fill, mark, mark, mark, end, 'exit_mark'):
            cash = funded
            totals['fees'] += entry_fee
            totals['funding_lo'] += fund_lo; totals['funding_hi'] += fund_hi
            journal.append(dict(item, complete=False, net_lo=None, net_hi=None, cash_lo=cash[0], cash_hi=cash[1]))
            break
        exit_mid = float(data['trade'][end][0])
        exit_fill = exit_mid*(1+slip)
        gross = position*(exit_mid-mid)
        pnl = position*(exit_fill-entry_fill)
        fees = entry_fee-position*exit_fill*fee
        cash = [before[0]+pnl-fees+fund_lo, before[1]+pnl-fees+fund_hi]
        totals['gross'] += gross; totals['fees'] += fees
        totals['slippage'] += gross-pnl
        totals['funding_lo'] += fund_lo; totals['funding_hi'] += fund_hi
        fills += 1
        position = entry_fill = 0.0
        journal.append(dict(item, complete=True, net_lo=cash[0]-before[0], net_hi=cash[1]-before[1],
            cash_lo=cash[0], cash_hi=cash[1], gross=gross, fees=fees, slippage=gross-pnl,
            funding_lo=fund_lo, funding_hi=fund_hi))
        if risk(cash, 0, 0, mark, mark, mark, end, 'exit_cash'):
            break
    require(math.isclose(cash[0], capital+totals['gross']-totals['slippage']-totals['fees']+totals['funding_lo'], abs_tol=1e-7), 'CASH_LOW_IDENTITY')
    require(math.isclose(cash[1], capital+totals['gross']-totals['slippage']-totals['fees']+totals['funding_hi'], abs_tol=1e-7), 'CASH_HIGH_IDENTITY')
    return dict(arm=arm, multiplier=multiplier, complete=stop is None, stop=stop,
        cash_lo=cash[0], cash_hi=cash[1], drawdown_lo=dd[0], drawdown_hi=dd[1],
        maximum_notional_fraction=max_notional, fills=fills, quantity=position, entry_fill=entry_fill,
        journal=journal, **totals)


def assess(paths, c):
    base, stress, baseline = (paths[k] for k in ('candidate_base', 'candidate_stress', 'baseline_stress'))
    result = dict(decision=None, economic_attempts=1, qualification=False,
                  independent_confirmation_admitted=False, learning_gain='NOT_EVALUATED', g4='NOT_ACHIEVED')
    stops = [v['stop'] for v in (base, stress) if not v['complete']]
    if stops:
        result['decision'] = 'REJECT_REFERENCE_RISK' if any(v['decision'] == 'REJECT_REFERENCE_RISK' for v in stops) else 'INSUFFICIENT_RISK_ORDER'
        return result
    if not all(v['complete'] for v in paths.values()):
        result['decision'] = 'INSUFFICIENT_CONTROL_PATH'
        return result
    halves = [math.fsum(v['net_lo'] for v in stress['journal'] if (v['entry_ms'] < c['split_ms']) == (i == 0)) for i in range(2)]
    residual = math.fsum(v['net_lo'] for v in stress['journal'])-max([0]+[v['net_lo'] for v in stress['journal']])
    paired = stress['cash_lo']-baseline['cash_hi']
    candidate_weeks = [[0.0, 0.0] for _ in range(52)]
    paired_weeks = [[0.0, 0.0] for _ in range(52)]
    for left, right in zip(stress['journal'], baseline['journal']):
        require(left['group'] == right['group'], 'PAIRED_CLOCK')
        week = (left['entry_ms']-c['evaluation_start_ms'])//604800000
        if week >= 52:
            continue
        candidate_weeks[week][0] += left['net_lo']; candidate_weeks[week][1] += left['net_hi']
        paired_weeks[week][0] += left['net_lo']-right['net_hi']; paired_weeks[week][1] += left['net_hi']-right['net_lo']
    hac = dict(candidate_stress=hac_box(candidate_weeks), paired_stress=hac_box(paired_weeks))
    result.update(half_year_stress_lower=halves, leave_largest_group_stress_lower=residual,
        paired_stress_lower=paired, weekly_hac=hac, candidate_week_intervals=candidate_weeks,
        paired_week_intervals=paired_weeks,
        base_return_interval=[base[k]/c['initial_capital']-1 for k in ('cash_lo', 'cash_hi')],
        stress_return_interval=[stress[k]/c['initial_capital']-1 for k in ('cash_lo', 'cash_hi')])
    result['decision'] = ('REJECT_NET_EDGE' if min(base['cash_lo'], stress['cash_lo']) <= c['initial_capital'] else
        'REJECT_PAIRED_EDGE' if paired <= 0 else 'REJECT_SUBPERIOD_STABILITY' if min(halves) <= 0 else
        'INSUFFICIENT_CONCENTRATION' if residual <= 0 else
        'INSUFFICIENT_STATISTICAL_SUPPORT' if min(v['lower'] for v in hac.values()) <= 0 else
        'RETAIN_DEVELOPMENT_ONLY_NOT_CONFIRMED')
    return result
