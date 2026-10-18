"""Sequence-aware quote diagnostic; no orders, policy scan or realized payoff."""
import argparse
from collections import Counter
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import statistics

from dated_basis_dual_ledger import pressure_forecast

D = Decimal


def levels(rows):
    result = {}
    for p, q in rows:
        p, q = D(p), D(q)
        if not p.is_finite() or not q.is_finite() or p <= 0 or q < 0 or p in result:
            raise ValueError('INVALID_LEVEL')
        result[p] = q
    return result


class Book:
    def __init__(self, kind):
        self.kind = kind
        self.last = None
        self.bridged = False
        self.event = None
        self.buffer = []
        self.applied = 0
        self.discarded = 0

    def snapshot(self, data):
        if self.last is not None:
            raise ValueError('SECOND_SNAPSHOT')
        self.bids, self.asks = levels(data['bids']), levels(data['asks'])
        if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
            raise ValueError('INVALID_SNAPSHOT')
        self.bid_floor, self.ask_ceiling = min(self.bids), max(self.asks)
        self.last = data['lastUpdateId']
        for event in self.buffer:
            self.update(event)
        self.buffer = []

    def update(self, row):
        if self.last is None:
            self.buffer.append(row)
            return
        data = json.loads(row['payload']['raw'])
        if data['u'] < data['U']:
            raise ValueError('REVERSED_SEQUENCE')
        if data['u'] < self.last or (self.kind == 'spot' and data['u'] == self.last):
            self.discarded += 1
            return
        if not self.bridged:
            target = self.last + (1 if self.kind == 'spot' else 0)
            if not data['U'] <= target <= data['u']:
                raise ValueError('SNAPSHOT_BRIDGE_GAP_' + self.kind)
        elif self.kind == 'future':
            if data['pu'] != self.last:
                raise ValueError('FUTURE_SEQUENCE_GAP')
        elif data['U'] > self.last + 1:
            raise ValueError('SPOT_SEQUENCE_GAP')
        if self.event and data['E'] < self.event['E']:
            raise ValueError('EVENT_CLOCK_REGRESSION')
        for target, updates in ((self.bids, data['b']), (self.asks, data['a'])):
            for p, q in levels(updates).items():
                if q == 0:
                    target.pop(p, None)
                else:
                    target[p] = q
        if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
            raise ValueError('EMPTY_OR_CROSSED_BOOK')
        self.last = data['u']
        self.bridged = True
        self.applied += 1
        self.event = dict(E=data['E'], T=data.get('T'), received=row['mono_ms'], wall=row['wall_ms'])

    def sweep(self, side, amount, inverse=False):
        """Only initial snapshot's known-price region; unknown tails never fill."""
        ladder = self.bids if side == 'bid' else self.asks
        prices = sorted(ladder, reverse=side == 'bid')
        remaining, value, last_price = D(amount), D(0), None
        for price in prices:
            if price < self.bid_floor or price > self.ask_ceiling:
                continue
            take = min(remaining, ladder[price])
            value += take * 100 / price if inverse else take * price
            remaining -= take
            last_price = price
            if not remaining:
                return (D(amount) * 100 / value if inverse else value / D(amount)), last_price
        raise ValueError('INSUFFICIENT_KNOWN_DEPTH')


def quote_pair(spot, future, mono):
    if not spot.bridged or not future.bridged:
        return None, 'UNBRIDGED'
    ages = [mono - b.event['received'] for b in (spot, future)]
    if min(ages) < 0 or max(ages) > 1000:
        return None, 'STALE_RECEIVE'
    skew = abs(spot.event['E'] - future.event['E'])
    if skew > 500:
        return None, 'EVENT_SKEW'
    try:
        f, _ = future.sweep('bid', D(25), inverse=True)
        q = D(2500) / f
        s, _ = spot.sweep('ask', q)
    except ValueError as exc:
        return None, str(exc)
    fb, sa = max(future.bids), min(spot.asks)
    forecast = pressure_forecast(float(s), float(f))
    return dict(mono_ms=mono, event_skew_ms=skew, max_receive_age_ms=max(ages),
                future_inverse_vwap=str(f), spot_vwap=str(s), spot_btc=str(q),
                entry_basis_bps=str((f / s - 1) * 10000),
                spot_depth_impact_bps=str((s / sa - 1) * 10000),
                future_depth_impact_bps=str((1 - f / fb) * 10000),
                conditional_pressure_cash_forecast=forecast), None


def analyze(directory):
    path = Path(directory)
    receipt = json.loads((path / 'receipt.json').read_text())
    books = {name: Book(name) for name in ('spot', 'future')}
    pairs, rejected, frame_counts = [], Counter(), Counter()
    previous_mono = -1
    failure = None
    with localcontext() as ctx:
        ctx.prec = 40
        for line in (path / 'frames.jsonl').open():
            row = json.loads(line)
            if row['mono_ms'] < previous_mono:
                failure = 'LOCAL_MONOTONIC_REGRESSION'
                break
            previous_mono = row['mono_ms']
            frame_counts[row['kind']] += 1
            try:
                if row['kind'] == 'snapshot':
                    books[row['source']].snapshot(json.loads(row['payload']['raw']))
                elif row['kind'] == 'update':
                    books[row['source']].update(row)
                elif row['kind'] == 'tick':
                    pair, reason = quote_pair(books['spot'], books['future'], row['mono_ms'])
                    if reason:
                        rejected[reason] += 1
                    else:
                        pairs.append(pair)
            except (ValueError, KeyError, TypeError) as exc:
                failure = str(exc)
                break
    complete = receipt['status'] == 'CAPTURE_COMPLETE'
    supported = complete and failure is None and len(pairs) >= 60 and not rejected.get('INSUFFICIENT_KNOWN_DEPTH')
    metrics = {}
    for key in ('event_skew_ms', 'max_receive_age_ms', 'entry_basis_bps', 'spot_depth_impact_bps',
                'future_depth_impact_bps', 'conditional_pressure_cash_forecast'):
        values = [float(p[key]) for p in pairs]
        metrics[key] = dict(min=min(values), median=statistics.median(values), max=max(values)) if values else None
    result = dict(status='QUOTE_INPUT_FEASIBLE_ONLY' if supported else 'QUOTE_INPUT_INSUFFICIENT',
                  failure=failure, capture=receipt, ticks=frame_counts['tick'], qualifying_pairs=len(pairs),
                  rejected=dict(rejected), applied={k: b.applied for k, b in books.items()},
                  discarded={k: b.discarded for k, b in books.items()}, metrics=metrics,
                  positive_conditional_forecasts=sum(p['conditional_pressure_cash_forecast'] > 0 for p in pairs),
                  raw_sha256=hashlib.sha256((path / 'frames.jsonl').read_bytes()).hexdigest(),
                  exchange_clock_offsets='UNKNOWN', realized_trades=0, payoff_attempts=0,
                  independent_settlement_events=0, actual_fees='UNKNOWN', confirmation_admitted=False, g4=False)
    return result, pairs


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    args = parser.parse_args()
    result, pairs = analyze(args.directory)
    for name, value in (('analysis.json', result), ('pairs.json', pairs)):
        with (Path(args.directory) / name).open('x') as output:
            json.dump(value, output, indent=2, allow_nan=False)
            output.write('\n')
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'QUOTE_INPUT_FEASIBLE_ONLY' else 1)
