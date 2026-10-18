"""Three fixed horizon covered inverse-futures policies with conditional sensitivity paths.

No candidate promotion, account access or orders. Prices are reconstructed
proxies; all costs, settlement clocks and margin rules are declared assumptions.
"""
import itertools
import math

HORIZONS = (30, 60, 90)


def require_horizon(days):
    if isinstance(days, bool) or days not in HORIZONS:
        raise ValueError('HORIZON_SCOPE')


BAR = 300000
DAY = 86400000
YEAR = 365*DAY
FACE = 2500.0
CAPITAL = 10000.0
PROFILES = {
    'zero_friction': dict(spot=0.0, future=0.0, slip=0.0, annual=0.0),
    'base': dict(spot=.001, future=.0005, slip=.0002, annual=.06),
    'stress': dict(spot=.002, future=.001, slip=.001, annual=.12),
}


def scenarios():
    return [dict(id=f'{cost}_h{hour}_b{bias}_m{delay}', cost=cost, hour=hour,
                 bias_bps=bias, delay_minutes=delay)
            for cost, hour, bias, delay in itertools.product(PROFILES, (0, 8), (-25, 0, 25), (0, 5, 60))]


def normalized(quarter):
    return dict(quarter, series={kind: {int(t): dict(bar, **{
        key: float(bar[key]) for key in ('o','h','l','c','v')}) for t,bar in rows.items()}
        for kind,rows in quarter['series'].items()}, settlement=float(quarter['settlement']))


def decision(quarter, days):
    """One causal base-cost mask shared by every sensitivity scenario."""
    require_horizon(days)
    t = quarter['expiry']-days*DAY-BAR
    spot, future = quarter['series']['spot'][t]['c'], quarter['series']['future'][t]['c']
    p = PROFILES['base']
    f = future*(1-p['slip']);s = spot*(1+p['slip']);q = FACE/f
    spent = q*s*(1+p['spot'])
    # Forecast only: no spot drift and USD/USDT parity. Never reads actual settlement.
    wallet = q*(1-p['future'])+FACE*(1/spot-1/f)-FACE/spot*p['future']
    proceeds = wallet*spot*(1-p['slip'])*(1-p['spot'])
    opportunity = spent*p['annual']*(days*DAY-BAR)/YEAR
    expected = proceeds-spent-opportunity
    return dict(decision_bar=t, expected_net=expected, enter=expected > 0,
                spot=spot, future=future, forecast='FLAT_SPOT_AND_USD_USDT_PARITY_ASSUMPTION')


