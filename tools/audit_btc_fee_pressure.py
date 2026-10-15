"""Independent feature, selection and economic-rule audit; no strategy replay."""
import csv
import datetime as dt
from decimal import Decimal as D
import io

DAY = 86400000
STEP = 300000
BOUNDARY = 1751328000000


def need(ok, why):
    if not ok:
        raise ValueError('FEE_AUDIT_' + why)


def features(fees_raw, weight_raw, rows, reported, start, end, lag):
    def values(raw):
        return {r['date']: D(r['value']) for r in csv.DictReader(io.StringIO(raw.decode()))}
    fees, weights = values(fees_raw), values(weight_raw)
    logs = {d: (fees[d]*100000000/weights[d]).ln() for d in fees
            if d in weights and fees[d] > 0 and weights[d] > 0}
    closes = {r['timestamp']+STEP: D(str(r['price'])) for r in rows}
    need(set(reported) == set(range(start, end, DAY)), 'DECISION_GRID')
    def close(actual, expected, why):
        need(abs(D(str(actual))-expected) < D('0.000000005'), why)
    def clip(x):
        return max(D(-3), min(D(3), x))
    for t, observed in reported.items():
        d = dt.datetime.fromtimestamp(t/1000, dt.timezone.utc).date()-dt.timedelta(days=lag)
        past = [logs[str(d-dt.timedelta(days=i))] for i in range(1,29)]
        mu = sum(past)/28
        sd = (sum((x-mu)**2 for x in past)/28).sqrt()
        z = clip((logs[str(d)]-mu)/sd) if sd >= D('.000001') else None
        if z is None:
            need(observed['z'] is None, 'ZERO_VARIANCE')
        else:
            close(observed['z'], z, 'STANDARDIZATION')
        close(observed['r'], clip((closes[t]/closes[t-7*DAY]).ln()/D('.05')), 'CAUSAL_PRICE_RETURN')
        close(observed['price'], closes[t], 'CAUSAL_PRICE')
        need(observed['source_day'] == str(d) and observed['latest_price_closed_at'] == t, 'FEATURE_CLOCK')
    return dict(status='DECIMAL_FEATURE_CLOCK_VERIFIED', decisions=len(reported), lag_days=lag)


def selection(training, reported):
    expected_grid = [[a,0,h] for a in (-1,1) for h in (1,2)] + [[0,b,h] for b in (-1,1) for h in (1,2)]
    need([r['parameters'] for r in training] == expected_grid, 'GRID')
    for r in training:
        need(all(not b['insufficient'] and b['quantity'] == 0 for b in r['books']), 'INCOMPLETE_TRAINING')
    def choose(indices, candidate):
        best = None
        for i in indices:
            r=training[i]
            if candidate and any(b['stopped'] for b in r['books']):
                continue
            score=min(D(str(b['net'])) for b in r['books'])
            key=(sum(D(str(b['turnover'])) for b in r['books']), tuple(r['parameters']))
            if best is None or score-best[1] > D('.000001') or (abs(score-best[1]) <= D('.000001') and key < best[2]):
                best=(i,score,key)
        return None if best is None else best[0]
    chosen,price=choose(range(4),True),choose(range(4,8),False)
    need(price is not None, 'PRICE_CONTROL_MISSING')
    status='NO_GO_TRAIN_RISK'
    if chosen is not None:
        enough=all(sum(c['complete'] for c in b['cycles']) >= 12 and len(b['active_weeks']) >= 8
                   for b in training[chosen]['books'])
        status='TRAINING_ADMITTED' if enough else 'INSUFFICIENT_TRAIN_CAPACITY'
    expected=dict(mechanism_index=chosen,price_index=price,fixed_index=2,
                  parameters=None if chosen is None else training[chosen]['parameters'],status=status)
    need(reported == expected, 'SELECTION_OR_CAPACITY')
    return dict(status='INDEPENDENT_SELECTION_VERIFIED',selection=expected)


def decision(paths, reported):
    need(len(paths)==6, 'PATH_COUNT')
    incomplete=any(b['insufficient'] or b['quantity'] for p in paths for b in p['books'])
    risk=any(b['stopped'] for i in (0,4,5) for b in paths[i]['books'])
    if incomplete:
        need(reported == dict(status='INSUFFICIENT_INPUT_OR_EXECUTION',hard_risk=bool(risk),
                             reasons=['EXECUTION_OR_RESIDUAL']), 'INCOMPLETE_VERDICT')
        return dict(status='INDEPENDENT_ECONOMIC_DECISION_VERIFIED',decision=reported['status'])
    def net(i,cost): return D(str(paths[i]['books'][cost]['net']))
    failures=[]
    if net(0,0)<=0 or net(0,1)<200: failures.append('ABSOLUTE_NET')
    if any(net(0,k)-net(2,k)<50 for k in (0,1)): failures.append('PRICE_INCREMENT')
    learned=paths[0]['parameters'] != [1,0,1]
    if learned and any(net(0,k)-net(1,k)<50 for k in (0,1)): failures.append('LEARNING_INCREMENT')
    mid=next((D(str(x['equity']))-10000 for x in paths[0]['books'][1]['days'] if x['t']==BOUNDARY),None)
    if mid is None or mid<=0 or net(0,1)-mid<=0: failures.append('HALF_YEAR_STABILITY')
    if any(net(i,k)<=0 for i in (4,5) for k in (0,1)): failures.append('DELAY_NET')
    def concentrated(values):
        positive=[D(str(x)) for x in values if D(str(x))>0]
        return not positive or max(positive) > sum(positive)*D('.35')
    diversity=any(sum(c['complete'] for c in b['cycles'])<12 or len(b['active_weeks'])<8 or
                  concentrated(c['net'] for c in b['cycles']) or concentrated(b['weeks'].values())
                  for b in paths[0]['books'])
    diversity=diversity or net(0,1)-max((D(str(x)) for x in paths[0]['books'][1]['weeks'].values()),default=D(0))<=0
    status=('NO_GO_RISK' if risk else 'NO_GO_ECONOMIC' if failures else
            'INSUFFICIENT_DIVERSITY' if diversity else 'DEVELOPMENT_SUPPORTED')
    expected=dict(status=status,reasons=['CANDIDATE_OR_SENSITIVITY_HARD_STOP'] if risk else
                  failures or (['EVENTS_OR_CONCENTRATION'] if diversity else []),economic_failures=failures,
                  hard_risk=bool(risk),diversity_failed=bool(diversity),learned=learned)
    need(reported==expected,'ECONOMIC_VERDICT')
    return dict(status='INDEPENDENT_ECONOMIC_DECISION_VERIFIED',decision=status,
                economic_failures=failures,hard_risk=bool(risk),diversity_failed=bool(diversity))
