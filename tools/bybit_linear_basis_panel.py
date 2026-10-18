"""Read-only arithmetic for one frozen public spot / linear expiry snapshot panel.

No network, account, order, optimization or production integration. Decimal
strings are retained; positive conditional cash is never a trading qualification.
"""
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR, localcontext
from itertools import product

SYMBOLS = tuple('BTCUSDT-' + x for x in (
    '23OCT26', '30OCT26', '06NOV26', '27NOV26', '25DEC26',
    '26MAR27', '25JUN27', '24SEP27'))
COSTS = {'base': ('0.001', '0.00055', '0.0002'),
         'stress': ('0.002', '0.001', '0.001')}


def decimal(value):
    if isinstance(value, (float, bool)):
        raise ValueError('NON_DECIMAL_INPUT')
    out = D(value)
    if not out.is_finite():
        raise ValueError('NON_FINITE')
    return out


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def metadata(payload, symbols):
    require(type(payload.get('retCode')) is int and payload['retCode'] == 0, 'METADATA_ERROR')
    records = payload['result']['list']
    selected = {}
    for symbol in symbols:
        rows = [r for r in records if r.get('symbol') == symbol]
        require(len(rows) == 1, 'METADATA_IDENTITY')
        r = rows[0]
        require(all(r.get(k) == v for k, v in dict(
            contractType='LinearFutures', baseCoin='BTC', quoteCoin='USDT',
            settleCoin='USDT', status='Trading').items()), 'METADATA_PRODUCT')
        require(r.get('fundingInterval') == 0 and int(r['deliveryTime']) > 0,
                'METADATA_EXPIRY')
        lot = r['lotSizeFilter']
        require(decimal(lot['qtyStep']) == D('.001') and
                decimal(lot['minOrderQty']) == D('.001'), 'METADATA_GRID')
        require(decimal(r['deliveryFeeRate']) in (D(0), D('.0005')),
                'UNMODELED_DELIVERY_FEE')
        selected[symbol] = r
    return selected


def book(payload, symbol, received_ms):
    require(type(payload.get('retCode')) is int and payload['retCode'] == 0, 'BOOK_ERROR')
    r = payload['result']
    require(r['s'] == symbol, 'BOOK_IDENTITY')
    out = {'symbol': symbol, 'received_ms': int(received_ms)}
    for k in ('cts', 'ts', 'u', 'seq'):
        require(type(r[k]) is int and r[k] > 0, 'BOOK_CLOCK_OR_SEQUENCE')
        out[k] = r[k]
    require(type(payload['time']) is int and payload['time'] > 0, 'SERVER_CLOCK')
    out['server_ms'] = payload['time']
    require(out['cts'] <= out['ts'] <= out['server_ms'], 'BOOK_CLOCK_ORDER')
    for key, sign in (('b', -1), ('a', 1)):
        rows = r[key]
        require(isinstance(rows, list), 'BOOK_LEVEL_SCHEMA')
        out[key] = []
        for pair in rows:
            require(isinstance(pair, list) and len(pair) == 2, 'BOOK_LEVEL_SCHEMA')
            p, q = map(decimal, pair)
            require(p > 0 and q > 0, 'BOOK_NONPOSITIVE')
            if out[key]:
                require(sign * (p - out[key][-1][0]) > 0, 'BOOK_ORDER')
            out[key].append((p, q))
    if out['a'] and out['b']:
        require(out['b'][0][0] < out['a'][0][0], 'CROSSED_BOOK')
    return out


def pair_reasons(spot, future):
    reasons = []
    for name, b in (('spot', spot), ('future', future)):
        if not b['a'] or not b['b']:
            reasons.append(name + ':EMPTY_BOOK')
        if not 0 <= b['server_ms'] - b['cts'] <= 1000:
            reasons.append(name + ':STALE_SERVER')
        if not 0 <= b['received_ms'] - b['cts'] <= 1000:
            reasons.append(name + ':STALE_RECEIVE')
    if abs(spot['cts'] - future['cts']) > 500:
        reasons.append('EVENT_SKEW')
    if abs(spot['received_ms'] - future['received_ms']) > 1000:
        reasons.append('RECEIVE_SKEW')
    return reasons


def sweep(rows, quantity):
    remaining, cash = quantity, D(0)
    require(quantity > 0, 'QUANTITY')
    for p, q in rows:
        taken = min(q, remaining)
        cash += p * taken
        remaining -= taken
        if remaining == 0:
            return cash
    raise ValueError('DEPTH_INSUFFICIENT')


def quantities(ask, spot_fee):
    q = (D(2500) / ask / D('.001')).to_integral_value(rounding=ROUND_FLOOR) * D('.001')
    require(q > 0, 'MINIMUM_QUANTITY')
    g = (q / (1 - spot_fee) / D('.000001')).to_integral_value(
        rounding=ROUND_CEILING) * D('.000001')
    return q, g, g * (1 - spot_fee)


