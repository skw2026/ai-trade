"""One fixed reserve per expiry over a frozen terminal domain, not live risk."""
from collections import Counter
from decimal import Decimal as D,localcontext
from itertools import product
from bybit_linear_basis_panel import SYMBOLS


def require(ok,reason):
    if not ok:raise ValueError(reason)


def number(value):
    require(isinstance(value,str),'DECIMAL_STRING')
    v=D(value);require(v.is_finite(),'NONFINITE');return v


def key(c):return (c['cost'],number(c['index_multiple']),number(c['deviation']),number(c['delivery_rate']))


def analyze_term(term):
    require(term['symbol'] in SYMBOLS,'SYMBOL')
    out=dict(symbol=term['symbol'],status='INPUT_INSUFFICIENT',cases=[],
        original_cash_status=term['status'],original_execution_status=term['original_execution_status'],
        original_execution_reasons=list(term['original_execution_reasons']),
        execution_admitted=False,path_validated=False,qualified=False,
        actual_margin_rule_certified=False,reference_terminal_only=True)
    if term['reasons']:return out
    cases=term['cases']
    expected=Counter(product(('base','stress'),map(D,('.5','1','2')),
                             map(D,('-.0025','0','.0025')),map(D,('0','.0005'))))
    require(Counter(key(c) for c in cases)==expected,'COMPLETE_GRID')
    with localcontext() as ctx:
        ctx.prec=80
        values=[{k:number(c[k]) for k in ('q','future_price','index','index_multiple','initial_margin_reference',
             'occupied_capital','days','profit','delivery_rate')} for c in cases]
        require(len({v['q'] for v in values})==1 and values[0]['q']>0,'FIXED_QUANTITY')
        require(len({v['index']/v['index_multiple'] for v in values})==1,'FIXED_SPOT_REFERENCE')
        require(len({v['days'] for v in values})==1 and values[0]['days']>0,'FIXED_POSITIVE_DAYS')
        for cost in ('base','stress'):
            selected=[v for v,c in zip(values,cases) if c['cost']==cost]
            require(len({v['future_price'] for v in selected})==1,'FIXED_ENTRY_PRICE')
        require(all(v['future_price']>0 and v['index']>0 and v['initial_margin_reference']>=0 and
                    v['occupied_capital']>=v['initial_margin_reference'] for v in values),'CAPITAL_INPUT')
        imax=max(v['index'] for v in values)
        needed=max([D(0)]+[v['q']*(D('1.1005')*imax-v['future_price']) for v in values])
        reserve=max(needed,max(v['initial_margin_reference'] for v in values))
        out.update(required_terminal_margin=str(needed),fixed_margin=str(reserve),terminal_index_limit=str(imax))
        for old,v in zip(cases,values):
            added=reserve-v['initial_margin_reference'];occupied=v['occupied_capital']+added
            headroom=reserve+v['q']*(v['future_price']-v['index'])-v['q']*v['index']*v['delivery_rate']-D('.10')*v['q']*v['index']
            c={k:old[k] for k in ('cost','index_multiple','deviation','delivery_rate','profit','days')}
            c.update(additional_margin=str(added),fixed_margin=str(reserve),occupied_capital=str(occupied),
                remaining_capital=str(D(10000)-occupied),terminal_headroom=str(headroom),
                break_even_simple_capital_rate_pct=str(v['profit']/occupied*365/v['days']*100))
            for rate in (6,12):
                c['excess_occupied_'+str(rate)]=str(v['profit']-occupied*D(rate)/100*v['days']/365)
            out['cases'].append(c)
        def bounds(field,selected):return list(map(str,(min(D(c[field]) for c in selected),max(D(c[field]) for c in selected))))
        stress=[c for c in out['cases'] if c['cost']=='stress']
        out.update(additional_margin_range=bounds('additional_margin',out['cases']),
             occupied_capital_range=bounds('occupied_capital',out['cases']),
             remaining_capital_range=bounds('remaining_capital',out['cases']),
             terminal_headroom_range=bounds('terminal_headroom',out['cases']),
             stress_cash_range=bounds('profit',stress),
             stress_break_even_simple_capital_rate_pct_range=bounds('break_even_simple_capital_rate_pct',stress),
             stress_excess_occupied_6_range=bounds('excess_occupied_6',stress),
             stress_excess_occupied_12_range=bounds('excess_occupied_12',stress))
        out['budget_ok']=all(D(c['remaining_capital'])>=0 for c in out['cases'])
        out['reference_terminal_nonnegative']=all(D(c['terminal_headroom'])>=0 for c in out['cases'])
        out['zero_headroom_cases']=sum(D(c['terminal_headroom'])==0 for c in out['cases'])
        require(out['reference_terminal_nonnegative'],'ANALYTIC_INEQUALITY_FAILURE')
        out['status']='REFERENCE_PREFUNDING_FEASIBLE' if out['budget_ok'] else 'REFERENCE_BUDGET_INSUFFICIENT'
        return out


def panel(original):
    require(original['endpoint_mono_ms']==120000 and original['qualified'] is False,'ORIGINAL_ENDPOINT')
    require([r['symbol'] for r in original['results']]==list(SYMBOLS),'ALL_FIXED_TERMS')
    return dict(schema='linear_prefunded_tail_v1',results=[analyze_term(t) for t in original['results']],
        endpoint_wall_ms=original['endpoint_wall_ms'],endpoint_mono_ms=120000,
        new_market_requests=0,cash_recalculations=0,capital_analyses=1,
        independent_events=0,qualified=False,execution_admitted=False,path_validated=False)
