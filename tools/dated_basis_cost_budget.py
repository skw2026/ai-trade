"""Cost-capacity identities, not a strategy runner or a fee entitlement oracle.

All prices/timing are held fixed. A fee floor assumes nonnegative futures fees,
no BNB acquisition/holding cost, and unchanged market slippage. Maker rebates,
different fills or different exits invalidate that scope rather than pass it.
"""
from decimal import Decimal, localcontext


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('MISSING_OR_BOOLEAN_INPUT')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('NONFINITE_INPUT')
    return result


def cost_capacity(*, face, spot_spend_without_fee, terminal_value_without_fee,
                  gross, slippage, annual, held_days, spot_rate):
    """Return budgets only; no revised policy, portfolio, or trade eligibility.

    B is spot spend after entry slippage, A is fee-free terminal sale proceeds.
    For nonnegative futures fees with terminal value X, total fee cost in the
    previous received-asset ledger is s*(A+B)+(1-s)*X >= s*(A+B).
    Charging spot fees in BNB does not remove their economic cost. A conservative
    lower floor also excludes financing the spot fee itself.
    """
    with localcontext() as context:
        context.prec = 40
        d, b, a, g, slip, rate, days, s = map(number, (
            face, spot_spend_without_fee, terminal_value_without_fee,
            gross, slippage, annual, held_days, spot_rate))
        if min(d, b, a) <= 0 or min(rate, days, slip) < 0 or not 0 <= s < 1:
            raise ValueError('OUT_OF_SCOPE_INPUT')
        if abs((a-b)-(g-slip)) > Decimal('0.0000001'):
            raise ValueError('CASH_IDENTITY_MISMATCH')
        carry = b*rate*days/365
        budget = g-slip-carry
        fee_floor = s*(a+b)
        scale = Decimal(10000)/d
        floor = slip+carry+fee_floor
        return dict(gross_capacity_bps=g*scale,
                    slippage_bps=slip*scale,
                    capital_floor_bps=carry*scale,
                    fee_budget_bps=budget*scale,
                    spot_fee_floor_bps=fee_floor*scale,
                    all_in_cost_floor_bps=floor*scale,
                    required_cost_or_gross_improvement_bps=max(Decimal(0),floor-g)*scale,
                    maximum_spot_rate_for_nonnegative_budget=budget/(a+b),
                    floor_exceeds_budget=floor>g,
                    equal_is_not_positive_advantage=floor==g)


def early_exit_fee_budget(*, settlement_rate, closing_rate, close_slippage_rate):
    """Normalized fee/slippage allowance for residual basis, not exit profit.

    Uses equal notional only. Real inverse cash flows and changed prices need a
    new execution experiment. Missing rates fail; no maker rate is hardcoded.
    """
    settlement, closing, slip = map(number, (settlement_rate, closing_rate,
                                           close_slippage_rate))
    if min(settlement, closing, slip) < 0:
        raise ValueError('NEGATIVE_RATE_OR_REBATE_REQUIRES_NEW_SCOPE')
    return (settlement-closing-slip)*10000
