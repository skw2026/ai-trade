"""Conditional economics at a consumed endpoint, never an execution admission."""
from decimal import Decimal as D,localcontext
from itertools import product
import copy
from bybit_linear_basis_panel import SYMBOLS,COSTS,quantities,sweep,cash_case,metadata
from bybit_linear_depth_endpoint import levels,used_levels,require


def pair(spot,future,instrument,end_wall,old):
    require(spot['symbol']=='BTCUSDT' and future['symbol']==instrument['symbol']==old['symbol'],'PAIR_IDENTITY')
    metadata(dict(retCode=0,result=dict(list=[instrument])),[future['symbol']])
    r=dict(symbol=future['symbol'],status='INPUT_INSUFFICIENT',cases=[],reasons=[],depth_used={},
        original_execution_status=old['status'],original_execution_reasons=list(old['reasons']),
        original_clock={k:copy.deepcopy(b['quote']) for k,b in [('spot',spot),('future',future)]},
        assumption='LAST_RECEIVED_ABSOLUTE_DEPTH_REMAINS_VALID_AT_T120_WITH_NO_UNRECEIVED_RELEVANT_UPDATES',
        input_use='CONSUMED_DEVELOPMENT',execution_admitted=False,actual_fills=False,
        path_validated=False,qualified=False,quote_age_risk_budget_certified=False)
    books={}
    for name,b in [('spot',spot),('future',future)]:
        if b['issue'] is not None or not b['initialized'] or b['snapshots']!=1 or b['quote'] is None:
            r['reasons'].append(name+':UNTRUSTED_BOOK_STATE');continue
        q=b['quote']
        require(all(type(q[k]) is int and q[k]>0 for k in ('ts','cts','wall','u','seq')),'CLOCK_SCHEMA')
        require(q['cts']<=q['ts']<=q['wall'] and q['ts']<=end_wall and 0<=q['mono']<=120000,'CLOCK_ORDER')
        books[name]=dict(b=levels(b['bids'],True,'b'),a=levels(b['asks'],True,'a'))
        if not books[name]['b'] or not books[name]['a']:r['reasons'].append(name+':EMPTY_BOOK')
        else:require(max(books[name]['b'])<min(books[name]['a']),'CROSSED_BOOK')
    if r['reasons']:return r
    r['time_controls']=dict(
        publication_age_ms={k:end_wall-v['ts'] for k,v in r['original_clock'].items()},
        receive_age_ms={k:120000-v['mono'] for k,v in r['original_clock'].items()},
        publication_skew_ms=abs(spot['quote']['ts']-future['quote']['ts']))
    with localcontext() as ctx:
        ctx.prec=40
        asks=sorted(books['spot']['a'].items());bids=sorted(books['future']['b'].items(),reverse=True)
        mid=(asks[0][0]+max(books['spot']['b']))/2
        days=D(int(instrument['deliveryTime'])-end_wall)/86400000;require(days>0,'EXPIRED')
        for cost,fees in COSTS.items():
            sf,ff,z=map(D,fees);q,gross,net=quantities(asks[0][0],sf);lot=instrument['lotSizeFilter']
            require(gross<=120 and q<=min(D(lot['maxMktOrderQty']),D(lot['maxOrderQty'])),'MAX_QUANTITY')
            try:
                spend=sweep(asks,gross)*(1+z);price=sweep(bids,q)/q*(1-z)
                r['depth_used'][cost]=dict(spot_asks=used_levels(asks,gross),future_bids=used_levels(bids,q))
            except ValueError as error:
                if str(error)!='DEPTH_INSUFFICIENT':raise
                r['reasons'].append(cost+':DEPTH_INSUFFICIENT');continue
            require(spend>=5 and q*price>=D(lot['minNotionalValue']),'MIN_NOTIONAL')
            for mult,dev,delivery in product(map(D,('.5','1','2')),map(D,('-.0025','0','.0025')),map(D,('0','.0005'))):
                c=cash_case(q,gross,net,spend,price,ff,sf,z,mid,days,mult,dev,delivery)
                c['cost']=cost;r['cases'].append(c)
        if r['reasons']:return r
        require(len(r['cases'])==36,'SCENARIO_COUNT')
        stress=[c for c in r['cases'] if c['cost']=='stress']
        for cost in COSTS:
            vals=[D(c['profit']) for c in r['cases'] if c['cost']==cost]
            r[cost+'_cash_range']=list(map(str,(min(vals),max(vals))))
        for label in ('occupied','total'):
            for rate in ('6','12'):
                key='excess_'+label+'_'+rate;vals=[D(c[key]) for c in stress]
                r['stress_'+key+'_range']=list(map(str,(min(vals),max(vals))))
        r['capital_budget_ok']=all(D(c['remaining_capital'])>=0 for c in r['cases'])
        r['terminal_margin_failure_cases']=sum(D(c['terminal_margin_headroom'])<0 for c in r['cases'])
        r['capital_productivity_6pct_supported']=min(D(c['excess_occupied_6']) for c in stress)>0
        r['reference_terminal_margin_supported']=r['terminal_margin_failure_cases']==0
        low,high=map(D,r['stress_cash_range'])
        r['additional_cash_cost_budget']=str(max(D(0),low))
        r['status']=('LAST_KNOWN_CONDITIONAL_CASH_UNSUPPORTED' if high<=0 else
             'LAST_KNOWN_CONDITIONAL_CASH_MARGIN' if low>0 and r['capital_budget_ok'] else
             'LAST_KNOWN_CONDITIONAL_ONLY')
        return r


def panel(original,instruments):
    require(original['endpoint_mono_ms']==120000 and original['qualified'] is False,'ORIGINAL_ENDPOINT')
    require([r['symbol'] for r in original['results']]==list(SYMBOLS),'FIXED_TERMS')
    results=[pair(original['books']['BTCUSDT'],original['books'][s],instruments[s],
                  original['endpoint_wall_ms'],old) for s,old in zip(SYMBOLS,original['results'])]
    return dict(schema='linear_last_known_conditional_v1',results=results,
                endpoint_wall_ms=original['endpoint_wall_ms'],endpoint_mono_ms=120000,
                economic_panels=1,new_market_requests=0,independent_events=0,
                original_execution_admitted=False,qualified=False)
