"""Independent rolling-governance and development-verdict audits.

No learner, rolling simulator, optimizer, network or file access. Execution is
first checked by the unchanged Decimal ledger; governance is then reconstructed
from the verified event clocks, not from the reported active-model history.
"""
from decimal import Decimal as D

import audit_open_market_execution as ledger
import audit_open_market_learning as learning_audit

DAY, STEP = 86400000, 300000
need, same, number = ledger.need, ledger.same, ledger.number


def audit_governance(result, rows, fs, schedule):
    """Verify causal, permanent update freezes and the entire prediction grid."""
    need(set(result) == {'rolling', 'path', 'predictions', 'governance'}, 'ROLLING_SCHEMA')
    rolling = result['rolling']
    need(type(rolling) is bool, 'ROLLING_MODE')
    path, predictions = result['path'], result['predictions']
    start, end = path['start'], path['end']
    need(type(start) is int and type(end) is int and start % DAY == 0 and
         end % DAY == 0 and start < end, 'ROLLING_PERIOD')
    dates = list(range(start, end, DAY))
    need(isinstance(schedule, list) and bool(schedule), 'ROLLING_SCHEDULE')
    need(all(set(item) == {'t', 'model_id', 'model'} for item in schedule),
         'ROLLING_SCHEDULE_SCHEMA')
    clocks = [item['t'] for item in schedule]
    identities = [item['model_id'] for item in schedule]
    need(all(type(t) is int and t % DAY == 0 and start <= t < end for t in clocks)
         and clocks == sorted(set(clocks)) and clocks[0] == start, 'ROLLING_SCHEDULE_CLOCKS')
    need(all(isinstance(identity, str) and bool(identity) for identity in identities)
         and len(set(identities)) == len(identities), 'ROLLING_MODEL_IDENTITIES')
    for item in schedule:
        model = item['model']
        need(model['arm'] == 'PV' and number(model['ridge']) == D('.1'),
             'ROLLING_MODEL_SCOPE')
        need(type(model['label_cutoff']) is int and model['label_cutoff'] <= item['t'],
             'ROLLING_MODEL_MATURITY')
    need(isinstance(predictions, list) and
         [p['t'] for p in predictions] == dates and
         all(set(p) == {'t', 'model_id', 'predicted_net_fraction', 'direction'}
             for p in predictions), 'ROLLING_PREDICTION_GRID')
    need(all(t in fs for t in dates), 'ROLLING_FEATURE_GRID')
    decisions = {p['t']: p['direction'] for p in predictions}
    receipt = ledger.audit(path, rows, fs, decisions)

    # stop/alert timestamps are preceding-bar CLOSE clocks; insufficient events
    # are emitted during current-bar execution, after that day's model decision.
    events = [event for book in path['books'] for event in book['events']]
    alerts = [event['t'] for event in events if event['kind'] == 'performance_alert']
    stops = [event['t'] for event in events if event['kind'] == 'stop']
    insufficient = [event['t'] for event in events if event['kind'] == 'insufficient']
    freeze_time = min(alerts) if alerts else None
    schedule_at = {item['t']: item for item in schedule}
    active = schedule[0]
    applied = [dict(t=start, model_id=active['model_id'], reason='INITIAL')]
    blocked, history = [], []
    groups = {item['model_id']: {'model': item['model'], 'features': {}, 'predictions': []}
              for item in schedule}
    for t, prediction in zip(dates, predictions):
        if t != start and t in schedule_at:
            item = schedule_at[t]
            reason = ('STATIC_REFERENCE' if not rolling else
                      'EXECUTION_INSUFFICIENT' if any(k < t for k in insufficient) else
                      'HARD_STOP' if any(k <= t for k in stops) else
                      'PERFORMANCE_FREEZE' if any(k <= t for k in alerts) else None)
            if reason is None:
                active = item
                applied.append(dict(t=t, model_id=item['model_id'], reason='SCHEDULED'))
            else:
                blocked.append(dict(t=t, model_id=item['model_id'], reason=reason))
        need(prediction['model_id'] == active['model_id'], 'ROLLING_ACTIVE_PREDICTION_MODEL')
        history.append(dict(t=t, model_id=active['model_id']))
        group = groups[active['model_id']]
        group['features'][t] = fs[t]
        group['predictions'].append({k: v for k, v in prediction.items() if k != 'model_id'})
    for group in groups.values():
        if group['predictions']:
            learning_audit.audit_predictions(group['model'], group['features'], group['predictions'])
    expected = dict(scheduled_updates=[dict(t=item['t'], model_id=item['model_id'])
                                       for item in schedule[1:]],
                    applied_updates=applied, blocked_updates=blocked,
                    active_model_by_date=history, performance_freeze_time=freeze_time)
    same(expected, result['governance'], 'ROLLING_GOVERNANCE')
    return dict(status='INDEPENDENT_ROLLING_GOVERNANCE_VERIFIED',
                prediction_dates=len(predictions), scheduled_updates=len(schedule)-1,
                actual_postinitial_updates=len(applied)-1, blocked_updates=len(blocked),
                performance_freeze_time=freeze_time, decimal_ledger=receipt)


