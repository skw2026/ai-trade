"""Offline BTC spot / inverse futures quantities and asset movements.

No account, HTTP, order submission or production adapter. Confirmed events are
explicit inputs, not fabricated exchange acknowledgments. Not a risk engine.
"""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal as D, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
from math import lcm


def decimal(x, positive=False):
    if isinstance(x, bool): raise ValueError('BOOLEAN_NUMBER')
    try: n = D(str(x))
    except InvalidOperation as exc: raise ValueError('INVALID_NUMBER') from exc
    if not n.is_finite() or n < 0 or positive and n == 0:
        raise ValueError('INVALID_NUMBER')
    return n


def rate(x):
    n = decimal(x)
    if n >= 1: raise ValueError('FEE_RATE')
    return n


@dataclass(frozen=True)
class Instrument:
    symbol: str
    unit: str
    face_usd: D
    rules: tuple

    @classmethod
    def metadata(cls, row, kind):
        if kind == 'spot':
            if (row['symbol'] != 'BTCUSDT' or row['status'] != 'TRADING'
                    or row['baseAsset'] != 'BTC' or row['quoteAsset'] != 'USDT'
                    or not row['isSpotTradingAllowed']):
                raise ValueError('SPOT_IDENTITY')
            unit, face = 'BTC', D(0)
        elif kind == 'future':
            if (row['symbol'] != 'BTCUSD_261225' or row['contractStatus'] != 'TRADING'
                    or row['baseAsset'] != 'BTC' or row['quoteAsset'] != 'USD'
                    or row['marginAsset'] != 'BTC'
                    or row['contractType'] not in ('CURRENT_QUARTER', 'NEXT_QUARTER')):
                raise ValueError('INVERSE_IDENTITY')
            unit, face = 'CONTRACTS', decimal(row['contractSize'], True)
        else: raise ValueError('INSTRUMENT_KIND')
        filters = {r['filterType']: r for r in row['filters']}
        if len(filters) != len(row['filters']) or 'LOT_SIZE' not in filters:
            raise ValueError('QUANTITY_FILTERS')
        rules = []
        for name in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
            if name in filters:
                f = filters[name]
                lo, hi, step = (decimal(f[k]) for k in ('minQty', 'maxQty', 'stepSize'))
                if hi and hi < lo: raise ValueError('QUANTITY_BOUNDS')
                rules.append((lo, hi, step))
        if not rules[0][2]: raise ValueError('LOT_STEP_REQUIRED')
        return cls(row['symbol'], unit, face, tuple(rules))

    def step(self):
        steps = [r[2] for r in self.rules if r[2]]
        quantum = D(10) ** min(x.as_tuple().exponent for x in steps)
        return D(lcm(*(int(x/quantum) for x in steps))) * quantum

    def validate(self, quantity):
        q = decimal(quantity, True)
        for lo, hi, step in self.rules:
            if q < lo or hi and q > hi or step and q % step:
                raise ValueError('QUANTITY_NOT_ADMISSIBLE')
        return q

    def fill_quantity(self, quantity):
        # Execution fragments are not new whole orders: min/max do not apply.
        q = decimal(quantity, True)
        if q % self.step(): raise ValueError('FILL_QUANTITY_GRID')
        return q

    def round_up(self, quantity):
        q = decimal(quantity, True)
        q = max(q, *(r[0] for r in self.rules))
        return self.validate((q/self.step()).to_integral_value(rounding=ROUND_CEILING)*self.step())

    def contracts(self, face_usd):
        if self.unit != 'CONTRACTS': raise ValueError('NOT_CONTRACTS')
        return self.validate(decimal(face_usd, True)/self.face_usd)


