"""Independent Decimal reconciliation of the consumed zero-update development prefix.

No learning, strategy replay, unconsumed return calculation, or market requests.
This scoped auditor cannot qualify a candidate or consume a confirmation set.
"""
from collections import Counter
import csv
from decimal import Decimal as D, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR
import json
from pathlib import Path
import run_bounded_learning as b

def close(actual, expected, label):
    b.need(abs(D(str(actual))-expected)<D('0.00000005'), 'DECIMAL_MISMATCH:'+label)

class Book:
    def __init__(self, stress=False):
        self.cash=D(10000);self.q=D(0);self.peak=D(10000);self.dd=D(0)
        self.fees=D(0);self.slip=D(0);self.funding=D(0);self.turnover=D(0);self.fills=0
        self.m=D(2 if stress else 1)

    def value(self, mark):return self.cash+self.q*mark

    def observe(self, hi, lo):
        high=max(self.value(hi),self.value(lo));low=min(self.value(hi),self.value(lo))
        self.peak=max(self.peak,high);self.dd=max(self.dd,1-low/self.peak)

    def fund(self, mark, rate):
        paid=self.q*mark*rate
        self.funding+=paid;self.cash-=paid;self.observe(mark,mark)

    def trade(self, target, price, mark):
        q=(target/price*1000).to_integral_value(rounding=ROUND_DOWN)/1000
        change=q-self.q
        if not change:return
        side=1 if change>0 else -1
        execution=price*(1+side*self.m*D('.0001'))
        execution=(execution*10).to_integral_value(rounding=ROUND_CEILING if side>0 else ROUND_FLOOR)/10
        fee=abs(change)*execution*self.m*D('.00055')
        self.cash-=change*execution+fee;self.q=q;self.fees+=fee
        self.slip+=change*(execution-price);self.turnover+=abs(change)*execution;self.fills+=1
        self.observe(mark,mark)

    def check(self, snapshot, mark):
        for key,expected in dict(cash=self.cash,qty=self.q,equity=self.value(mark),max_drawdown=self.dd,
                fees=self.fees,slippage=self.slip,funding_paid=self.funding,turnover=self.turnover,fills=D(self.fills)).items():
            close(snapshot[key],expected,key)

