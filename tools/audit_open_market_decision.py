"""Independent fixed selection/verdict audit; no model or simulation imports.

Ledger values must separately pass the Decimal calendar audit. This layer checks
the complete comparison set and frozen decision rules, including diagnostic-only
selection, partial-cycle concentration and the unchanged qualification locks.
"""
from decimal import Decimal as D

from audit_open_market_execution import need, number, same

STEP = 300000
ARMS = ('P', 'PV', 'STALE')
RIDGES = (D('.1'), D(1), D(10))
PATHS = {period + '-' + arm for period in ('2021', '2025') for arm in ARMS} | {'2025-PV-delay'}


def complete(path):
    need(isinstance(path['books'], list) and len(path['books']) == 2, 'DECISION_PAIRED_BOOKS')
    return all(b['insufficient'] is None and number(b['quantity']) == 0 for b in path['books'])


def full_calendar(path):
    need(type(path['start']) is int and type(path['end']) is int and
         path['end'] > path['start'] and (path['end'] - path['start']) % STEP == 0,
         'DECISION_DOMAIN')
    return type(path['bars']) is int and path['bars'] == (path['end'] - path['start']) // STEP


def eligible(path):
    return complete(path) and all(b['stopped'] is None for b in path['books'])


def audit_selection(comparisons, selection):
    need(len(comparisons) == 3 and {number(r['ridge']) for r in comparisons} == set(RIDGES),
         'SELECTION_COMPLETE_RIDGES')
    identities = {(r['start'], r['end'], r['latency']) for r in comparisons}
    need(len(identities) == 1, 'SELECTION_MATCHED_CALENDAR')
    need(all(full_calendar(r) for r in comparisons), 'SELECTION_FULL_CALENDAR')
    allowed = [r for r in comparisons if eligible(r)]
    pool = allowed if allowed else comparisons
    chosen = max(pool, key=lambda r: (number(r['books'][1]['net']), number(r['ridge'])))
    expected = dict(ridge=number(chosen['ridge']), eligible=bool(allowed),
                    status='SELECTED' if allowed else 'DIAGNOSTIC_ONLY_NO_ELIGIBLE_PATH')
    same(expected, selection, 'SELECTION')
    return dict(status='INDEPENDENT_SELECTION_VERIFIED', compared=3,
                eligible_paths=len(allowed), ridge=str(number(chosen['ridge'])))


def audit_verdict(paths, selection, decision):
    need(set(paths) == PATHS, 'VERDICT_COMPLETE_PATH_SET')
    need(set(selection) == set(ARMS), 'VERDICT_COMPLETE_SELECTION_SET')
    for arm in ARMS:
        s = selection[arm]
        need(set(s) == {'ridge', 'eligible', 'status'} and type(s['eligible']) is bool and
             number(s['ridge']) in RIDGES and
             s['status'] == ('SELECTED' if s['eligible'] else 'DIAGNOSTIC_ONLY_NO_ELIGIBLE_PATH'),
             'VERDICT_SELECTION_IDENTITY')
    for period in ('2021', '2025'):
        group = [paths[period + '-' + arm] for arm in ARMS]
        need(len({(p['start'], p['end'], p['latency']) for p in group}) == 1 and
             group[0]['latency'] == 1, 'VERDICT_MATCHED_ARM_CALENDAR')
    pv, delayed = paths['2025-PV'], paths['2025-PV-delay']
    need((pv['start'], pv['end']) == (delayed['start'], delayed['end']) and
         delayed['latency'] == 2, 'VERDICT_DELAY_IDENTITY')
    locks = dict(independent_confirmation=False, qualified_candidates=0, g4=False,
                 online_learning_proven=False, demo_opening_lock_unchanged=True)
    if not all(complete(p) and full_calendar(p) for p in paths.values()):
        expected = dict(status='COMPARISON_EVIDENCE_INCOMPLETE',
                        reasons=['INCOMPLETE_CONTINUOUS_CALENDAR'], **locks)
        same(expected, decision, 'VERDICT')
        return dict(status='INDEPENDENT_VERDICT_VERIFIED', paths=7,
                    decision_status=expected['status'])
    reasons = []
    for period in ('2021', '2025'):
        candidate = paths[period + '-PV']
        if not eligible(candidate):
            reasons.append(period + '_RISK_OR_EXECUTION')
        if any(number(b['net']) <= 0 for b in candidate['books']):
            reasons.append(period + '_NET_NONPOSITIVE')
        for arm in ('P', 'STALE'):
            if number(candidate['books'][1]['net']) <= number(paths[period + '-' + arm]['books'][1]['net']):
                reasons.append(period + '_NO_INCREMENT_' + arm)
    if not selection['PV']['eligible']:
        reasons.append('SELECTION_RISK')
    central = not reasons
    stress = pv['books'][1]
    if number(stress['net']) < 100:
        reasons.append('MINIMUM_ABSOLUTE_EFFECT')
    for arm in ('P', 'STALE'):
        if number(stress['net']) - number(paths['2025-' + arm]['books'][1]['net']) < 25:
            reasons.append('MINIMUM_INCREMENT_' + arm)
    need(all(type(c['complete']) is bool for c in stress['cycles']), 'CYCLE_COMPLETENESS_TYPE')
    capacity = sum(c['complete'] for c in stress['cycles']) >= 30 and len(stress['active_weeks']) >= 12
    if not capacity:
        reasons.append('EVENT_CAPACITY')
    # Incomplete/partial cycles still contribute money and can dominate the total.
    best_cycle = max([number(c['net']) for c in stress['cycles']] + [D(0)])
    if number(stress['net']) - best_cycle <= 0:
        reasons.append('BEST_CYCLE_CONCENTRATION')
    midpoint = 1751328000000
    previous, first, second = D(10000), D(0), D(0)
    for day in stress['days']:
        equity = number(day['equity'])
        if day['t'] <= midpoint:
            first += equity - previous
        else:
            second += equity - previous
        previous = equity
    if min(first, second) <= 0:
        reasons.append('HALF_YEAR_SIGN')
    if not eligible(delayed) or number(delayed['books'][1]['net']) <= 0:
        reasons.append('DELAY_SENSITIVITY')
    status = ('DEVELOPMENT_SUPPORTED' if not reasons else
              'INSUFFICIENT_EVENT_CAPACITY' if central and not capacity else
              'FRAGILE_DEVELOPMENT_SIGNAL' if central else 'NET_INCREMENT_UNSUPPORTED')
    expected = dict(status=status, reasons=reasons, half_year_pressure_net=[first, second], **locks)
    same(expected, decision, 'VERDICT')
    return dict(status='INDEPENDENT_VERDICT_VERIFIED', paths=7, decision_status=status,
                complete_cycles=sum(c['complete'] for c in stress['cycles']),
                active_weeks=len(stress['active_weeks']), half_year_pressure_net=[str(first), str(second)])
