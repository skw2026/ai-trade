#!/usr/bin/env python3
"""Independent raw-row/calendar audit, not importing the supply signal helper."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from screen_regional_session import need, strict, save_new

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.artifacts/eth-net-supply-screen-20260929'


def iso(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def ts(value):
    return datetime.fromisoformat(value.replace('Z','+00:00'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconstruct(c,raw_rows,retrieved_at):
    indexed={}
    for row in raw_rows:
        at=ts(row['time'])
        need(row['asset']=='eth' and at.hour==at.minute==at.second==at.microsecond==0,'RAW_IDENTITY')
        key=at.date()
        need(key not in indexed,'DUPLICATE_RAW_DAY')
        indexed[key]=row
    first=date.fromisoformat(c['calendar']['first_entry'][:10])
    last=date.fromisoformat(c['calendar']['last_entry'][:10])
    split=ts(c['calendar']['split'])
    weeks=[]
    late_unique=set()
    largest=None
    for week_index in range((last-first).days//7+1):
        entry_date=first+timedelta(days=week_index*7)
        decision=datetime.combine(entry_date,datetime.min.time(),timezone.utc)+timedelta(hours=12)
        days=[entry_date-timedelta(days=k) for k in range(9,1,-1)]
        reasons=set()
        late=[]
        endpoints=[]
        for i,day in enumerate(days):
            row=indexed.get(day)
            if row is None:
                reasons.add('MISSING_DAY')
                continue
            value=row.get('SplyCur')
            if value is None:
                reasons.add('MISSING_SUPPLY')
            else:
                need(isinstance(value,str),'SUPPLY_STRING')
                number=Decimal(value)
                need(number.is_finite() and number>0,'SUPPLY_FINITE_POSITIVE')
                if i in (0,7):
                    endpoints.append(number)
            period_end=datetime.combine(day+timedelta(days=1),datetime.min.time(),timezone.utc)
            observed=[]
            for field in ('AssetCompletionTime','AssetEODCompletionTime'):
                value=row.get(field)
                if value is None:
                    reasons.add('MISSING_COMPLETION')
                    continue
                need(isinstance(value,str) and value.isascii() and value.isdigit(),'RAW_CLOCK_TYPE')
                clock=datetime.fromtimestamp(int(value),timezone.utc)
                need(period_end<=clock<=ts(retrieved_at),'RAW_CLOCK_RANGE')
                observed.append(clock)
            if observed and max(observed)>decision:
                reasons.add('COMPLETION_AFTER_DECISION')
                late_unique.add(day.isoformat())
                maximum=max(observed)
                delay=int((maximum-period_end).total_seconds())
                late.append(dict(day=day.isoformat(),completion_utc=iso(maximum),delay_seconds=delay))
                if largest is None or delay>largest['delay_seconds']:
                    largest=late[-1]
        if len(endpoints)==2 and endpoints[0]==endpoints[1]:
            reasons.add('SUPPLY_TIE')
        weeks.append(dict(entry=iso(decision),valid=not reasons,reasons=sorted(reasons),late_days=late))
    valid=sum(w['valid'] for w in weeks)
    halves=[sum(w['valid'] and (ts(w['entry'])<split)==early for w in weeks) for early in (True,False)]
    insufficient=(valid<c['capacity']['minimum_valid_weeks'] or
                  min(halves)<c['capacity']['minimum_each_calendar_half'])
    return dict(status='CAPACITY_INSUFFICIENT' if insufficient else 'PRICE_CAPACITY_STILL_REQUIRED',
                maximum_valid_weeks=valid,maximum_half_counts=halves,weeks=weeks,
                excluded_by_entry_year=dict(sorted(Counter(w['entry'][:4] for w in weeks if not w['valid']).items())),
                unique_decision_violating_days=len(late_unique),maximum_recorded_delay=largest,
                reasons=dict(Counter(r for w in weeks for r in w['reasons'])))


def main():
    gate=strict((RUN/'validation-state.json').read_bytes())
    need(gate['status']=='RUNNING','FORMAL_GATE_REQUIRED')
    c=strict((ROOT/'docs/contracts/eth_net_supply_proxy_v1.json').read_bytes())
    baseline=strict((RUN/'baseline.json').read_bytes())
    need(all(digest(ROOT/p)==h for p,h in baseline['preserved_sha256'].items()),'PRESERVED_CHANGED')
    expected=strict((RUN/'clock-capacity.json').read_bytes())
    need(all(digest(RUN/p)==h for p,h in expected['input_sha256'].items()),'INPUT_CHANGED')
    rows=[]
    clocks=[]
    receipts=[]
    for path in sorted((RUN/'receipts').glob('*.json')):
        receipt=strict(path.read_bytes())
        need(receipt['status']==200 and receipt['error'] is None,'RECEIPT_FAILED')
        need(digest(RUN/receipt['raw'])==receipt['sha256'],'RAW_SHA')
        rows.extend(strict((RUN/receipt['raw']).read_bytes())['data'])
        clocks.append(receipt['retrieved_utc'])
        receipts.append(receipt)
    dates=sorted(ts(row['time']).date() for row in rows)
    first,last=date(2022,12,24),date(2025,12,27)
    need(dates==[first+timedelta(days=k) for k in range((last-first).days+1)],'CONTINUOUS_DAILY_COVERAGE')
    result=reconstruct(c,rows,max(clocks))
    for key in ('status','maximum_valid_weeks','maximum_half_counts','reasons'):
        need(result[key]==expected[key],'INDEPENDENT_MISMATCH '+key)
    need([(w['entry'],w['valid'],w['reasons']) for w in result['weeks']]==
         [(w['entry'],w['valid'],w['reasons']) for w in expected['weeks']],'EXCLUSIONS_MISMATCH')
    need(result['status']=='CAPACITY_INSUFFICIENT','THIS_CLOSEOUT_REQUIRES_NEGATIVE_CAPACITY')
    need(not (RUN/'economic-attempt.json').exists() and not (RUN/'economy.json').exists(),
         'UNAUTHORIZED_DEPENDENT_OUTPUT')
    result.update(audit='INDEPENDENT_RAW_CLOCK_RECONSTRUCTION_PASS',supply_rows=len(rows),
                  price_rows=0,economic_attempts=0,candidate_qualified=False,
                  preserved_files=len(baseline['preserved_sha256']),preserved_gates=15,
                  actual_gets=len(receipts),documentation_reads=12,
                  source_sha256={str(p.relative_to(ROOT)):digest(p) for p in
                    (Path(__file__).resolve(),ROOT/'tools/test_audit_eth_supply_clock.py')},
                  clock_capacity_sha256=digest(RUN/'clock-capacity.json'))
    save_new(RUN/'independent-clock-audit.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='weeks'},indent=2))


if __name__=='__main__':
    main()