def quarter_path(quarter, scenario, capital=CAPITAL, prior_peak=None, days=30):
    require_horizon(days)
    p = PROFILES[scenario['cost']]
    series = quarter['series'];entry = quarter['expiry']-days*DAY+BAR
    settle = quarter['expiry']-8*3600000+scenario['hour']*3600000
    planned_exit = settle+scenario['delay_minutes']*60000
    index = quarter['settlement']*(1+scenario['bias_bps']/10000)
    raw_s, raw_f = series['spot'][entry]['o'], series['future'][entry]['o']
    s_in, f_in = raw_s*(1+p['slip']), raw_f*(1-p['slip'])
    q = FACE/f_in;spot_fee_in = q*s_in*p['spot'];spent = q*s_in+spot_fee_in
    fee_in_btc = q*p['future'];wallet = q-fee_in_btc;cash = capital-spent
    if cash < 0:
        raise ValueError('INSUFFICIENT_UNLEVERED_CASH')
    peak = max(capital, prior_peak if prior_peak is not None else capital)
    worst_dd = 0.0;min_margin = math.inf;checked = 0;unknown = 0
    settled = False;pending_exit = None;risk_reason = None;exit_t = None
    future_exit = None;raw_future_exit = None;fee_out_btc = None
    risk_exit_unavailable = False
    for t in range(entry, planned_exit+BAR, BAR):
        if not settled and t >= settle:
            raw_future_exit = index;future_exit = index
            fee_out_btc = FACE/index*p['future']
            wallet += FACE*(1/index-1/f_in)-fee_out_btc
            settled = True
        if t == planned_exit or (pending_exit is not None and t >= pending_exit):
            spot_bar = series['spot'].get(t)
            future_bar = series['future'].get(t) if not settled else None
            usable = spot_bar is not None and spot_bar.get('complete', True)
            if not settled:
                usable = usable and future_bar is not None and future_bar.get('complete', True)
            if usable:
                if spot_bar['n'] == 0 or spot_bar['v'] == 0 or (not settled and
                        (future_bar['n'] == 0 or future_bar['v'] == 0)):
                    unknown += 1
                if not settled:
                    raw_future_exit = future_bar['o']
                    future_exit = raw_future_exit*(1+p['slip'])
                    fee_out_btc = FACE/future_exit*p['future']
                    wallet += FACE*(1/future_exit-1/f_in)-fee_out_btc
                exit_t = t
                break
            # No fabricated fill: keep the pending risk exit, disclose unresolved latency.
            risk_exit_unavailable = True;unknown += 1
        spot_bar = series['spot'].get(t)
        mark_bar = series['mark'].get(t) if not settled else None
        future_bar = series['future'].get(t) if not settled else None
        usable = spot_bar is not None and spot_bar.get('complete', True)
        if not settled:
            usable = usable and mark_bar is not None and mark_bar.get('complete', True)
        if not usable:
            unknown += 1
            continue
        if spot_bar['n'] == 0 or spot_bar['v'] == 0 or (not settled and (
                future_bar is None or not future_bar.get('complete', True) or
                future_bar['n'] == 0 or future_bar['v'] == 0)):
            unknown += 1  # Quoted-price computation remains conditional, never executable proof.
        opportunity = spent*p['annual']*(t+BAR-entry)/YEAR
        if settled:
            values = [cash+wallet*s*(1-p['slip'])*(1-p['spot'])-opportunity
                      for s in (spot_bar['l'],spot_bar['h'])]
            margin = math.inf
        else:
            values = []
            ratios = []
            for m in (mark_bar['l'],mark_bar['h']):
                equity_btc = wallet+FACE*(1/m-1/f_in)
                ratios.append(equity_btc/(.05*FACE/m))
                close = m*(1+p['slip'])
                liquid_btc = wallet+FACE*(1/close-1/f_in)-FACE/close*p['future']
                values.extend(cash+liquid_btc*s*(1-p['slip'])*(1-p['spot'])-opportunity
                              for s in (spot_bar['l'],spot_bar['h']))
            margin = min(ratios);min_margin = min(min_margin, margin)
        # Within-bar ordering is unknown: favorable then adverse is a conservative envelope.
        peak = max(peak, max(values));dd = (peak-min(values))/peak
        worst_dd = max(worst_dd, dd);checked += 1
        if pending_exit is None and (dd >= .08 or margin <= 2):
            pending_exit = t+BAR
            risk_reason = 'DRAWDOWN_8_PERCENT' if dd >= .08 else 'MARGIN_BUFFER'
    if exit_t is None:
        raise ValueError('NO_EXIT_PRICE')
    raw_out = series['spot'][exit_t]['o'];s_out = raw_out*(1-p['slip'])
    spot_fee_out = wallet*s_out*p['spot']
    proceeds = wallet*s_out-spot_fee_out
    opportunity = spent*p['annual']*(exit_t-entry)/YEAR
    final = cash+proceeds-opportunity
    peak = max(peak, final);worst_dd = max(worst_dd, (peak-final)/peak)
    if risk_reason is None and worst_dd >= .08:
        risk_reason = 'EXIT_DRAWDOWN_8_PERCENT'
    gross = FACE*raw_out/raw_future_exit-FACE*raw_s/raw_f
    no_fee = FACE*s_out/future_exit-FACE*s_in/f_in
    fees = spot_fee_in+spot_fee_out+(fee_in_btc+fee_out_btc)*s_out
    slippage = gross-no_fee
    net = final-capital
    if not math.isclose(net, gross-slippage-fees-opportunity, abs_tol=1e-8):
        raise ValueError('CASH_DECOMPOSITION')
    return dict(scenario=scenario, entry_ms=entry, settlement_ms=settle,
                planned_exit_ms=planned_exit, exit_ms=exit_t, capital=capital,
                prior_peak=prior_peak, end_capital=final, end_peak=peak, net=net,
                gross=gross, slippage_drag=slippage, fees=fees, opportunity_cost=opportunity,
                extra_cost_headroom_bps=net/FACE*10000, gross_cost_capacity_bps=gross/FACE*10000,
                spot_entry=raw_s, future_entry=raw_f, spot_exit=raw_out,
                execution_spot_entry=s_in, execution_future_entry=f_in,
                execution_future_exit=future_exit, raw_future_exit=raw_future_exit,
                execution_spot_exit=s_out, btc_bought=q, btc_after_exit=wallet,
                spot_spend=spent, spot_fee_in=spot_fee_in, spot_fee_out=spot_fee_out,
                future_fee_in_btc=fee_in_btc, future_fee_out_btc=fee_out_btc,
                held_ms=exit_t-entry, risk_reason=risk_reason, observed_drawdown=worst_dd,
                minimum_margin_ratio=None if math.isinf(min_margin) else min_margin,
                checked_bars=checked, unknown_bars=unknown,
                risk_exit_unavailable=risk_exit_unavailable,
                risk_status='UNKNOWN' if unknown or risk_exit_unavailable else 'MODEL_OBSERVED_ONLY',
                actual_execution_qualified=False)


