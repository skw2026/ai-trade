"""Predeclared development verdict, never G4 or trading permission."""
from decimal import Decimal
import math

from cross_asset_relative_strength import INITIAL, HALF, LAST, END, WEEK, MINUTE
from eth_sol_complete_model import long_variance

D = lambda x: Decimal(str(x))


def metrics(snap):
    cash = {r[0]: r[1:] for r in snap['daily_cash']}
    if any(t not in cash or any(v is None for v in cash[t])
           for t in [HALF+i*WEEK for i in range(27)]+[END]):
        return None
    result = []
    for i, b in enumerate(snap['books']):
        weekly = [D(cash[HALF+(j+1)*WEEK][i])-D(cash[HALF+j*WEEK][i]) for j in range(26)]
        net = D(cash[END][i])-D(cash[HALF][i])
        positive = sum(max(D(0), v) for v in weekly)
        days = (END-INITIAL)//MINUTE
        mean = math.fsum(map(float, weekly))/26
        var = math.fsum((float(v)-mean)**2 for v in weekly)/25
        sharpe = {}
        if not b['stopped'] and not b['insufficient']:
            sharpe['naive'] = mean/math.sqrt(var)*math.sqrt(52) if var > 0 else None
            for lag in (1, 2, 4):
                v = long_variance(list(map(float, weekly)), lag)
                sharpe['hac'+str(lag)] = mean/math.sqrt(v)*math.sqrt(52) if v else None
        completed = sum(c['completed'] and HALF <= snap['start']+c['start']*MINUTE < LAST for c in b['cycles'])
        # FIRST->HALF is 25 weeks, so week IDs 25..50 are H2.
        active = sum(25 <= w < 51 for w in b['active_weeks'])
        result.append(dict(full_net=str(D(b['cash'])-10000), h2_net=str(net),
            weekly=list(map(str, weekly)), half13_net=[str(sum(weekly[:13])), str(sum(weekly[13:]))],
            tail=str(D(cash[END][i])-D(cash[LAST][i])),
            completed_cycles=completed, active_weeks=active,
            largest_positive_share=str(max(weekly)/positive) if positive else None,
            without_best_week=str(net-max(weekly)),
            gross=str(b['gross']), fees=str(b['fees']), slippage=str(b['slippage']),
            signed_funding_cost=str(b['funding']), turnover=str(b['turnover']),
            maximum_drawdown=b['dd'], stopped=b['stopped'], insufficient=b['insufficient'],
            failed_entries=b['failed_entries'], sharpe_development_only=sharpe,
            avg_gross_exposure=b['gross_integral']/days, avg_net_exposure=b['net_integral']/days,
            avg_btc_signed_exposure=b['btc_integral']/days, max_gross=b['max_gross'],
            max_abs_net=b['max_abs_net']))
    return result


def candidate(normal, delay):
    failures = []
    books = normal['books']+delay['books']
    if (any(s['start']+s['minute']*MINUTE != END for s in (normal, delay)) or
        any(b['insufficient'] or any(map(D, b['q'])) or b['queue'] for b in books)):
        failures.append('INPUT_OR_EXECUTION_INSUFFICIENT')
    if any(b['stopped'] for b in books):
        failures.append('NO_GO_RISK')
    main, latency = metrics(normal), metrics(delay)
    if main is None or latency is None:
        if 'INPUT_OR_EXECUTION_INSUFFICIENT' not in failures:
            failures.append('INPUT_OR_EXECUTION_INSUFFICIENT')
    else:
        if not (all(D(b['full_net']) > 0 for b in main) and
                D(main[0]['h2_net']) > 0 and D(main[1]['h2_net']) >= 100):
            failures.append('ABSOLUTE_NET_HURDLE')
        if any(b['completed_cycles'] < 20 or b['active_weeks'] < 20 for b in main):
            failures.append('INSUFFICIENT_DIVERSITY')
        pressure = main[1]
        if (any(D(x) <= 0 for x in pressure['half13_net']) or
            pressure['largest_positive_share'] is None or D(pressure['largest_positive_share']) > D('.35') or
            D(pressure['without_best_week']) <= 0):
            failures.append('CONCENTRATION_OR_HALF_STABILITY')
        if not all(D(b['full_net']) > 0 and D(b['h2_net']) > 0 for b in latency):
            failures.append('LATENCY_NET_HURDLE')
    return dict(passed=not failures, failures=failures, normal=main, delay=latency)


