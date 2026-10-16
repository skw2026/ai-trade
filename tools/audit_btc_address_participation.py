"""Independent Decimal address-feature audit. No payoff or model fitting."""
import csv
import datetime as dt
from decimal import Decimal as D
import io

DAY = 86400000
STEP = 300000


def features(raw, rows, reported, start, end, lag):
    if lag not in (3, 4):
        raise ValueError('ADDRESS_AUDIT_LAG')
    source = {r['time']: r for r in csv.DictReader(io.StringIO(raw.decode()))}
    values = {d: D(r['AdrActCnt']) for d,r in source.items() if r['AdrActCnt']}
    closes = {r['timestamp']+STEP: D(str(r['price'])) for r in rows}
    if set(reported) != set(range(start, end, DAY)):
        raise ValueError('ADDRESS_AUDIT_GRID')
    def equal(actual, expected):
        if abs(D(str(actual))-expected) >= D('.000000005'):
            raise ValueError('ADDRESS_AUDIT_VALUE')
    def clip(x): return max(D(-3), min(D(3), x))
    for t, f in reported.items():
        day = dt.datetime.fromtimestamp(t/1000, dt.timezone.utc).date()-dt.timedelta(days=lag)
        past = [values[str(day-dt.timedelta(days=i))].ln() for i in range(1, 29)]
        mu = sum(past)/28
        sd = (sum((x-mu)**2 for x in past)/28).sqrt()
        times=[]
        for i in range(29):
            row=source[str(day-dt.timedelta(days=i))]
            raw_time=row.get('AssetEODCompletionTime','')
            times.append(None if not raw_time else 1000*max(D(raw_time),D(row.get('AssetCompletionTime') or raw_time)))
        ready=all(v is not None and v<=t for v in times)
        if sd < D('.000001') or not ready:
            if f['z'] is not None: raise ValueError('ADDRESS_AUDIT_CONSTANT')
        else:
            weekday=sum(values[str(day-dt.timedelta(days=i))].ln() for i in (7,14,21,28))/4
            equal(f['z'], clip((values[str(day)].ln()-weekday)/sd))
        latest=max(times) if all(v is not None for v in times) else None
        if f['completion_ready'] != ready or f['latest_completion_ms'] != latest:
            raise ValueError('ADDRESS_AUDIT_COMPLETION')
        equal(f['r'], clip((closes[t]/closes[t-7*DAY]).ln()/D('.05')))
        equal(f['price'], closes[t])
        if f['source_day'] != str(day) or f['latest_price_closed_at'] != t:
            raise ValueError('ADDRESS_AUDIT_CLOCK')
    return dict(status='DECIMAL_ADDRESS_FEATURE_CLOCK_VERIFIED', decisions=len(reported), lag_days=lag)
