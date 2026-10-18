"""Exact conditional accounting boundaries, not a new strategy or guarantee.

Uses the frozen pressure fees. No account, market-file, order or promotion API.
The interval calculation is a counterexample/identification diagnostic: it does
not impose bounded worst-case profit as a new product qualification criterion.
"""
from fractions import Fraction as F

N = F(2500)
SF, FF, Z = F(2, 1000), F(1, 1000), F(1, 1000)


def positive(value):
    x = F(str(value))
    if x <= 0:
        raise ValueError('POSITIVE_FINITE_INPUT_REQUIRED')
    return x


def nonnegative(value):
    x = F(str(value))
    if x < 0:
        raise ValueError('NONNEGATIVE_INPUT_REQUIRED')
    return x


def entry(spot, future):
    s, f = positive(spot), positive(future)
    q = N / (f * (1-Z))
    return dict(btc_quantity=q, spent=q*s*(1+Z)*(1+SF), fee_btc=q*FF)


def delivery_cash(spot, future, exit_spot, index, *, usdt_owed=0, btc_owed=0):
    e = entry(spot, future)
    s, i = positive(exit_spot), positive(index)
    usd, btc = nonnegative(usdt_owed), nonnegative(btc_owed)
    # USD-equivalent of the final signed BTC balance, before spot liquidation.
    value = N*(1-FF)*(s/i) - (e['fee_btc']+btc)*s
    multiplier = (1-Z)*(1-SF) if value >= 0 else (1+Z)*(1+SF)
    return value*multiplier-e['spent']-usd


def conditional_lower_bound(spot, future, *, spot_max=None, index_ratio_max=None,
                            usdt_owed_max=None, btc_owed_max=None):
    """Exact rectangle minimum if ALL stated bounds hold and delivery is reached.

    Let x=S/I and y=S. V=N(1-ff)x-(fee_in+Lbtc)y. Liquidation
    g(V) is continuous/increasing on both signed branches. Therefore the minimum
    is at smallest x, largest y and largest liabilities. No path guarantee follows.
    """
    if any(v is None for v in (spot_max, index_ratio_max, usdt_owed_max, btc_owed_max)):
        return dict(status='UNBOUND', lower_cash=None, market_certified=False)
    s, r = positive(spot_max), positive(index_ratio_max)
    lower = delivery_cash(spot, future, s, s*r, usdt_owed=usdt_owed_max, btc_owed=btc_owed_max)
    return dict(status='CONDITIONAL_MODEL_ONLY', lower_cash=lower,
                positive=lower > 0, market_certified=False)


def required_index_ratio(spot, future, exit_spot, *, target_cash=0, usdt_owed=0, btc_owed=0):
    """Largest I/S compatible with positive-coin cash target; not a forecast."""
    e = entry(spot, future)
    s = positive(exit_spot)
    target = nonnegative(target_cash) + nonnegative(usdt_owed)
    d = e['fee_btc'] + nonnegative(btc_owed)
    k = (1-Z)*(1-SF)
    return N*(1-FF)/(d*s+(e['spent']+target)/k)


def proof_obligations():
    return {
        'entry_cash_cost': 'Observed executable prices, full fees, size precision and causal two-leg fills',
        'settlement_conversion': 'Joint exit spot/index/FX distribution; +25bps is a scenario, not an attested bound',
        'coin_liabilities': 'BTC commissions and financing, including balance after delivery until actual spot disposal',
        'path_risk': 'Full-life mark/spot/account observations and actual risk-exit mechanics; terminal result alone insufficient',
        'economic_confirmation': 'Frozen effect, dependence/tails, selection control, independent inputs and affordable endpoint',
        'worst_case_guarantee_required_by_product': False,
        'new_economic_admission': False,
    }