def audit(run=None, csv_path=None, start=None):
    run=run or b.RUN
    result=json.loads((run/'result.json').read_text())
    b.need(result['controller_updates']==0,'AUDIT_ONLY_ZERO_UPDATE_PREFIX')
    b.need(result['trace_sha256']==b.sha(run/'trace.jsonl'),'TRACE_IDENTITY')
    b.need(result['confirmation_runs']==0 and result['candidate_status']=='NO_QUALIFIED_CANDIDATE','NO_AUTHORITY')
    if csv_path is None:
        c=b.read_contract();csv_path=b.ROOT/c['input']['csv'];start=c['input']['development_start_ms']
        freeze=json.loads((run/'execution-freeze.json').read_text())
        b.need(b.sha(run/'execution-freeze.json')==result['execution_freeze_sha256'],'FREEZE_IDENTITY')
        source_root=run/'executed-sources'
        b.need(source_root.is_dir(),'EXECUTED_SOURCE_ARCHIVE_REQUIRED')
        b.need(all(b.sha(source_root/p)==h for p,h in freeze['sources'].items()),'EXECUTED_SOURCE_CHANGED')
        b.need(b.sha(b.BINARY)==freeze['binary_sha256'],'BINARY_CHANGED')
    books={k:Book(k.endswith('stress')) for k in b.ARMS}
    target=dict.fromkeys(b.ARMS,D(0));prior=None;window=[];windows=[];active_index=0
    action_records=[];bars=0;last=None;terminal=None
    with Path(csv_path).open() as f,(run/'trace.jsonl').open() as tr:
        rows=csv.DictReader(f)
        for line in tr:
            rec=json.loads(line)
            if rec.get('terminal'):
                terminal=rec
                if rec['terminal_flat']:
                    if result['stop_reason']=='SAFETY_WITHDRAWAL':
                        row=next(rows)
                        b.need(int(row['timestamp'])==rec['settlement_timestamp_ms'],'NEXT_OPEN_IDENTITY')
                        mark=D(row['mark_open']);price=D(row['open'])
                        for book in books.values():book.fund(mark,D(row['funding_rate_per_interval']))
                    else:
                        mark=D(last['mark_close']);price=D(last['price'])
                    for book in books.values():book.trade(D(0),price,mark)
                else:
                    mark=D(str(rec['value_mark']))
                for k,book in books.items():book.check(rec['wallets'][k],mark)
                continue
            b.need(terminal is None,'TRACE_AFTER_TERMINAL')
            row=next(rows);last=row;bars+=1
            ts=int(row['timestamp']);b.need(rec['timestamp']==ts,'TRACE_ROW_ALIGNMENT')
            active=ts>=start
            mark=D(row['mark_close']);mo=D(row['mark_open']);price=D(row['open'])
            if active:
                for book in books.values():book.fund(mo,D(row['funding_rate_per_interval']))
                if rec.get('boundary')!='OPEN_FUNDING':
                    for k,book in books.items():
                        book.trade(target[k],price,mo)
                        book.observe(D(row['mark_high']),D(row['mark_low']))
            for k,book in books.items():book.check(rec['wallets'][k],mark)
            if rec['signal'] is None:
                b.need(result['stop_reason']=='REFERENCE_RISK_STOP','MISSING_SIGNAL_WITHOUT_RISK_STOP')
                continue
            s=rec['signal']
            b.need(not s['updated'] and s['weights']==[.5,.5,.5] and s['weight']==.5,'ZERO_UPDATE_IDENTITY')
            b.need(s['ts']==ts+300000,'CAUSAL_SIGNAL_TIME')
            for k in b.ARMS:close(rec['executed_targets'][k],target[k],'lagged_target')
            blend=(D(str(s['trend']))+D(str(s['defensive'])))/2
            if active:
                unit=D(2500 if blend>D('1e-8') else -2500 if blend<D('-1e-8') else 0)
                target={k:(unit if k.endswith('unit') else blend) for k in b.ARMS}
                if prior is not None:
                    previous,prior_mark=prior
                    gross=previous*(mark/prior_mark-1)
                    funding=-previous*D(row['funding_rate_per_interval'])
                    cost=abs(blend-previous)*D('.00065')
                    window.append(dict(bucket=s['bucket'],gross=gross,funding=funding,cost=cost,net=gross+funding-cost))
                prior=(blend,mark)
                if active_index>0 and active_index%240==0:
                    bucket_stats={}
                    for bucket in range(3):
                        selected=[v for v in window if v['bucket']==bucket]
                        bucket_stats[str(bucket)]=dict(samples=len(selected),**{
                            k:float(sum((v[k] for v in selected),D(0))) for k in ('gross','funding','cost','net')})
                    windows.append(dict(end_timestamp_ms=s['ts'],buckets=bucket_stats))
                    if s['action']=='EVOLUTION_LEARNABILITY_INSUFFICIENT_SAMPLES':
                        bucket=bucket_stats[str(s['bucket'])]
                        b.need(bucket['samples']==s['learnability_samples']<120,'SAMPLE_CAPACITY_DIAGNOSIS')
                        close(s['virtual_pnl'],D(str(bucket['net'])),'virtual_window')
                    if s['withdrawn']:
                        losing=[k for k,v in bucket_stats.items() if v['net']<0 and windows[-2]['buckets'][k]['net']<0]
                        b.need(bool(losing),'SAFETY_TWO_NEGATIVE_WINDOWS')
                        # The controller checks buckets in fixed trend/range/extreme order.
                        close(s['virtual_pnl'],D(str(bucket_stats[losing[0]]['net'])),'safety_window')
                    window=[]
                active_index+=1
            if s['action']:action_records.append(dict(timestamp=s['ts'],**s))
    b.need(terminal is not None and bars==result['processed_bars'],'AUDIT_COVERAGE')
    for k,book in books.items():book.check(result['wallets'][k],D(str(terminal['value_mark'])))
    fixed=books['fixed'];net=fixed.value(D(str(terminal['value_mark'])))-10000
    gross=net+fixed.fees+fixed.slip+fixed.funding
    return dict(schema='bounded_learning_independent_prefix_audit_v1',status='PASS_DIAGNOSTIC_ONLY',
        decimal_books=6,verified_prefix_bars=bars,controller_window_diagnosis=windows,actions=action_records,
        fixed_net_pnl=float(net),fixed_gross_pnl=float(gross),fixed_execution_cost=float(fixed.fees+fixed.slip),
        execution_cost_share_of_loss=float((fixed.fees+fixed.slip)/(-net)) if net<0 else None,
        paired_uplift=0,unit_direction_uplift=0,confirmation_runs=0,qualification=False,
        result_sha256=b.sha(run/'result.json'),trace_sha256=b.sha(run/'trace.jsonl'))

if __name__=='__main__':
    report=audit()
    output=b.RUN/'independent-audit.json'
    if output.exists():
        b.need(json.loads(output.read_text())==report,'ARCHIVED_AUDIT_MISMATCH')
    else:
        b.save(output,report)
    print(json.dumps(report,sort_keys=True,allow_nan=False))
