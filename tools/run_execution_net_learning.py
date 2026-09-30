"""Approved single offline batch: execution-net learning, no final confirmation."""
from collections import Counter
import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import subprocess
import time
import run_bounded_learning as old
from execution_net_learning import ExecutionLedger,LearningWindow,Safety,target,WINDOW,TRAIN

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.artifacts/execution-net-learning-20260930'
CONTRACT=ROOT/'docs/plans/2026-09-30-execution-net-learning.contract.json'
BINARY=RUN/'build/bounded_learning_driver'
ARMS=old.ARMS
need,sha,save=old.need,old.sha,old.save

def contract():
    c=json.loads(CONTRACT.read_text())
    elapsed=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(c['started_utc'])).total_seconds()
    need(0<=elapsed<=8*3600,'EIGHT_HOUR_BATCH')
    need(c['confirmation']==dict(interval=None,maximum_runs=0,learning=False),'CONFIRMATION_CLOSED')
    need(c['maximum_market_attempts']==1 and c['new_network_requests']==0,'BATCH_LIMIT')
    b=json.loads((RUN/'baseline.json').read_text())
    need(all(sha(ROOT/p)==h for p,h in b['preserved'].items()),'PRESERVED_CHANGED')
    return c

def preflight():
    from mvp_reference_inputs import compile_archive
    c=contract()
    compiled,proof=compile_archive(ROOT/'.artifacts/mvp-reference-history-20260921/archive/manifest.json')
    import hashlib
    need(hashlib.sha256(compiled).hexdigest()==c['input']['sha256'],'RAW_INPUT_CHANGED')
    # Capacity-only inspection of already-consumed records; no new strategy outcomes.
    buckets=[[0,0] for _ in range(3)];n=0;previous=None
    with (ROOT/'.artifacts/bounded-learning-20260929/trace.jsonl').open() as f:
        for line in f:
            r=json.loads(line)
            if r.get('terminal') or r['timestamp']<c['input']['development_start']:continue
            if previous and abs(previous['trend'])+abs(previous['defensive'])>1e-8:
                buckets[previous['bucket']][0 if n<TRAIN else 1]+=1
            previous=r['signal'];n+=1
            if n==WINDOW:break
    feasible=[i for i,(a,b) in enumerate(buckets) if a+b>=120 and min(a,b)>=10]
    need(bool(feasible),'NO_SAMPLE_FEASIBLE_BUCKET_IN_EXISTING_PREFIX')
    sources=set(old.SOURCES)|{'tools/execution_net_learning.py','tools/run_execution_net_learning.py',
                             'tools/audit_execution_net_learning.py','tools/audit_bounded_learning.py'}
    save(RUN/'execution-freeze.json',dict(contract_sha256=sha(CONTRACT),binary_sha256=sha(BINARY),
        sources={p:sha(ROOT/p) for p in sorted(sources)},input_sha256=c['input']['sha256'],
        raw_pages=proof['raw_pages'],raw_bars=proof['bars'],funding_events=proof['funding_events'],
        capacity_from_old_prefix=buckets,feasible_buckets=feasible,confirmation_runs=0,at=old.now()))
    print('INPUT_AND_CAPACITY_FROZEN',buckets,'FEASIBLE',feasible)

def desired(signal,weights):
    f=target(signal,[.5]*3);a=target(signal,weights)
    def unit(x):return 2500 if x>1e-8 else -2500 if x < -1e-8 else 0
    return dict(fixed=f,adaptive=a,fixed_stress=f,adaptive_stress=a,fixed_unit=unit(f),adaptive_unit=unit(a))

def evaluate(reason,books,updates,weekly):
    lcb=dict(stress=old.hac_lower([w['adaptive_stress'] for w in weekly]),
        paired=old.hac_lower([w['adaptive']-w['fixed'] for w in weekly]),
        unit=old.hac_lower([w['adaptive_unit']-w['fixed_unit'] for w in weekly]))
    supported=reason=='COMPLETE' and updates>0 and all(books[k].wallet.cash>10000
        for k in ('adaptive','adaptive_stress')) and all(x is not None and x>0 for x in lcb.values())
    verdict='DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE' if supported else (
        'NO_GO_SAFETY_WITHDRAWAL' if reason=='SAFETY_WITHDRAWAL' else
        'NO_GO_REFERENCE_RISK' if reason=='REFERENCE_RISK_STOP' else 'INSUFFICIENT_DEVELOPMENT_EVIDENCE')
    return verdict,lcb

