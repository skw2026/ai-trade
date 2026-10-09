"""One fixed development screen. Research cash/mark bounds, never an execution adapter."""
import argparse
from decimal import Decimal, ROUND_FLOOR
import json
import math
import time

import eth_queue_development_inputs as inp
from mvp_reference_inputs import strict_json
from run_bounded_learning import need, now, save, sha, hac_lower

SOURCES = ('tools/eth_queue_development_screen.py', 'tools/test_eth_queue_development_screen.py',
           'tools/audit_eth_queue_development.py', 'tools/audit_eth_queue_development_inputs.py')


def market():
    f = strict_json((inp.RUN/'freeze.json').read_bytes())
    need((inp.RUN/'market-collection.json').exists(), 'MARKET_NOT_COMPLETE')
    result = {kind: {} for kind in ('trade', 'mark', 'funding')}
    for req in f['market_requests']:
        rows = inp.load(req)
        for row in rows:
            funding = req['kind'] == 'funding'
            t = int(row['fundingRateTimestamp'] if funding else row[0])
            target = result[req['kind']]
            need(t not in target, 'DUPLICATE_INPUT_TIME')
            target[t] = row['fundingRate'] if funding else row[1:5]
    c = strict_json(inp.CONTRACT.read_bytes())
    start, end = inp.ms(c['decision_start_utc']), inp.ms(c['terminal_utc'])+inp.HOUR
    for kind in result:
        step = 8*inp.HOUR if kind == 'funding' else inp.HOUR
        need(sorted(result[kind]) == list(range((start+step-1)//step*step, end, step)), 'COMPLETE_MARKET_GRID')
    return result


def quantity(mid, c, multiplier):
    slip = Decimal(str(c['slippage_bps_per_side']))*multiplier/10000
    fill = Decimal(str(mid))*(1-slip)
    step = Decimal(c['quantity_step'])
    units = (Decimal(str(c['initial_capital']))*Decimal(str(c['entry_notional_fraction']))/fill/step).to_integral_value(rounding=ROUND_FLOOR)
    return -float(units*step)


def simulate(rows, data, c, multiplier, control=False, deadline=math.inf):
    capital = float(c['initial_capital'])
    cash_lo = cash_hi = peak_lo = peak_hi = capital
    qty = entry = 0.0
    fees = slip_cost = gross = fund_lo = fund_hi = 0.0
    dd_lo = dd_hi = max_notional = 0.0
    fee = c['fee_bps_per_side']*multiplier/10000
    slip = c['slippage_bps_per_side']*multiplier/10000
    daily = []
    stopped = None
    completed = fills = active_days = 0
    last_at = rows[0]['entry_ms']
    mark_bounds = (capital, capital)

    def observe(t, phase, low_price, high_price, close_price):
        nonlocal peak_lo, peak_hi, dd_lo, dd_hi, max_notional, stopped, mark_bounds, last_at
        # q <= 0: higher price is the adverse mark.
        min_lo, min_hi = cash_lo+qty*(high_price-entry), cash_hi+qty*(high_price-entry)
        max_lo, max_hi = cash_lo+qty*(low_price-entry), cash_hi+qty*(low_price-entry)
        close_hi = cash_hi+qty*(close_price-entry)
        lower = max(0.0, 1-min_hi/peak_lo, 1-close_hi/max(peak_lo, max_lo))
        upper = max(0.0, 1-min_lo/max(peak_hi, max_hi))
        dd_lo, dd_hi = max(dd_lo, lower), max(dd_hi, upper)
        max_notional = max(max_notional, abs(qty)*high_price/capital)
        mark_bounds = (min_lo, max_hi); last_at = t
        if max_notional > c['maximum_notional_fraction'] or lower >= c['maximum_drawdown']:
            verdict = 'REJECT_REFERENCE_RISK'
        elif upper >= c['maximum_drawdown']:
            verdict = 'INSUFFICIENT_RISK_ORDER'
        else:
            verdict = None
        peak_lo, peak_hi = max(peak_lo, max_lo), max(peak_hi, max_hi)
        if verdict:
            stopped = dict(decision=verdict, at_ms=t, phase=phase, equity_low=min_lo, equity_high=max_hi,
                           close_equity_high=close_hi, no_stop_fill_invented=True)
        return stopped is not None

    for row in rows:
        need(time.monotonic() < deadline, 'COMPUTE_TIMEOUT')
        start, end = row['entry_ms'], row['exit_ms']
        before_lo, before_hi = cash_lo, cash_hi
        before_components = (gross, fees, slip_cost, fund_lo, fund_hi)
        active = control or row['active']
        mid = float(data['trade'][start][0])
        if active:
            qty = quantity(data['trade'][start][0], c, multiplier)
            need(qty < 0, 'ZERO_SIZED_POSITION')
            entry = mid*(1-slip)
            cost = abs(qty)*entry*fee
            cash_lo -= cost; cash_hi -= cost; fees += cost
            fills += 1; active_days += 1
        for t in range(start, end, inp.HOUR):
            op, high, low, close = map(float, data['mark'][t])
            if observe(t, 'open', op, op, op): break
            if t in data['funding'] and qty:
                rate = float(data['funding'][t])
                a, b = -qty*rate*low, -qty*rate*high
                lower, upper = min(a, b), max(a, b)
                cash_lo += lower; cash_hi += upper
                fund_lo += lower; fund_hi += upper
                if observe(t, 'funding', op, op, op): break
            if observe(t, 'ohlc', low, high, close): break
        if stopped: break
        exit_mid = float(data['trade'][end][0])
        exit_mark = float(data['mark'][end][0])
        if observe(end, 'exit_mark', exit_mark, exit_mark, exit_mark): break
        if active:
            exit_fill = exit_mid*(1+slip)
            realised = qty*(exit_fill-entry)
            raw_gross = qty*(exit_mid-mid)
            cost = abs(qty)*exit_fill*fee
            cash_lo += realised-cost; cash_hi += realised-cost
            gross += raw_gross; fees += cost; slip_cost += raw_gross-realised
            fills += 1
            qty = entry = 0.0
        if observe(end, 'exit', exit_mark, exit_mark, exit_mark): break
        daily.append(dict(entry_ms=start, exit_ms=end, active=active, group=row['group'],
            net_lo=cash_lo-before_lo, net_hi=cash_hi-before_hi, cash_lo=cash_lo, cash_hi=cash_hi,
            gross=gross-before_components[0], fees=fees-before_components[1],
            slippage=slip_cost-before_components[2], funding_lo=fund_lo-before_components[3],
            funding_hi=fund_hi-before_components[4]))
        completed += 1
    # Realised cash identity also holds when an open position remains at a risk stop.
    need(math.isclose(cash_lo, capital+gross-slip_cost-fees+fund_lo, abs_tol=1e-7), 'LOW_CASH_IDENTITY')
    need(math.isclose(cash_hi, capital+gross-slip_cost-fees+fund_hi, abs_tol=1e-7), 'HIGH_CASH_IDENTITY')
    return dict(multiplier=multiplier, control=control, complete=stopped is None, stop=stopped,
        completed_days=completed, active_days=active_days, fills=fills, last_at_ms=last_at,
        cash_lo=cash_lo, cash_hi=cash_hi, gross=gross, fees=fees, slippage=slip_cost,
        funding_lo=fund_lo, funding_hi=fund_hi, qty=qty, entry_fill=entry,
        drawdown_lo=dd_lo, drawdown_hi=dd_hi, maximum_notional_fraction=max_notional,
        marked_equity_lo=mark_bounds[0], marked_equity_hi=mark_bounds[1], daily=daily,
        full_window_return_lo=(cash_lo/capital-1) if stopped is None else None,
        full_window_return_hi=(cash_hi/capital-1) if stopped is None else None)


def assess(paths, c):
    base, stress, cb, cs = (paths[n] for n in ('candidate_base', 'candidate_stress', 'control_base', 'control_stress'))
    result = dict(decision=None, qualification=False, independent_confirmation_admitted=False,
                  training_vectors=0, economic_attempts=1, weekly_hac=None, diagnostics=None)
    stopped = [p for p in (base, stress) if not p['complete']]
    if stopped:
        result['decision'] = 'REJECT_REFERENCE_RISK' if any(p['stop']['decision'] == 'REJECT_REFERENCE_RISK' for p in stopped) else 'INSUFFICIENT_RISK_ORDER'
        return result
    if min(base['cash_lo'], stress['cash_lo']) <= c['initial_capital']:
        result['decision'] = 'REJECT_NET_EDGE'
        return result
    if not cb['complete'] or not cs['complete']:
        result['decision'] = 'INSUFFICIENT_CONTROL_PATH'
        return result
    split = inp.ms(c['split_utc'])
    subperiod = [sum(r['net_lo'] for r in stress['daily'] if (r['entry_ms'] < split) == (k == 0)) for k in (0, 1)]
    groups = {}
    for r in stress['daily']:
        if r['group'] is not None: groups[r['group']] = groups.get(r['group'], 0.0)+r['net_lo']
    residual = sum(groups.values())-max(groups.values(), default=0)
    paired = [base['cash_lo']-cb['cash_hi'], stress['cash_lo']-cs['cash_hi']]
    blocks, uplift = [], []
    for i in range(0, len(stress['daily'])-6, 7):
        blocks.append(sum(r['net_lo'] for r in stress['daily'][i:i+7]))
        uplift.append(sum(a['net_lo']-b['net_hi'] for a, b in zip(stress['daily'][i:i+7], cs['daily'][i:i+7])))
    result['diagnostics'] = dict(subperiod_stress_lower_net=subperiod, separated_group_lower_net=groups,
        leave_largest_group_lower_net=residual, paired_lower_net=paired, complete_7_day_blocks=len(blocks))
    result['weekly_hac'] = dict(stress_lower=hac_lower(blocks), paired_stress_lower=hac_lower(uplift))
    if min(paired) <= 0: result['decision'] = 'REJECT_PAIRED_EDGE'
    elif min(subperiod) <= 0: result['decision'] = 'REJECT_SUBPERIOD_STABILITY'
    elif residual <= 0: result['decision'] = 'INSUFFICIENT_CONCENTRATION'
    elif not all(x is not None and x > 0 for x in result['weekly_hac'].values()): result['decision'] = 'INSUFFICIENT_STATISTICAL_SUPPORT'
    else: result['decision'] = 'RETAIN_DEVELOPMENT_ONLY_NOT_CONFIRMED'
    return result


def freeze():
    inp.guard(); inp.preserve()
    schedule = strict_json((inp.RUN/'schedule.json').read_bytes())
    need(schedule['capacity_admitted'], 'CAPACITY_REQUIRED')
    data = market()
    save(inp.RUN/'economic-freeze.json', dict(created_utc=now(), contract_sha256=sha(inp.CONTRACT),
        input_freeze_sha256=sha(inp.RUN/'freeze.json'), schedule_sha256=sha(inp.RUN/'schedule.json'),
        sources={p: sha(inp.ROOT/p) for p in SOURCES}, market_rows={k: len(v) for k, v in data.items()},
        pages={p.name: sha(p) for p in sorted((inp.RUN/'pages').glob('*.json'))},
        economic_attempts_maximum=1, independent_confirmation_admitted=False))
    print(json.dumps(dict(scope='ECONOMIC_RULE_FROZEN', market_rows={k: len(v) for k, v in data.items()})))


def check_economic():
    c = inp.guard(); inp.preserve()
    f = strict_json((inp.RUN/'economic-freeze.json').read_bytes())
    need(f['contract_sha256'] == sha(inp.CONTRACT) and f['schedule_sha256'] == sha(inp.RUN/'schedule.json') and
         f['input_freeze_sha256'] == sha(inp.RUN/'freeze.json'), 'ECONOMIC_INPUT_IDENTITY')
    need(all(sha(inp.ROOT/p) == h for p, h in f['sources'].items()), 'ECONOMIC_SOURCE_CHANGED')
    need(all(sha(inp.RUN/'pages'/p) == h for p, h in f['pages'].items()), 'PAGE_RECEIPT_CHANGED')
    return c


def screen():
    c = check_economic()
    save(inp.RUN/'economic-attempt.json', dict(started_utc=now(), maximum_attempts=1,
        freeze_sha256=sha(inp.RUN/'economic-freeze.json'), independent_confirmation_admitted=False))
    data = market()
    schedule = strict_json((inp.RUN/'schedule.json').read_bytes())
    started = time.monotonic(); paths = {}
    for control in (False, True):
        for multiplier in (1, 2):
            name = ('control_' if control else 'candidate_')+('base' if multiplier == 1 else 'stress')
            paths[name] = simulate(schedule['rows'], data, c, multiplier, control,
                                   started+c['maximum_compute_seconds'])
    result = dict(**assess(paths, c), paths=paths, elapsed_seconds=time.monotonic()-started,
                  economic_freeze_sha256=sha(inp.RUN/'economic-freeze.json'), finished_utc=now())
    save(inp.RUN/'result.json', result)
    print(json.dumps({k: v for k, v in result.items() if k != 'paths'}))
    for name, path in paths.items():
        print(json.dumps(dict(path=name, **{k: v for k, v in path.items() if k != 'daily'})))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'screen'))
    args = parser.parse_args()
    freeze() if args.command == 'freeze' else screen()
