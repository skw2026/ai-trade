"""One frozen development policy with a full delivery-to-spot-disposal path.

Offline only. No fetch, account, order, training or promotion interface. Historical
OHLC fills and contract specifications remain conditional model assumptions.
"""
from dataclasses import asdict, replace
import itertools
import math

from dated_basis_dual_ledger import BAR, DAY, FACE, CashState, Position, first_opportunity
from dated_basis_opportunity_capacity import project


def scenarios():
    return [dict(id=f'{cost}_h{hour}_b{bias}_m{delay}', cost=cost, hour=hour,
                 bias_bps=bias, delay_minutes=delay)
            for cost, hour, bias, delay in itertools.product(
                ('base', 'stress'), (0, 8), (-25, 0, 25), (0, 5, 60))]


def usable(bar, t, trades=True):
    if bar is None:
        return False
    if bar.get('complete') is not True or bar.get('close_ms') != t + BAR - 1:
        return False
    values = [float(bar[k]) for k in ('o', 'h', 'l', 'c', 'v')]
    o, h, l, c, v = values
    if not all(math.isfinite(x) for x in values) or min(o,h,l,c) <= 0:
        raise ValueError('INVALID_BAR_PRICE')
    if not l <= min(o,c) <= max(o,c) <= h or v < 0:
        raise ValueError('INVALID_BAR_BOUNDS')
    return not trades or v > 0 and bar['n'] > 0


def intentions(bundle):
    projected = project(bundle)
    result = {}
    for symbol, rows in projected.items():
        quotes = [dict(t=r['t'], spot=float(r.get('spot', 1)),
                       future=float(r.get('future', 1)), valid=not r['reasons']) for r in rows]
        result[symbol] = first_opportunity(quotes, expiry=bundle[symbol]['expiry'],
                                          observed_through=bundle[symbol]['expiry']-DAY)
    return result


