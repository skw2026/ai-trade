"""Research-only cash ledger, benchmark ledger and frozen causal intention rule.

No market-file runner, account connection, promotion or order interface. Funding
declarations describe model inputs; they cannot attest to an actual account.
"""
from dataclasses import dataclass
import math

BAR = 300000
DAY = 86400000
YEAR = 365 * DAY
FACE = 2500.0
COSTS = {'base': (.001, .0005, .0002), 'stress': (.002, .001, .001)}
BENCHMARKS = (.06, .12)


def number(value, name, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(name)
    return float(value)


def stamp(value):
    if type(value) is not int or value < 0 or value % BAR:
        raise ValueError('BAR_CLOCK')
    return value


def price(value):
    if number(value, 'PRICE') <= 0:
        raise ValueError('PRICE')
    return float(value)


@dataclass(frozen=True)
class CashState:
    equity: float = 10000.0
    peak: float = 10000.0
    halted: bool = False
    uncertain: bool = False


@dataclass(frozen=True)
class Liability:
    """A modeled accrued financing charge, not an opportunity-cost debit."""
    event_id: str
    time_ms: int
    usdt: float = 0.0
    btc: float = 0.0


class Position:
    def __init__(self, state, *, entry_ms, spot, future, cost='stress',
                 financing_status, opening_debt_usdt=0, opening_debt_btc=0,
                 liabilities=()):
        if financing_status != 'DECLARED_SELF_FINANCED_MODEL':
            raise ValueError('UNKNOWN_OR_UNSUPPORTED_FINANCING')
        if number(opening_debt_usdt, 'DEBT') or number(opening_debt_btc, 'DEBT'):
            raise ValueError('BORROWED_PRINCIPAL_NOT_SUPPORTED')
        if type(state.halted) is not bool or type(state.uncertain) is not bool:
            raise ValueError('STATE_FLAGS')
        if state.halted or state.uncertain:
            raise ValueError('PATH_CANNOT_REENTER')
        number(state.equity, 'EQUITY'); number(state.peak, 'PEAK')
        if state.equity <= 0 or state.peak < state.equity:
            raise ValueError('CASH_STATE')
        if (state.peak - state.equity) / state.peak >= .08:
            raise ValueError('INHERITED_HARD_RISK')
        self.entry = stamp(entry_ms)
        self.raw_spot, self.raw_future = price(spot), price(future)
        if cost not in COSTS:
            raise ValueError('COST_PROFILE')
        self.sf, self.ff, self.slip = COSTS[cost]
        self.sin = self.raw_spot * (1 + self.slip)
        self.fin = self.raw_future * (1 - self.slip)
        self.quantity = FACE / self.fin
        self.spot_fee_in = self.quantity * self.sin * self.sf
        self.spent = self.quantity * self.sin + self.spot_fee_in
        self.cash = state.equity - self.spent
        if self.cash < 0:
            raise ValueError('INSUFFICIENT_SELF_FINANCED_CASH')
        self.fee_in_btc = self.quantity * self.ff
        self.wallet = self.quantity - self.fee_in_btc
        self.initial = state
        self.peak = state.peak
        self.max_dd = 0.0
        self.min_margin = None
        self.reason = None
        self.pending_exit_ms = None
        self.uncertain = False
        self.closed = False
        self.last_ms = self.entry
        self.liabilities = tuple(liabilities)
        ids = set()
        previous = self.entry
        for item in self.liabilities:
            if not isinstance(item.event_id, str) or not item.event_id or item.event_id in ids:
                raise ValueError('LIABILITY_ID')
            ids.add(item.event_id)
            if stamp(item.time_ms) < previous:
                raise ValueError('LIABILITY_ORDER')
            previous = item.time_ms
            number(item.usdt, 'LIABILITY_USDT'); number(item.btc, 'LIABILITY_BTC')

    def _clock(self, t):
        stamp(t)
        if self.closed or t < self.last_ms:
            raise ValueError('CLOSED_OR_REVERSED_CLOCK')
        self.last_ms = t

    def owed(self, t):
        return (sum(x.usdt for x in self.liabilities if x.time_ms <= t),
                sum(x.btc for x in self.liabilities if x.time_ms <= t))

    def _spot_cash(self, coins, raw):
        # A negative balance must be bought back at ask, including fees.
        execution = raw * (1 - self.slip if coins >= 0 else 1 + self.slip)
        fee = abs(coins) * execution * self.sf
        return coins * execution - fee, execution, fee

    def _observe(self, values, margin, t):
        if not all(math.isfinite(v) for v in values) or margin is not None and not math.isfinite(margin):
            raise ValueError('NONFINITE_VALUATION')
        self.peak = max(self.peak, max(values))
        self.max_dd = max(self.max_dd, (self.peak - min(values)) / self.peak)
        if margin is not None:
            self.min_margin = margin if self.min_margin is None else min(self.min_margin, margin)
        if self.reason is None and (self.max_dd >= .08 or margin is not None and margin <= 2):
            self.reason = 'DRAWDOWN_8_PERCENT' if self.max_dd >= .08 else 'MARGIN_BUFFER'
            self.pending_exit_ms = t + BAR

    def observe_bar(self, t, *, spot_low, spot_high, mark_low, mark_high):
        """t is the bar START; information is available at its end, not at t."""
        if stamp(t) < self.last_ms:
            raise ValueError('OVERLAPPING_OR_PREENTRY_BAR')
        bounds = list(map(price, (spot_low, spot_high, mark_low, mark_high)))
        if bounds[0] > bounds[1] or bounds[2] > bounds[3]:
            raise ValueError('OHLC_ORDER')
        if t > self.last_ms:
            self.uncertain = True
            if self.pending_exit_ms is None:
                self.pending_exit_ms = t + BAR
        self._clock(t + BAR)
        usd, btc = self.owed(t + BAR)
        values, ratios = [], []
        for m in bounds[2:]:
            equity_btc = self.wallet + FACE * (1 / m - 1 / self.fin) - btc
            ratios.append(equity_btc / (.05 * FACE / m))
            close = m * (1 + self.slip)
            coins = self.wallet + FACE * (1 / close - 1 / self.fin) - FACE / close * self.ff - btc
            values.extend(self.cash - usd + self._spot_cash(coins, s)[0] for s in bounds[:2])
        self._observe(values, min(ratios), t)
        return dict(cash_low=min(values), cash_high=max(values), margin=min(ratios),
                    cash_drawdown=self.max_dd, reason=self.reason, pending_exit_ms=self.pending_exit_ms)

    def missing_risk_bar(self, t):
        if stamp(t) < self.last_ms:
            raise ValueError('OVERLAPPING_OR_PREENTRY_BAR')
        self._clock(t + BAR)
        self.uncertain = True
        if self.pending_exit_ms is None:
            self.pending_exit_ms = t + BAR

    def benchmark(self, exit_ms):
        if type(exit_ms) is not int or exit_ms < self.entry:
            raise ValueError('BENCHMARK_CLOCK')
        return {str(rate): self.spent * rate * (exit_ms - self.entry) / YEAR for rate in BENCHMARKS}

    def close(self, t, *, spot, future, settlement=False):
        """Given fill proxies only; no invented fill on missing quotes.

        settlement=True models the delivery fee without trade slippage on the
        future leg. Delayed post-settlement BTC risk needs a full replay adapter.
        """
        raw_out, raw_fout = price(spot), price(future)
        if type(settlement) is not bool:
            raise ValueError('SETTLEMENT_FLAG')
        if t < self.entry + BAR or (self.pending_exit_ms is not None and t < self.pending_exit_ms):
            raise ValueError('EXIT_BEFORE_CAUSAL_FILL')
        if t > self.last_ms:
            self.uncertain = True
        self._clock(t)
        usd, btc = self.owed(t)
        fout = raw_fout if settlement else raw_fout * (1 + self.slip)
        fee_out_btc = FACE / fout * self.ff
        wallet = self.wallet + FACE * (1 / fout - 1 / self.fin) - fee_out_btc - btc
        proceeds, sout, spot_fee_out = self._spot_cash(wallet, raw_out)
        final = self.cash + proceeds - usd
        self._observe([final], None, t - BAR)
        gross = FACE * raw_out / raw_fout - FACE * self.raw_spot / self.raw_future
        no_fees = FACE * sout / fout - FACE * self.sin / self.fin
        fees = self.spot_fee_in + spot_fee_out + (self.fee_in_btc + fee_out_btc) * sout
        financing = usd + btc * sout
        net = final - self.initial.equity
        if not math.isclose(net, gross - (gross - no_fees) - fees - financing, abs_tol=1e-8):
            raise ValueError('CASH_IDENTITY')
        self.closed = True
        state = CashState(final, self.peak, self.reason is not None, self.uncertain)
        charges = self.benchmark(t)
        return dict(cash_state=state, cash_net=net, gross=gross, slippage_drag=gross-no_fees,
                    execution_fees=fees, financing_cost=financing, benchmark_charges=charges,
                    benchmark_excess={k: net-v for k, v in charges.items()},
                    cash_drawdown=self.max_dd, minimum_margin_ratio=self.min_margin,
                    risk_reason=self.reason, exit_ms=t, spot_spend=self.spent,
                    risk_status='UNKNOWN' if self.uncertain else 'MODEL_OBSERVED_ONLY',
                    actual_execution_qualified=False, candidate_qualified=False, g4=False)


def pressure_forecast(spot, future):
    """Flat-spot, +25bps settlement conversion assumption; past quotes only."""
    s, f = price(spot), price(future)
    sf, ff, slip = COSTS['stress']
    fin = f * (1 - slip)
    quantity = FACE / fin
    spent = quantity * s * (1 + slip) * (1 + sf)
    assumed_index = s * 1.0025
    terminal = quantity * (1 - ff) + FACE * (1 / assumed_index - 1 / fin) - FACE / assumed_index * ff
    result = terminal * s * (1 - slip) * (1 - sf) - spent
    if not math.isfinite(result):
        raise ValueError('NONFINITE_FORECAST')
    return result


def first_opportunity(quotes, *, expiry, observed_through):
    """Quotes: {t, spot, future, valid}; projection must strip future information.

    No choice among dates: the first completed qualifying run is terminal. A
    resulting intention is not an execution, permission, or evidence of profit.
    """
    stamp(expiry); stamp(observed_through)
    previous = None
    streak = 0
    for row in quotes:
        t = stamp(row['t'])
        if previous is not None and t <= previous:
            raise ValueError('UNORDERED_OR_DUPLICATE_QUOTE')
        if type(row['valid']) is not bool:
            raise ValueError('QUOTE_VALIDITY')
        if t + BAR > observed_through:
            return None
        contiguous = previous is not None and t == previous + BAR
        previous = t
        in_domain = expiry - 90 * DAY - BAR <= t <= expiry - DAY - BAR
        positive = in_domain and row['valid'] and pressure_forecast(row['spot'], row['future']) > 1e-9
        streak = (streak + 1 if contiguous else 1) if positive else 0
        if streak == 12:
            return dict(decision_bar=t, available_ms=t+BAR, earliest_entry_ms=t+2*BAR,
                        forecast_cash=pressure_forecast(row['spot'], row['future']),
                        policy='FIRST_12_STRESS_CASH_POSITIVE', actual_execution_qualified=False)
    return None
