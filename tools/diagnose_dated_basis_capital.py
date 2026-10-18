"""Synthetic-only evidence of opportunity-cost propagation in the frozen engine.

This is NOT a new trading model or a market-payoff runner. Temporary constants
are restored before returning. The original engine and audit files are unchanged.
"""
from decimal import Decimal as D, localcontext, ROUND_CEILING
from unittest.mock import patch
import copy

import dated_basis_term_screen as engine
import audit_dated_basis_term as reference

BAR=300000
DAY=86400000
EXPIRY=100*DAY
ENTRY=EXPIRY-90*DAY+BAR
SCENARIO=dict(id='stress_h8_b0_m0',cost='stress',hour=8,bias_bps=0,delay_minutes=0)


def synthetic(future=104, shock=False):
    series={k:{} for k in ('spot','future','mark')}
    for t in range(EXPIRY-90*DAY-BAR,EXPIRY+BAR,BAR):
        spot=70 if shock and t>=ENTRY+BAR else 100
        for key,price in (('spot',spot),('future',future),('mark',future)):
            series[key][t]=dict(o=float(price),h=float(price),l=float(price),c=float(price),
                               v=1.,n=1,complete=True,close_ms=t+BAR-1)
    return dict(expiry=EXPIRY,settlement=100.,series=series)


def one(annual, peak, future=104, shock=False):
    quarter=synthetic(future,shock)
    profile=dict(engine.PROFILES['stress'],annual=annual)
    params=(*reference.PARAMS['stress'][:3],str(annual))
    with patch.dict(engine.PROFILES,{'stress':profile}), patch.dict(reference.PARAMS,{'stress':params}):
        result=engine.quarter_path(quarter,SCENARIO,10000.,peak,90)
        with localcontext() as ctx:
            ctx.prec=40
            checked=reference.check_ledger(reference.prep(quarter),result,90)
    # Account value on the SAME exit path, not a new cash-risk strategy.
    result['same_path_cash_end']=result['end_capital']+result['opportunity_cost']
    result['same_path_cash_net']=result['net']+result['opportunity_cost']
    return result,checked['checked_bars']


def constant_cash_reference():
    with localcontext() as ctx:
        ctx.prec=40
        face=D(2500);s=D(100);f=D(104);sf=D('.002');ff=D('.001');z=D('.001')
        size=face/(f*(1-z))
        spend=size*s*(1+z)*(1+sf)
        # Liquidation quote before settlement stays constant throughout the flat fixture.
        coins=face*(1-ff)/(f*(1+z))-size*ff
        equity=D(10000)-spend+coins*s*(1-z)*(1-sf)
        peak=D(10800)
        daily=spend*D('.12')/365
        charge_to_limit=equity-peak*D('.92')
        first_bars=(charge_to_limit/(daily/D(288))).to_integral_value(rounding=ROUND_CEILING)
        return dict(spend=spend,cash_equity=equity,cash_drawdown=(peak-equity)/peak,
            charge_needed_to_economic_stop=charge_to_limit,charge_per_day=daily,
            first_trigger_ms=ENTRY+(int(first_bars)-1)*BAR,
            expected_exit_ms=ENTRY+int(first_bars)*BAR)


def run_controls():
    original_profiles=copy.deepcopy(engine.PROFILES)
    original_params=copy.deepcopy(reference.PARAMS)
    spec=(('opportunity_debited',.12,10800.,104,False),
          ('cash_control',0.,10800.,104,False),
          ('cash_loss_control',0.,10800.,104,True),
          ('no_edge_control',0.,10000.,100,False))
    results={};bars=0
    for name,annual,peak,future,shock in spec:
        result,checked=one(annual,peak,future,shock)
        results[name]=result;bars+=checked
    assert engine.PROFILES==original_profiles and reference.PARAMS==original_params
    analytic=constant_cash_reference()
    reference.close(results['opportunity_debited']['same_path_cash_end'],analytic['cash_equity'],'CASH_REFERENCE')
    assert results['opportunity_debited']['exit_ms']==analytic['expected_exit_ms']
    return dict(status='SYNTHETIC_CAPITAL_SEMANTICS_DIAGNOSIS',controls=results,
        analytic={k:str(v) if isinstance(v,D) else v for k,v in analytic.items()},
        decimal_checked_ledgers=4,decimal_checked_risk_bars=bars,
        original_constants_restored=True,market_payoff_attempts=0,training_vectors=0,
        candidate_qualified=False,g4=False)
