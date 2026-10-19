"""Fixed exploratory endpoints, with economics separate from update capacity."""
from btc_old_output_model import STEP, millis
from open_market_learning import complete, eligible

KEYS = {'old2021', 'recent2025', 'rolling', 'delay'}


def classify(results):
    locks = dict(independent_confirmation=False, qualified_candidates=0, g4=False,
                 online_learning_proven=False, demo_opening_lock_unchanged=True)
    if set(results) != KEYS:
        raise ValueError('COMPLETE_COMPARISON_SET_REQUIRED')
    paths = {k: r['path'] for k, r in results.items()}
    if any(p['latency'] != (2 if k == 'delay' else 1) for k, p in paths.items()):
        raise ValueError('COMPARISON_LATENCY_IDENTITY')
    full = all(complete(p) and p['bars'] == (p['end']-p['start'])//STEP for p in paths.values())
    full = full and len({(p['start'], p['end']) for p in paths.values()}) == 1
    if not full:
        return dict(status='COMPARISON_EVIDENCE_INCOMPLETE', economic_status='UNKNOWN',
                    adaptation_status='NOT_EVALUATED', reasons=['INCOMPLETE_COMPARISON'], **locks)
    candidate = paths['rolling']
    economic = eligible(candidate) and all(b['net'] > 0 for b in candidate['books'])
    recent = paths['recent2025']
    recent_economic = eligible(recent) and all(b['net'] > 0 for b in recent['books'])
    stress = candidate['books'][1]
    applied = sum(v['reason'] == 'SCHEDULED' for v in results['rolling']['governance']['applied_updates'])
    increments = {k: stress['net']-paths[k]['books'][1]['net'] for k in ('old2021', 'recent2025')}
    reasons = []
    if not eligible(candidate):
        reasons.append('ROLLING_RISK_OR_EXECUTION')
    if any(b['net'] <= 0 for b in candidate['books']):
        reasons.append('ROLLING_NET_NONPOSITIVE')
    if stress['net'] < 100:
        reasons.append('MINIMUM_ABSOLUTE_EFFECT')
    for k in ('old2021', 'recent2025'):
        if increments[k] < 25:
            reasons.append('MINIMUM_INCREMENT_'+k)
    if sum(c['complete'] for c in stress['cycles']) < 30 or len(stress['active_weeks']) < 12:
        reasons.append('EVENT_CAPACITY')
    if stress['net']-max([c['net'] for c in stress['cycles']]+[0.]) <= 0:
        reasons.append('BEST_CYCLE_CONCENTRATION')
    previous = 10000.
    first = second = 0.
    for d in stress['days']:
        change = d['equity']-previous
        if d['t'] <= millis('2025-10-01'):
            first += change
        else:
            second += change
        previous = d['equity']
    if min(first, second) <= 0:
        reasons.append('SUBPERIOD_SIGN')
    if not eligible(paths['delay']) or paths['delay']['books'][1]['net'] <= 0:
        reasons.append('DELAY_SENSITIVITY')
    if applied < 2:
        reasons.append('APPLIED_UPDATE_CAPACITY')
    adaptation = ('ADAPTATION_NOT_IDENTIFIED' if applied < 2 else
                  'INCREMENT_DEVELOPMENT_SUPPORTED' if all(v >= 25 for v in increments.values()) else
                  'INCREMENT_UNSUPPORTED')
    status = ('DEVELOPMENT_SUPPORTED' if not reasons else
              'ADAPTATION_NOT_IDENTIFIED' if applied < 2 else
              'FRAGILE_DEVELOPMENT_SIGNAL' if economic else 'NET_OR_RISK_UNSUPPORTED')
    return dict(status=status,
                economic_status='BASE_AND_STRESS_POSITIVE' if economic else 'NET_OR_RISK_UNSUPPORTED',
                recent_model_economic_status='BASE_AND_STRESS_POSITIVE' if recent_economic else 'NET_OR_RISK_UNSUPPORTED',
                adaptation_status=adaptation, reasons=reasons, applied_updates=applied,
                pressure_increments=increments, half_period_pressure_net=[first, second], **locks)
