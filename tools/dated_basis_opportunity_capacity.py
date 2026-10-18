"""Past-quote-only capacity diagnostic. No future payoff, policy or orders."""
import datetime as dt
import math
import statistics

BAR = 300000
DAY = 86400000
YEAR = 365*DAY
FACE = 2500.0
EPS = 1e-9
LAYERS = {
    'base_cash': (.001, .0005, .0002, 0., 0., 0),
    'base_economic': (.001, .0005, .0002, .06, 0., 0),
    'stress_cash': (.002, .001, .001, 0., 0., 0),
    'stress_economic': (.002, .001, .001, .12, 0., 0),
    'stress_terminal_cash': (.002, .001, .001, 0., .0025, 3600000),
    'stress_terminal_economic': (.002, .001, .001, .12, .0025, 3600000),
}
STRONG = 'stress_terminal_economic'
BUCKETS = ('1_to_7', '7_to_30', '30_to_60', '60_to_90')


def amount(spot, future, held_ms, layer):
    """Flat-spot conditional cash budget; does not receive terminal market data."""
    if layer not in LAYERS or not all(math.isfinite(x) and x > 0 for x in (spot, future)):
        raise ValueError('PRICE_OR_LAYER')
    if not math.isfinite(held_ms) or held_ms <= 0:
        raise ValueError('HOLD_TIME')
    sfee, ffee, slip, annual, bias, delay = LAYERS[layer]
    entry_f = future*(1-slip)
    q = FACE/entry_f
    spent = q*spot*(1+slip)*(1+sfee)
    terminal = spot*(1+bias)
    wallet = q*(1-ffee)+FACE*(1/terminal-1/entry_f)-FACE/terminal*ffee
    proceeds = wallet*spot*(1-slip)*(1-sfee)
    return proceeds-spent-spent*annual*(held_ms+delay)/YEAR


def bucket(remaining_ms):
    if not DAY <= remaining_ms <= 90*DAY:
        raise ValueError('REMAINING_DOMAIN')
    return ('1_to_7' if remaining_ms < 7*DAY else '7_to_30' if remaining_ms < 30*DAY
            else '30_to_60' if remaining_ms < 60*DAY else '60_to_90')


def project(bundle):
    """Discard settlement, marks, OHLC extremes and all future execution prices."""
    result = {}
    for symbol, quarter in sorted(bundle.items()):
        expiry = quarter['expiry']
        series = {kind: {int(t): b for t,b in quarter['series'][kind].items()}
                  for kind in ('spot', 'future')}
        rows = []
        for t in range(expiry-90*DAY-BAR, expiry-DAY-BAR+BAR, BAR):
            reasons = []
            prices = {}
            for kind in ('spot', 'future'):
                bar = series[kind].get(t)
                if bar is None:
                    reasons.append(kind+'_missing')
                    continue
                if not bar.get('complete', False) or bar.get('close_ms') != t+BAR-1:
                    reasons.append(kind+'_incomplete')
                if not math.isfinite(float(bar['v'])) or float(bar['v']) <= 0 or bar['n'] <= 0:
                    reasons.append(kind+'_no_trades')
                price = float(bar['c'])
                if not math.isfinite(price) or price <= 0:
                    raise ValueError('INVALID_QUOTE_PRICE')
                prices[kind] = str(bar['c'])
            rows.append(dict(t=t, expiry=expiry, reasons=reasons, **prices))
        result[symbol] = rows
    return result


def longest_runs(times):
    lengths = []
    previous = None
    for t in times:
        if previous is not None and t <= previous:
            raise ValueError('UNSORTED_OR_DUPLICATE')
        if previous is not None and t == previous+BAR:
            lengths[-1] += 1
        else:
            lengths.append(1)
        previous = t
    return lengths


def summarize(rows, layer, start):
    valid = [r for r in rows if r['values'] is not None]
    positive = [r for r in valid if r['values'][layer] > EPS]
    values = sorted(r['values'][layer] for r in valid)
    positive_values = [r['values'][layer] for r in positive]
    times = [r['t'] for r in positive]
    runs = longest_runs(times)
    return dict(total_quotes=len(rows), valid_quotes=len(valid), positive_quotes=len(positive),
        positive_fraction=len(positive)/len(valid) if valid else None,
        positive_utc_days=len({(t+BAR)//DAY for t in times}),
        positive_7day_blocks=len({(t-start)//(7*DAY) for t in times}),
        runs=len(runs), runs_at_least_1h=sum(n >= 12 for n in runs),
        runs_at_least_24h=sum(n >= 288 for n in runs), longest_run_bars=max(runs, default=0),
        margin_min=min(values, default=None), margin_median=statistics.median(values) if values else None,
        margin_max=max(values, default=None),
        positive_margin_median=statistics.median(positive_values) if positive_values else None,
        first_positive_ms=min(times, default=None), last_positive_ms=max(times, default=None))


def classify(quarters):
    positive = sum(q['layers'][STRONG]['positive_quotes'] for q in quarters.values())
    persistent = sum(q['layers'][STRONG]['runs_at_least_1h'] > 0 for q in quarters.values())
    return ('NO_OBSERVED_PRESSURE_HEADROOM' if positive == 0 else
            'TRANSIENT_OR_SINGLE_EXPIRY_ONLY' if persistent < 2 else
            'MULTI_EXPIRY_PERSISTENT_HEADROOM')


def scan(projection):
    ledgers = {}
    summaries = {}
    for symbol, source in sorted(projection.items()):
        rows = []
        reasons = {}
        for row in source:
            t, expiry = row['t'], row['expiry']
            for reason in row['reasons']:
                reasons[reason] = reasons.get(reason, 0)+1
            remaining = expiry-t-BAR
            values = (None if row['reasons'] else
                      {key: amount(float(row['spot']), float(row['future']), expiry-t-2*BAR, key)
                       for key in LAYERS})
            rows.append(dict(t=t, bucket=bucket(remaining), values=values))
        ledgers[symbol] = rows
        start = source[0]['expiry']-90*DAY-BAR
        summaries[symbol] = dict(reasons=reasons,
            layers={key: summarize(rows,key,start) for key in LAYERS},
            maturity_buckets={b: {key: summarize([r for r in rows if r['bucket']==b],key,start)
                                  for key in LAYERS} for b in BUCKETS},
            fixed_calendar_points={str(days): next(r for r in rows if r['t']==source[0]['expiry']-days*DAY-BAR)
                                   for days in (30,60,90)})
    return dict(status=classify(summaries), quarters=summaries,
        observed_expiry_events=len(summaries), independent_events_not_established=True,
        economic_attempts=0, training_vectors=0, candidate_qualified=False, g4=False), ledgers