def spot_requirement(spot, face_usd, future_price, fee_asset, fee_rate='.002'):
    target = decimal(face_usd, True)/decimal(future_price, True)
    f = rate(fee_rate)
    if fee_asset not in ('BTC', 'USDT'): raise ValueError('UNSUPPORTED_FEE_ASSET')
    gross = spot.round_up(target/(1-f) if fee_asset == 'BTC' else target)
    net = gross*(1-f) if fee_asset == 'BTC' else gross
    return dict(target_btc=target, gross_btc=gross, net_btc=net,
                rounding_excess_btc=net-target, full_order_qualified=False)


class AssetLedger:
    """Atomic, idempotent conditional fills; asset locations are never doubled.

    This supplies bookkeeping, NOT exchange liquidation or order eligibility.
    Borrowed principal is not modelled; collateral eligibility is not certified.
    Confirmed deficits stay debts, never permissions to borrow or trade.
    """
    def __init__(self, spot, future, cash='10000'):
        self.spot, self.future = spot, future
        self.state = dict(usdt=decimal(cash, True), spot_btc=D(0), margin_btc=D(0),
                          transit_btc=D(0), contracts=D(0), inverse_basis=D(0),
                          fees_usdt=D(0), fees_btc=D(0), halted=False, uncertain=False,
                          pending_transfer_id=None)
        self.events = {}; self.last_ms = -1

    def apply(self, event):
        identity = event.get('id')
        if not isinstance(identity, str) or not identity: raise ValueError('EVENT_ID')
        fingerprint = json.dumps(event, sort_keys=True, allow_nan=False)
        if identity in self.events:
            if self.events[identity] != fingerprint: raise ValueError('CONFLICTING_EVENT')
            return False
        t = event.get('ms')
        if type(t) is not int or t < 0 or t < self.last_ms: raise ValueError('EVENT_CLOCK')
        s = deepcopy(self.state)
        with localcontext() as ctx:
            ctx.prec = 50
            self._apply(s, event)
        self.state = s; self.last_ms = t; self.events[identity] = fingerprint
        return True

    def check_order(self, event):
        """Conditional preflight only; never suppress a later confirmed fill."""
        s = deepcopy(self.state)
        with localcontext() as ctx:
            ctx.prec = 50
            self._apply(s, event, preflight=True)
        return dict(conditional_preflight=True, full_order_qualified=False)

    def _apply(self, s, e, preflight=False):
        kind = e['kind']
        if (s['halted'] or s['uncertain']) and kind in ('SPOT_BUY', 'SHORT_FILL'):
            if preflight: raise ValueError('INCREASING_RISK_BLOCKED')
            s['uncertain'] = True
        if kind == 'SPOT_BUY':
            q = (self.spot.validate if preflight else self.spot.fill_quantity)(e['qty'])
            p = decimal(e['price'], True); f = rate(e['fee_rate'])
            asset = e['fee_asset']
            if asset not in ('BTC', 'USDT'): raise ValueError('FEE_ASSET')
            quote_fee = q*p*f if asset == 'USDT' else D(0)
            coin_fee = q*f if asset == 'BTC' else D(0)
            if s['usdt'] < q*p+quote_fee:
                if preflight: raise ValueError('INSUFFICIENT_CASH')
                s['uncertain'] = True
            s['usdt'] -= q*p+quote_fee; s['spot_btc'] += q-coin_fee
            s['fees_usdt'] += quote_fee; s['fees_btc'] += coin_fee
        elif kind == 'TRANSFER_START':
            q = decimal(e['qty'], True)
            if s['transit_btc'] or s['spot_btc'] < q: raise ValueError('TRANSFER_BALANCE')
            s['spot_btc'] -= q; s['transit_btc'] = q; s['pending_transfer_id'] = e['id']
        elif kind in ('TRANSFER_CONFIRM', 'TRANSFER_CANCEL'):
            q = decimal(e['qty'], True)
            if q != s['transit_btc'] or e['transfer_id'] != s['pending_transfer_id']:
                raise ValueError('TRANSFER_IDENTITY')
            s['margin_btc' if kind == 'TRANSFER_CONFIRM' else 'spot_btc'] += q
            s['transit_btc'] = D(0); s['pending_transfer_id'] = None
        elif kind == 'SHORT_FILL':
            if e.get('fee_asset','BTC') != 'BTC': raise ValueError('FEE_ASSET')
            q = (self.future.validate if preflight else self.future.fill_quantity)(e['qty'])
            p = decimal(e['price'], True); f = rate(e['fee_rate'])
            if s['transit_btc'] or s['margin_btc'] <= 0:
                if preflight: raise ValueError('COLLATERAL_NOT_READY')
                s['uncertain'] = True
            coins = q*self.future.face_usd/p
            # Declared conservative accounting buffer only, not exchange tiers.
            if s['margin_btc']-coins*f < (s['contracts']+q)*self.future.face_usd/p*D('.10'):
                if preflight: raise ValueError('DECLARED_COLLATERAL_BUFFER')
                s['uncertain'] = True
            s['margin_btc'] -= coins*f; s['fees_btc'] += coins*f
            s['contracts'] += q; s['inverse_basis'] += coins
        elif kind in ('CLOSE_FILL', 'SETTLE'):
            if e.get('fee_asset','BTC') != 'BTC': raise ValueError('FEE_ASSET')
            q = s['contracts'] if kind == 'SETTLE' else (self.future.validate if preflight else self.future.fill_quantity)(e['qty'])
            p = decimal(e['price'], True); f = rate(e['fee_rate'])
            if q <= 0 or q > s['contracts']: raise ValueError('CLOSE_QUANTITY')
            entry_coins = s['inverse_basis']*q/s['contracts']
            exit_coins = q*self.future.face_usd/p
            s['margin_btc'] += exit_coins-entry_coins-exit_coins*f
            s['fees_btc'] += exit_coins*f; s['contracts'] -= q
            s['inverse_basis'] = s['inverse_basis']-entry_coins if s['contracts'] else D(0)
            if s['margin_btc'] < 0: s['uncertain'] = True
        elif kind == 'RETURN_MARGIN_CONFIRMED':
            if s['contracts'] or s['transit_btc'] or s['margin_btc'] < 0:
                raise ValueError('MARGIN_NOT_RETURNABLE')
            s['spot_btc'] += s['margin_btc']; s['margin_btc'] = D(0)
        elif kind == 'SPOT_SELL':
            if e.get('fee_asset','USDT') != 'USDT': raise ValueError('FEE_ASSET')
            q = (self.spot.validate if preflight else self.spot.fill_quantity)(e['qty'])
            p = decimal(e['price'], True); f = rate(e['fee_rate'])
            if q > s['spot_btc']: raise ValueError('SPOT_BALANCE')
            s['spot_btc'] -= q; s['usdt'] += q*p*(1-f); s['fees_usdt'] += q*p*f
        elif kind == 'RISK_STOP': s['halted'] = True
        elif kind in ('MARK_UNKNOWN', 'SECOND_LEG_REJECTED'): s['uncertain'] = True
        else: raise ValueError('EVENT_KIND')

    def equity(self, spot_price, future_mark):
        s = self.state
        coins = s['spot_btc']+s['margin_btc']+s['transit_btc']
        if s['contracts']:
            coins += s['contracts']*self.future.face_usd/decimal(future_mark, True)-s['inverse_basis']
        return s['usdt']+coins*decimal(spot_price, True)

    def liquidation_quantity(self):
        q = self.state['spot_btc']; step = self.spot.step()
        sell = (q/step).to_integral_value(rounding=ROUND_FLOOR)*step
        if sell:
            try: self.spot.validate(sell)
            except ValueError: sell = D(0)
        return sell, q-sell

    def snapshot(self):
        return dict(balances={k:str(v) if isinstance(v, D) else v for k,v in self.state.items()},
                    actual_execution_qualified=False, risk_engine_qualified=False,
                    account_permission_verified=False, g4=False)