def classify(portfolios, mask):
    if not any(v['enter'] for v in mask.values()):
        return 'NO_GO_NO_TRADES'
    optimistic = [p for p in portfolios if p['scenario']['cost'] == 'zero_friction']
    if max(p['net'] for p in optimistic) <= 0:
        return 'NO_GO_EVEN_OPTIMISTIC'
    base = next(p for p in portfolios if p['scenario']['id'] == 'base_h8_b0_m0')
    if base['net'] <= 0 or base['halted']:
        return 'NO_GO_BASE'
    pressure = [p for p in portfolios if p['scenario']['cost'] == 'stress']
    if any(p['net'] <= 0 or p['halted'] or p['risk_unknown'] for p in pressure):
        return 'CONDITIONAL_ONLY'
    return 'ASSUMPTION_ROBUST_DEVELOPMENT'


def sweep(bundle, days):
    require_horizon(days)
    quarters = {s: normalized(q) for s,q in bundle.items()}
    ordered = sorted(quarters, key=lambda s: quarters[s]['expiry'])
    mask = {s: decision(quarters[s], days) for s in ordered}
    diagnostics = {s: [quarter_path(quarters[s], scenario, days=days) for scenario in scenarios()] for s in ordered}
    portfolios = []
    for scenario in scenarios():
        capital = peak = CAPITAL;halted = False;unknown = False;paths = []
        for symbol in ordered:
            if halted or not mask[symbol]['enter']:
                paths.append(dict(symbol=symbol, status='HALTED' if halted else 'NO_ENTRY'))
                continue
            result = quarter_path(quarters[symbol], scenario, capital, peak, days)
            capital, peak = result['end_capital'], result['end_peak']
            halted = result['risk_reason'] is not None
            unknown = unknown or result['risk_status'] == 'UNKNOWN'
            paths.append(dict(symbol=symbol, status='CONDITIONAL_MODEL_PATH', ledger=result))
        portfolios.append(dict(scenario=scenario, net=capital-CAPITAL,
            return_percent=(capital/CAPITAL-1)*100, halted=halted, risk_unknown=unknown, paths=paths))
    return dict(holding_days=days, status=classify(portfolios, mask), decisions=mask, diagnostics=diagnostics,
                portfolios=portfolios, scenario_count=54, independent_quarter_ledgers=54*len(quarters),
                economic_attempts=1, training_vectors=0, candidate_qualified=False, g4=False)


def development_check(result):
    """Predeclared robustness, not selection or independent confirmation."""
    base = next(p for p in result['portfolios'] if p['scenario']['id']=='base_h8_b0_m0')
    pressure = [p for p in result['portfolios'] if p['scenario']['cost']=='stress']
    central = next(p for p in pressure if p['scenario']['id']=='stress_h8_b0_m0')
    def without_best(path):
        profits = [x['ledger']['net'] for x in path['paths'] if 'ledger' in x]
        return sum(profits)-max([0.0]+profits)
    return dict(base_positive=base['net']>0 and not base['halted'],
        all_stress_positive=all(p['net']>0 and not p['halted'] for p in pressure),
        base_without_best=without_best(base), stress_without_best=without_best(central),
        supported=base['net']>0 and not base['halted'] and
          all(p['net']>0 and not p['halted'] for p in pressure) and
          without_best(base)>0 and without_best(central)>0,
        interpretation='CONDITIONAL_DEVELOPMENT_ONLY_NO_PROMOTION')