def audit_verdict(results, decision):
    """Verify the four-path endpoint after ledger/governance audits have passed.

    Absolute economics, relative gain and identifiable update opportunities are
    separate claims. Relative improvement cannot turn a losing path into support.
    """
    need(set(results) == {'old2021', 'recent2025', 'rolling', 'delay'},
         'ROLLING_VERDICT_COMPARISON_SET')
    paths = {name: result['path'] for name, result in results.items()}

    def complete(path):
        need(isinstance(path['books'], list) and len(path['books']) == 2,
             'ROLLING_VERDICT_PAIRED_BOOKS')
        return all(book['insufficient'] is None and number(book['quantity']) == 0
                   for book in path['books'])

    def eligible(path):
        return complete(path) and all(book['stopped'] is None for book in path['books'])

    def calendar(path):
        need(type(path['start']) is int and type(path['end']) is int and
             path['start'] % DAY == 0 and path['end'] % DAY == 0 and
             path['end'] > path['start'], 'ROLLING_VERDICT_PERIOD')
        return type(path['bars']) is int and path['bars'] == (path['end']-path['start'])//STEP

    locks = dict(independent_confirmation=False, qualified_candidates=0, g4=False,
                 online_learning_proven=False, demo_opening_lock_unchanged=True)
    need(all(path['latency'] == (2 if name == 'delay' else 1)
             for name, path in paths.items()), 'ROLLING_VERDICT_LATENCY_IDENTITY')
    full = all(complete(path) and calendar(path) for path in paths.values())
    full = full and len({(path['start'], path['end']) for path in paths.values()}) == 1
    if not full:
        expected = dict(status='COMPARISON_EVIDENCE_INCOMPLETE', economic_status='UNKNOWN',
                        adaptation_status='NOT_EVALUATED', reasons=['INCOMPLETE_COMPARISON'], **locks)
        same(expected, decision, 'ROLLING_VERDICT')
        return dict(status='INDEPENDENT_ROLLING_VERDICT_VERIFIED',
                    decision_status=expected['status'], paths=4)
    candidate, recent = paths['rolling'], paths['recent2025']
    economic = eligible(candidate) and all(number(book['net']) > 0 for book in candidate['books'])
    recent_economic = eligible(recent) and all(number(book['net']) > 0 for book in recent['books'])
    stress = candidate['books'][1]
    applied_rows = results['rolling']['governance']['applied_updates']
    need(isinstance(applied_rows, list) and bool(applied_rows) and
         applied_rows[0]['reason'] == 'INITIAL' and
         all(row['reason'] == 'SCHEDULED' for row in applied_rows[1:]),
         'ROLLING_VERDICT_UPDATE_REASONS')
    need([row['t'] for row in applied_rows] == sorted({row['t'] for row in applied_rows}) and
         len({row['model_id'] for row in applied_rows}) == len(applied_rows),
         'ROLLING_VERDICT_UPDATE_IDENTITIES')
    applied = len(applied_rows)-1
    increments = {name: number(stress['net'])-number(paths[name]['books'][1]['net'])
                  for name in ('old2021', 'recent2025')}
    reasons = []
    if not eligible(candidate):
        reasons.append('ROLLING_RISK_OR_EXECUTION')
    if any(number(book['net']) <= 0 for book in candidate['books']):
        reasons.append('ROLLING_NET_NONPOSITIVE')
    if number(stress['net']) < 100:
        reasons.append('MINIMUM_ABSOLUTE_EFFECT')
    for name in ('old2021', 'recent2025'):
        if increments[name] < 25:
            reasons.append('MINIMUM_INCREMENT_'+name)
    need(all(type(cycle['complete']) is bool for cycle in stress['cycles']),
         'ROLLING_VERDICT_CYCLE_TYPE')
    cycle_count = sum(cycle['complete'] for cycle in stress['cycles'])
    if cycle_count < 30 or len(stress['active_weeks']) < 12:
        reasons.append('EVENT_CAPACITY')
    best_cycle = max([number(cycle['net']) for cycle in stress['cycles']]+[D(0)])
    if number(stress['net'])-best_cycle <= 0:
        reasons.append('BEST_CYCLE_CONCENTRATION')
    # 2025-10-01 00:00 UTC: changes ending at the boundary belong to the first part.
    boundary = 1759276800000
    previous, first, second = D(10000), D(0), D(0)
    for day in stress['days']:
        equity = number(day['equity'])
        if day['t'] <= boundary:
            first += equity-previous
        else:
            second += equity-previous
        previous = equity
    if min(first, second) <= 0:
        reasons.append('SUBPERIOD_SIGN')
    if not eligible(paths['delay']) or number(paths['delay']['books'][1]['net']) <= 0:
        reasons.append('DELAY_SENSITIVITY')
    if applied < 2:
        reasons.append('APPLIED_UPDATE_CAPACITY')
    adaptation = ('ADAPTATION_NOT_IDENTIFIED' if applied < 2 else
                  'INCREMENT_DEVELOPMENT_SUPPORTED' if min(increments.values()) >= 25 else
                  'INCREMENT_UNSUPPORTED')
    status = ('DEVELOPMENT_SUPPORTED' if not reasons else
              'ADAPTATION_NOT_IDENTIFIED' if applied < 2 else
              'FRAGILE_DEVELOPMENT_SIGNAL' if economic else 'NET_OR_RISK_UNSUPPORTED')
    expected = dict(status=status,
                    economic_status='BASE_AND_STRESS_POSITIVE' if economic else 'NET_OR_RISK_UNSUPPORTED',
                    recent_model_economic_status='BASE_AND_STRESS_POSITIVE' if recent_economic else 'NET_OR_RISK_UNSUPPORTED',
                    adaptation_status=adaptation, reasons=reasons, applied_updates=applied,
                    pressure_increments=increments, half_period_pressure_net=[first, second], **locks)
    same(expected, decision, 'ROLLING_VERDICT')
    return dict(status='INDEPENDENT_ROLLING_VERDICT_VERIFIED', paths=4,
                decision_status=status, actual_postinitial_updates=applied,
                complete_cycles=cycle_count, active_weeks=len(stress['active_weeks']),
                half_period_pressure_net=[str(first), str(second)])