def quarter_path(quarter, scenario, intention, state=None):
    """Never chooses a new intention in response to fills or terminal outcomes."""
    state = CashState() if state is None else state
    if scenario not in scenarios():
        raise ValueError('SCENARIO_SCOPE')
    initial = asdict(state)
    common = dict(initial_state=initial, actual_execution_qualified=False,
                  candidate_qualified=False, g4=False)
    if state.halted or state.uncertain:
        return dict(common, status='BLOCKED_PRIOR_STATE', cash_state=initial, cash_net=0.)
    if intention is None:
        return dict(common, status='NO_INTENTION', cash_state=initial, cash_net=0.)
    entry = intention['earliest_entry_ms']
    if entry != intention['decision_bar']+2*BAR or intention['available_ms'] != entry-BAR:
        raise ValueError('INTENTION_CLOCK')
    settle = quarter['expiry'] - 8*3600000 + scenario['hour']*3600000
    planned = settle + scenario['delay_minutes']*60000
    end = quarter['expiry'] + 3600000
    if entry >= settle or entry % BAR:
        raise ValueError('ENTRY_DOMAIN')
    series = {k: {int(t):b for t,b in rows.items()} for k,rows in quarter['series'].items()}
    good = lambda k,t: usable(series[k].get(t), t, trades=k != 'mark')
    price = lambda k,t,field: float(series[k][t][field])
    common.update(entry_ms=entry, settlement_ms=settle, planned_exit_ms=planned,
                  last_exit_attempt_ms=end, decision_bar=intention['decision_bar'])
    if not all(good(k,entry) for k in ('spot','future')):
        return dict(common, status='ENTRY_UNVERIFIABLE', cash_state=asdict(replace(state,uncertain=True)),
                    cash_net=0., risk_status='UNKNOWN', first_unknown_ms=entry)
    position = Position(state,entry_ms=entry,spot=price('spot',entry,'o'),
                        future=price('future',entry,'o'),cost=scenario['cost'],
                        financing_status='DECLARED_SELF_FINANCED_MODEL')
    index = float(quarter['settlement'])*(1+scenario['bias_bps']/10000)
    if not math.isfinite(index) or index <= 0:
        raise ValueError('SETTLEMENT_PRICE')
    settled = False
    settlement_event = None
    checked = tail_bars = missing = unavailable = 0
    first_unknown = trigger_ms = None
    unknown_reasons = {}

    def unknown(t, reasons):
        nonlocal first_unknown
        if first_unknown is None:
            first_unknown = t
        for reason in reasons:
            unknown_reasons[reason] = unknown_reasons.get(reason,0)+1
        position.uncertain = True

    for t in range(entry,end+BAR,BAR):
        if not settled and t >= settle:
            # Record settlement once. Position.close later reconstructs this same
            # terminal transfer, rather than debiting this event a second time.
            coins = position.wallet+FACE*(1/index-1/position.fin)-FACE/index*position.ff
            settlement_event = dict(time_ms=t,index=index,fee_btc=FACE/index*position.ff,
                                    coins_before_later_liabilities=coins)
            settled = True
        pending = position.pending_exit_ms
        due = (settled and t >= planned) or pending is not None and t >= pending
        if due:
            fill_kinds = ('spot',) if settled else ('spot','future')
            if all(good(k,t) for k in fill_kinds):
                raw_spot = price('spot',t,'o')
                raw_future = index if settled else price('future',t,'o')
                ledger = position.close(t,spot=raw_spot,future=raw_future,settlement=settled)
                if settled:
                    usd, btc = position.owed(t)
                    cash = position.cash-usd+position._spot_cash(
                        settlement_event['coins_before_later_liabilities']-btc,raw_spot)[0]
                    if not math.isclose(cash,ledger['cash_state'].equity,abs_tol=1e-8):
                        raise ValueError('DELIVERY_DOUBLE_COUNT')
                if trigger_ms is None and ledger['risk_reason'] is not None:
                    trigger_ms=t
                ledger['cash_state']=asdict(ledger['cash_state'])
                return dict(common, **ledger, status='CLOSED', exit_spot=raw_spot,
                            exit_future=raw_future, settled=settled, settlement_event=settlement_event,
                            risk_bar_count=checked, tail_bar_count=tail_bars,
                            missing_bar_count=missing, unavailable_exit_count=unavailable,
                            first_unknown_ms=first_unknown, first_risk_trigger_ms=trigger_ms,
                            unknown_reasons=unknown_reasons)
            unavailable += 1
            unknown(t,['EXIT_PROXY_UNAVAILABLE'])
        if t == end:
            break
        required = ('spot',) if settled else ('spot','mark','future')
        bad = [k for k in required if not good(k,t)]
        if bad:
            missing += 1
            unknown(t,[k.upper()+'_RISK_OR_LIQUIDITY_UNOBSERVED' for k in bad])
            position.missing_risk_bar(t)
        elif settled:
            position._clock(t+BAR)
            usd, btc = position.owed(t+BAR)
            coins = settlement_event['coins_before_later_liabilities']-btc
            values=[position.cash-usd+position._spot_cash(coins,price('spot',t,k))[0]
                    for k in ('l','h')]
            position._observe(values,None,t)
            checked += 1
            tail_bars += 1
        else:
            position.observe_bar(t,spot_low=price('spot',t,'l'),spot_high=price('spot',t,'h'),
                                 mark_low=price('mark',t,'l'),mark_high=price('mark',t,'h'))
            checked += 1
        if trigger_ms is None and position.reason is not None:
            trigger_ms=t+BAR
    # No terminal fill, hence no fabricated equity / realized return. The last
    # realized initial balance is not the open-position value.
    return dict(common,status='UNRESOLVED',cash_state=None,cash_net=None,risk_status='UNKNOWN',
                settled=settled,settlement_event=settlement_event,exit_ms=None,
                cash_drawdown=position.max_dd,minimum_margin_ratio=position.min_margin,
                risk_reason=position.reason,first_risk_trigger_ms=trigger_ms,
                risk_bar_count=checked,tail_bar_count=tail_bars,missing_bar_count=missing,
                unavailable_exit_count=unavailable,first_unknown_ms=first_unknown,
                unknown_reasons=unknown_reasons)