def cash_case(q, gross, net, spot_spend, future_price, future_fee,
              spot_fee, slip, mid, days, multiple, deviation, delivery):
    """One conditional terminal cash ledger, not a margin-path simulation."""
    index = mid * multiple
    sell_qty = (net / D('.000001')).to_integral_value(rounding=ROUND_FLOOR) * D('.000001')
    remaining_btc = net - sell_qty
    sale = sell_qty * index * (1 + deviation) * (1 - slip) * (1 - spot_fee)
    open_fee = q * future_price * future_fee
    settle_fee = q * index * delivery
    pnl = q * (future_price - index)
    margin = q * future_price
    buffer = q * 2 * mid * D('.0025')
    occupied = spot_spend + margin + open_fee + buffer
    profit = sale + pnl - spot_spend - open_fee - settle_fee
    # A reference end-point test only. No spot gains or outside buffer top up M.
    headroom = margin + pnl - settle_fee - D('.10') * q * index
    result = dict(profit=profit, initial_margin_reference=margin, fee_buffer=buffer,
                  occupied_capital=occupied, remaining_capital=D(10000) - occupied,
                  future_entry_fee=open_fee, delivery_fee=settle_fee,
                  future_pnl=pnl, spot_exit_net=sale, index=index,
                  terminal_margin_headroom=headroom, net_btc=net, dust_btc=net-q,
                  sold_btc=sell_qty, remaining_btc=remaining_btc,
                  remaining_btc_reference_value=remaining_btc*index,
                  gross_spot_btc=gross, spot_spend=spot_spend, future_price=future_price,
                  q=q, days=days, index_multiple=multiple, deviation=deviation,
                  delivery_rate=delivery)
    for rate in ('0.06', '0.12'):
        label = str(int(D(rate) * 100))
        result['excess_occupied_' + label] = profit - occupied * D(rate) * days / 365
        result['excess_total_' + label] = profit - D(10000) * D(rate) * days / 365
    return {k: str(v) for k, v in result.items()}


def analyze_pair(spot, future, instrument):
    require(spot['symbol'] == 'BTCUSDT' and instrument['symbol'] in SYMBOLS and
            future['symbol'] == instrument['symbol'], 'PAIR_IDENTITY')
    result = dict(symbol=instrument['symbol'], status='INPUT_INSUFFICIENT',
                  reasons=pair_reasons(spot, future), cases=[], path_validated=False,
                  actual_fills=False, qualified=False)
    if result['reasons']:
        return result
    with localcontext() as ctx:
        ctx.prec = 40
        mid = (spot['a'][0][0] + spot['b'][0][0]) / 2
        # The later matching-engine time is the panel reference instant.
        start = max(spot['cts'], future['cts'])
        days = D(int(instrument['deliveryTime']) - start) / 86400000
        require(days > 0, 'EXPIRED_INSTRUMENT')
        for name, fees in COSTS.items():
            s, f, z = map(D, fees)
            q, gross, net = quantities(spot['a'][0][0], s)
            require(gross <= 120, 'SPOT_MAXIMUM_QUANTITY')
            lot = instrument['lotSizeFilter']
            require(q <= min(decimal(lot['maxMktOrderQty']), decimal(lot['maxOrderQty'])),
                    'FUTURE_MAXIMUM_QUANTITY')
            try:
                spend = sweep(spot['a'], gross) * (1 + z)
                price = sweep(future['b'], q) / q * (1 - z)
            except ValueError as error:
                if str(error) != 'DEPTH_INSUFFICIENT':
                    raise
                result['reasons'].append(name + ':DEPTH_INSUFFICIENT')
                continue
            require(spend >= 5 and q * price >= decimal(lot['minNotionalValue']), 'MIN_NOTIONAL')
            for mult, dev, delivery in product(map(D, ('0.5', '1', '2')),
                                               map(D, ('-.0025', '0', '.0025')),
                                               map(D, ('0', '.0005'))):
                case = cash_case(q, gross, net, spend, price, f, s, z, mid, days,
                                 mult, dev, delivery)
                case['cost'] = name
                result['cases'].append(case)
        if result['reasons']:
            return result  # Never turn partial depth into a complete pass.
        cases = result['cases']
        stress = [c for c in cases if c['cost'] == 'stress']
        require(len(cases) == 36 and len(stress) == 18, 'SCENARIO_COUNT')
        result['cash_range'] = [str(min(D(c['profit']) for c in cases)),
                                str(max(D(c['profit']) for c in cases))]
        result['stress_cash_range'] = [str(min(D(c['profit']) for c in stress)),
                                       str(max(D(c['profit']) for c in stress))]
        result['terminal_margin_failure_cases'] = sum(D(c['terminal_margin_headroom']) < 0 for c in cases)
        result['capital_budget_ok'] = all(D(c['remaining_capital']) >= 0 for c in cases)
        if max(D(c['profit']) for c in cases) <= 0:
            result['status'] = 'NO_CURRENT_CASH_MARGIN'
        elif min(D(c['profit']) for c in stress) > 0 and result['capital_budget_ok']:
            result['status'] = 'CONDITIONAL_CASH_MARGIN'
        else:
            result['status'] = 'CONDITIONAL_ONLY'
        return result