def decide(paths, selected):
    if selected is None:
        reason = 'NO_GO_RISK' if all(any(b['stopped'] for b in s['books'])
                                    for name, s in paths.items() if name.startswith('prefix-')) else 'INPUT_OR_EXECUTION_INSUFFICIENT'
        return dict(status=reason, selected_at_half=None, candidate=None, checks={},
                    failures=['ALL_SELECTION_PATHS_INELIGIBLE'], learning_claim=False, g4=False)
    checked = {str(h): candidate(paths[f'normal-{h}'], paths[f'delay-{h}']) for h in sorted({7, selected})}
    chosen = None; claim = False; increments = None
    if selected != 7:
        learned, base = checked[str(selected)]['normal'], checked['7']['normal']
        if learned is not None and base is not None:
            increments = [str(D(x['h2_net'])-D(y['h2_net'])) for x, y in zip(learned, base)]
        if checked[str(selected)]['passed'] and increments is not None and all(D(x) >= 25 for x in increments):
            chosen = selected; claim = True
        elif increments is None or not all(D(x) >= 25 for x in increments):
            checked[str(selected)]['absolute_passed'] = checked[str(selected)]['passed']
            checked[str(selected)]['passed'] = False
            checked[str(selected)]['failures'].append('LEARNING_INCREMENT_HURDLE')
    if chosen is None and checked['7']['passed']:
        chosen = 7
    all_failures = sorted({f for c in checked.values() for f in c['failures']})
    if chosen is not None:
        status = 'LEARNED_DEVELOPMENT_SUPPORTED' if claim else 'FIXED_DEVELOPMENT_SUPPORTED'
    elif 'INPUT_OR_EXECUTION_INSUFFICIENT' in all_failures:
        status = 'INPUT_OR_EXECUTION_INSUFFICIENT'
    elif 'NO_GO_RISK' in all_failures:
        status = 'NO_GO_RISK'
    elif 'INSUFFICIENT_DIVERSITY' in all_failures:
        status = 'INSUFFICIENT_DIVERSITY'
    else:
        status = 'NO_GO_ECONOMIC'
    return dict(status=status, selected_at_half=selected, candidate=chosen, checks=checked,
                increments=increments, failures=all_failures, learning_claim=claim, g4=False)


def confirmation_planning(decision):
    if decision['candidate'] is None:
        return dict(status='NOT_JUSTIFIED_BY_DEVELOPMENT', scheduled=False)
    chosen = decision['checks'][str(decision['candidate'])]['normal']
    series = {'absolute': [float(v) for v in chosen[1]['weekly']]}
    if decision['learning_claim']:
        baseline = decision['checks']['7']['normal']
        series['increment'] = [float(D(a)-D(b)) for a, b in zip(chosen[1]['weekly'], baseline[1]['weekly'])]
    rows = []
    for name, values in series.items():
        effect = (100 if name == 'absolute' else 25)/26
        for lag in (1, 2, 4):
            variance = long_variance(values, lag)
            for scale in (1., 1.5, 2.):
                rows.append(dict(effect=name, min_effect_usdt_week=effect, hac_lag=lag, sd_multiplier=scale,
                    weeks=math.ceil((1.645+.842)**2*variance*scale**2/effect**2) if variance else None))
    return dict(status='PLANNING_ONLY_NO_CONFIRMATION_AUTHORITY', scheduled=False, scenarios=rows,
                warning='26 development weeks cannot certify stable long-run variance; fixed endpoint and budget still required')