def portfolio(bundle, scenario, mask):
    state=CashState(); paths={}; unresolved=False
    for symbol,quarter in sorted(bundle.items(),key=lambda pair:pair[1]['expiry']):
        if unresolved:
            paths[symbol]=dict(status='BLOCKED_UNRESOLVED',cash_state=None,cash_net=None)
            continue
        path=quarter_path(quarter,scenario,mask[symbol],state)
        paths[symbol]=path
        if path['cash_state'] is None:
            unresolved=True
        else:
            state=CashState(**path['cash_state'])
    closed=[p for p in paths.values() if p['status']=='CLOSED']
    net=None if unresolved else state.equity-10000
    contributions=[p['cash_net'] for p in closed]
    return dict(scenario=scenario,paths=paths,cash_net=net,
                cash_return_pct=None if net is None else net/100,
                end_state=None if unresolved else asdict(state),
                closed_quarters=len(closed),unresolved=unresolved,
                observed_cash_drawdown=max((p.get('cash_drawdown',0.) for p in paths.values()),default=0.),
                without_best_quarter=None if not contributions or unresolved else sum(contributions)-max(contributions),
                benchmark_excess={r:None if unresolved else sum(p['benchmark_excess'][r] for p in closed)
                                  for r in ('0.06','0.12')},
                gross=sum(p['gross'] for p in closed),
                slippage_drag=sum(p['slippage_drag'] for p in closed),
                execution_fees=sum(p['execution_fees'] for p in closed),
                halted=state.halted,uncertain=state.uncertain or unresolved)


def summarize(portfolios, mask):
    rows={p['scenario']['id']:p for p in portfolios}
    base=rows['base_h8_b0_m0']; central=rows['stress_h8_b0_m0']
    pressure=[p for p in portfolios if p['scenario']['cost']=='stress']
    if not any(mask.values()):
        status='NO_TRADES'
    elif any(p['uncertain'] or p['unresolved'] for p in portfolios):
        status='DATA_INSUFFICIENT'
    elif any(p['halted'] for p in [base]+pressure):
        status='RISK_STOP'
    elif (base['cash_net'] > 0 and all(p['cash_net'] > 0 for p in pressure)
          and min(base['closed_quarters'],central['closed_quarters']) >= 2
          and base['without_best_quarter'] > 0 and central['without_best_quarter'] > 0):
        status='DEVELOPMENT_SUPPORTED'
    else:
        status='NET_UNSUPPORTED'
    pressure_values=[p['cash_net'] for p in pressure if p['cash_net'] is not None]
    return dict(status=status,base_central_cash=base['cash_net'],stress_central_cash=central['cash_net'],
                pressure_cash_min=min(pressure_values,default=None),pressure_cash_max=max(pressure_values,default=None),
                base_closed_quarters=base['closed_quarters'],stress_closed_quarters=central['closed_quarters'],
                uncertain_scenarios=sum(p['uncertain'] for p in portfolios),
                halted_scenarios=sum(p['halted'] for p in portfolios),
                unresolved_scenarios=sum(p['unresolved'] for p in portfolios),
                policy_count=1,economic_batch_count=1,training_vectors=0,
                independent_confirmation_attempts=0,candidate_qualified=False,g4=False)


def replay(bundle):
    mask=intentions(bundle)
    portfolios=[portfolio(bundle,s,mask) for s in scenarios()]
    return dict(intentions=mask,portfolios=portfolios,summary=summarize(portfolios,mask),
                assumptions=dict(financing='DECLARED_SELF_FINANCED_MODEL',actual_borrowing_certified=False,
                                 historical_specs_certified=False,fill_proxies='OHLC_NOT_EXECUTION_PROOF',
                                 independent_domain=False,benchmark_investability_certified=False))