def execute():
    c=contract();frozen=json.loads((RUN/'execution-freeze.json').read_text())
    need(sha(CONTRACT)==frozen['contract_sha256'] and sha(BINARY)==frozen['binary_sha256'],'IDENTITY_CHANGED')
    need(all(sha(ROOT/p)==h for p,h in frozen['sources'].items()),'SOURCE_CHANGED')
    need(sha(ROOT/c['input']['path'])==frozen['input_sha256'],'INPUT_CHANGED')
    save(RUN/'market-attempt.json',dict(started=old.now(),freeze_sha256=sha(RUN/'execution-freeze.json'),
                                     attempts=1,confirmation_runs=0))
    started=time.monotonic();books={k:ExecutionLedger(2 if k.endswith('stress') else 1) for k in ARMS}
    weights=[.5]*3;previous=None;window=None;safety=Safety();windows=[];reasons=Counter();updates=0
    active_bars=0;all_bars=0;reason='COMPLETE';last=None;last_mark=0;terminal=None;terminal_flat=False
    weekly=[];week_equity=dict.fromkeys(ARMS,10000.0);week_end=c['input']['development_start']+7*86400000
    p=subprocess.Popen([str(BINARY),'frozen','.5','.5','.5'],stdin=subprocess.PIPE,
                       stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    try:
        with (ROOT/c['input']['path']).open() as source,(RUN/'trace.jsonl').open('x') as out:
            reader=csv.DictReader(source)
            for row in reader:
                ts=int(row['timestamp'])
                if ts>=c['input']['development_end']:break
                need(time.monotonic()-started<c['maximum_compute_seconds'],'COMPUTE_LIMIT')
                active=ts>=c['input']['development_start'];last=row;last_mark=float(row['mark_close']);all_bars+=1
                rec=dict(timestamp=ts,active=active,weights=list(weights),previous_signal=previous)
                if active:
                    if window is None:
                        window=LearningWindow(weights,books['adaptive'],books['adaptive_stress'])
                        rec['window_start']={k:books[k].snapshot(last_mark) for k in ('adaptive','adaptive_stress')}
                    targets=desired(previous,weights)
                    for b in books.values():
                        b.wallet.fund(float(row['mark_open']),float(row['funding_rate_per_interval']))
                    opening_stop=any(books[k].wallet.drawdown>=.08 for k in ARMS[:4])
                    rec['boundary']='OPEN_FUNDING' if opening_stop else 'POST_TRADE_OR_INTRABAR'
                    changes={k:b.step(row,targets[k],funded=True,halt=opening_stop) for k,b in books.items()}
                    active_bars+=1
                    safety.observe(changes['adaptive'],previous['bucket'] if previous else 1)
                    if any(books[k].wallet.drawdown>=.08 for k in ARMS[:4]):
                        reason='REFERENCE_RISK_STOP'
                    # No selection/learning on or after a breached execution bar.
                    if reason=='COMPLETE':
                        window.step(row,previous)
                        for key,book in zip(('adaptive','adaptive_stress'),window.books['current']):
                            need(abs(book.last_liquidation-books[key].last_liquidation)<1e-8,
                                 'LEARNING_AND_EXECUTION_LEDGER_DIVERGENCE')
                        rec['candidate_liquidation']={k:[b.last_liquidation for b in v] for k,v in window.books.items()}
                        if window.steps==TRAIN:rec['train_lock']=window.locked
                        if window.steps==WINDOW:
                            net=window.finish();s=safety.assess()
                            net.update(safety=s,end_ms=ts+300000,selection_reason=net['reason'])
                            need(len(windows)<c['maximum_evaluation_windows'],'WINDOW_BUDGET')
                            if s['withdrawn']:
                                # Never apply a nominal winner after a safety withdrawal.
                                net.update(updated=False,weights_after=list(weights),reason='SAFETY_WITHDRAWAL')
                                reason='SAFETY_WITHDRAWAL'
                            if net['updated']:weights=list(net['weights_after']);updates+=1
                            reasons[net['reason']]+=1;windows.append(net);rec['window_result']=net;window=None
                    rec['receipts']=changes;rec['executed_targets']=targets
                    if ts+300000>=week_end:
                        current={k:b.last_liquidation for k,b in books.items()}
                        weekly.append({k:current[k]-week_equity[k] for k in ARMS})
                        week_equity=current;week_end+=7*86400000
                rec['wallets']={k:b.snapshot(last_mark) for k,b in books.items()}
                if reason!='COMPLETE':
                    rec['signal']=None;out.write(json.dumps(rec,allow_nan=False)+'\n');break
                need(p.stdin is not None and p.stdout is not None,'NATIVE_PIPES')
                p.stdin.write(old.protocol(row,False,books['adaptive'].wallet));p.stdin.flush()
                raw=p.stdout.readline();need(bool(raw),'NATIVE_TERMINATED')
                signal=json.loads(raw);need(signal['ts']==ts+300000,'BAR_CLOSE_CAUSALITY')
                need(not signal['updated'] and not signal['withdrawn'],'NATIVE_SIGNAL_ONLY')
                rec['signal']=signal;out.write(json.dumps(rec,allow_nan=False)+'\n')
                previous=signal if active else None
            need(last is not None,'EMPTY_INPUT')
            if reason=='SAFETY_WITHDRAWAL':
                row=next(reader,None)
                need(row is not None and int(row['timestamp'])==int(last['timestamp'])+300000,'NEXT_OPEN_EXIT_MISSING')
                last_mark=float(row['mark_open']);terminal=int(row['timestamp'])
                for b in books.values():b.settle(row,True)
                terminal_flat=True
            elif reason=='COMPLETE':
                need(int(last['timestamp'])+300000==c['input']['development_end'],'INCOMPLETE_INPUT')
                terminal=int(last['timestamp'])+300000
                for b in books.values():b.settle(last,False)
                terminal_flat=True
            if any(books[k].wallet.drawdown>=.08 for k in ARMS[:4]):reason='REFERENCE_RISK_STOP'
            out.write(json.dumps(dict(terminal=True,at=terminal,flat=terminal_flat,reason=reason,
                mark=last_mark,wallets={k:b.snapshot(last_mark) for k,b in books.items()}),allow_nan=False)+'\n')
    finally:
        if p.stdin:p.stdin.close()
        try:code=p.wait(timeout=10)
        except subprocess.TimeoutExpired:p.kill();p.wait();raise ValueError('NATIVE_EXIT_TIMEOUT')
        error=p.stderr.read() if p.stderr else ''
        if p.stdout:p.stdout.close()
        if p.stderr:p.stderr.close()
        need(code==0,'NATIVE_FAILED:'+error[:500])
    verdict,lcb=evaluate(reason,books,updates,weekly)
    result=dict(schema='execution_net_learning_result_v1',verdict=verdict,stop_reason=reason,
        scope='DEVELOPMENT_ONLY',confirmation_runs=0,candidate_status='NO_QUALIFIED_CANDIDATE',
        controller_updates=updates,windows=windows,window_reasons=dict(reasons),final_weights=weights,
        source_bars=all_bars,development_bars=active_bars,stop_timestamp_ms=int(last['timestamp']),
        terminal_timestamp_ms=terminal,terminal_flat=terminal_flat,full_development_period=reason=='COMPLETE',
        wallets={k:b.snapshot(last_mark) for k,b in books.items()},weekly=weekly,hac_lower=lcb,
        trace_sha256=sha(RUN/'trace.jsonl'),freeze_sha256=sha(RUN/'execution-freeze.json'),
        compute_seconds=time.monotonic()-started,completed=old.now(),market_attempts=1,
        account_access=0,network_requests=0,deployment=0,push_attempts=0)
    save(RUN/'result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('windows','weekly')},allow_nan=False))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('preflight','run'))
    preflight() if p.parse_args().action=='preflight' else execute()
