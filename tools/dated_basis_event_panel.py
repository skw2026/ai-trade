"""Fixed four-event mechanism study, NOT a revived continuous account.

Reuses two frozen policies and their independent Decimal auditors unchanged.
No aggregate annual account, promotion, fetching, trading or parameter search.
"""
import dated_basis_conditional_replay as control
import dated_basis_convergence_replay as candidate
import audit_dated_basis_conditional_replay as control_audit
import audit_dated_basis_convergence_replay as candidate_audit

SYMBOLS = ('BTCUSD_240329', 'BTCUSD_240628', 'BTCUSD_240927', 'BTCUSD_241227')
GAPPED = 'BTCUSD_240628'
COMPLETE_TARGETS = tuple(s for s in SYMBOLS if s != GAPPED)


def _observed(result):
    return all(not p['uncertain'] and not p['unresolved'] and p['closed_quarters'] == 1
               for p in result['portfolios'])


def describe(symbol, result, reference):
    assert result['intentions'] == reference['intentions']
    assert len(result['portfolios']) == len(reference['portfolios']) == 36
    assert [p['scenario'] for p in result['portfolios']] == candidate.scenarios()
    assert [p['scenario'] for p in reference['portfolios']] == candidate.scenarios()
    by_id = {p['scenario']['id']: p for p in result['portfolios']}
    prior = {p['scenario']['id']: p for p in reference['portfolios']}
    b, s = (by_id[k] for k in ('base_h8_b0_m0', 'stress_h8_b0_m0'))
    pressure = [p for p in result['portfolios'] if p['scenario']['cost'] == 'stress']
    observed = _observed(result) and _observed(reference)
    cash = [p['cash_net'] for p in pressure if p['cash_net'] is not None]
    delta = {k: None if by_id[k]['cash_net'] is None or prior[k]['cash_net'] is None
             else by_id[k]['cash_net']-prior[k]['cash_net'] for k in ('base_h8_b0_m0', 'stress_h8_b0_m0')}
    positive = observed and all(p['cash_net'] > 0 and not p['halted'] for p in [b]+pressure)
    gain = observed and all(v is not None and v > 0 for v in delta.values())
    def central(p):
        path = p['paths'][symbol]
        return {k: path.get(k) for k in (
            'status', 'entry_ms', 'exit_ms', 'cash_net', 'gross', 'execution_fees',
            'slippage_drag', 'spot_spend', 'cash_drawdown', 'benchmark_excess',
            'risk_status', 'risk_reason', 'first_unknown_ms', 'unknown_reasons',
            'convergence_decision_bar', 'settled')}
    return dict(symbol=symbol, event_account_initial_cash=10000, observed_complete=observed,
                candidate_absolute_support=positive, central_increment_support=gain,
                pressure_cash_min=min(cash, default=None), pressure_cash_max=max(cash, default=None),
                central_delta=delta, candidate_base=central(b), candidate_stress=central(s),
                control_base=central(prior['base_h8_b0_m0']), control_stress=central(prior['stress_h8_b0_m0']),
                candidate_halted_scenarios=sum(p['halted'] for p in result['portfolios']),
                candidate_uncertain_scenarios=sum(p['uncertain'] for p in result['portfolios']),
                candidate_unresolved_scenarios=sum(p['unresolved'] for p in result['portfolios']))


def summarize(rows):
    if [r['symbol'] for r in rows] != list(SYMBOLS):
        raise ValueError('EXACT_FOUR_EVENTS_REQUIRED')
    target = [r for r in rows if r['symbol'] in COMPLETE_TARGETS]
    if not all(r['observed_complete'] for r in target):
        code = 'EVENT_EVIDENCE_INSUFFICIENT'
    elif not all(r['candidate_absolute_support'] for r in target) or sum(r['central_increment_support'] for r in target) < 2:
        code = 'NO_REPEATABLE_EVENT_SUPPORT'
    else:
        code = 'REPEATED_OBSERVED_EVENT_SUPPORT'
    return dict(status=code, design='INDEPENDENT_EVENT_PANEL', events_registered=4,
                observed_complete_events=sum(r['observed_complete'] for r in rows),
                unknown_or_unclosed_events=sum(not r['observed_complete'] for r in rows),
                absolute_support_events=sum(r['candidate_absolute_support'] for r in rows),
                central_increment_support_events=sum(r['central_increment_support'] for r in rows),
                continuous_account_status='DATA_INSUFFICIENT_UNCHANGED',
                annual_return=None, statistical_independence_established=False,
                actual_execution_qualified=False, candidate_qualified=False,
                independent_confirmation_attempts=0, g4=False)


def replay(bundle):
    if set(bundle) != set(SYMBOLS):
        raise ValueError('EXACT_FOUR_EVENTS_REQUIRED')
    events = {}
    rows = []
    for symbol in SYMBOLS:
        singleton = {symbol: bundle[symbol]}
        c = control.replay(singleton)
        r = candidate.replay(singleton)
        ca = control_audit.verify(singleton, c)
        ra = candidate_audit.verify(singleton, r)
        row = describe(symbol, r, c)
        rows.append(row)
        events[symbol] = dict(candidate=r, control=c, candidate_audit=ra, control_audit=ca)
    return dict(summary=summarize(rows), rows=rows, events=events)
