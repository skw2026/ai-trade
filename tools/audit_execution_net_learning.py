"""Independent Decimal audit of consumed execution-net evidence, never new research.

Reconstructs six executed books and all 32 shadow books per window from receipts.
No strategy generator, market transport, learner call, or confirmation authority.
"""
import copy
import csv
from decimal import Decimal as D
import json
from pathlib import Path
import statistics
import math
from audit_bounded_learning import Book, close
import run_bounded_learning as common

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.artifacts/execution-net-learning-20260930'
ARMS=common.ARMS
need=common.need

def signal_target(signal,weights):
    if signal is None:return D(0)
    w=D(str(weights[signal['bucket']]))
    x=w*D(str(signal['trend']))+(1-w)*D(str(signal['defensive']))
    return D(0) if abs(x)<D('1e-8') else x

class Ledger(Book):
    def __init__(self,stress=False):
        super().__init__(stress);self.last=D(0);self.liquid=D(10000)

    def reserve(self,price,mark):
        book=copy.deepcopy(self);book.trade(D(0),price,mark)
        return book.cash

    def step(self,row,target,*,funded=False,halt=False):
        op,mo,cl,mc,rate=(D(row[k]) for k in ('open','mark_open','price','mark_close','funding_rate_per_interval'))
        if not funded:self.fund(mo,rate)
        halt=halt or self.dd>=D('.08')
        if not halt:
            if abs(self.q)*max(op,mo)>D('2500.00000001') or abs(target-self.last)>D('1e-8'):
                capped=min(abs(target),D(2500)*op/max(op,mo))*(-1 if target<0 else 1)
                self.trade(capped,op,mo);self.last=target
            self.observe(D(row['mark_high']),D(row['mark_low']))
        new=self.reserve(cl,mc);delta=new-self.liquid;self.liquid=new
        return delta

    def check(self,snapshot,mark):
        super().check(snapshot,mark)
        close(snapshot['last_target'],self.last,'last_target')
        close(snapshot['net_liquidation_equity'],self.liquid,'net_liquidation')

def statistic(values):
    if len(values)<2:return 0.0
    values=[float(v) for v in values];sd=statistics.stdev(values)
    return statistics.mean(values)*math.sqrt(len(values))/sd if sd>1e-12 else 0.0

def audit(run=None,csv_path=None,start=None):
    run=Path(run or RUN);result=json.loads((run/'result.json').read_text())
    need(common.sha(run/'trace.jsonl')==result['trace_sha256'],'TRACE_IDENTITY')
    need(result['confirmation_runs']==0 and result['candidate_status']=='NO_QUALIFIED_CANDIDATE','NO_AUTHORITY')
    if csv_path is None:
        contract=json.loads((ROOT/'docs/plans/2026-09-30-execution-net-learning.contract.json').read_text())
        csv_path=ROOT/contract['input']['path'];start=contract['input']['development_start']
        freeze=json.loads((run/'execution-freeze.json').read_text())
        need(common.sha(csv_path)==freeze['input_sha256'],'INPUT_IDENTITY')
        need(common.sha(run/'execution-freeze.json')==result['freeze_sha256'],'FREEZE_IDENTITY')
        need(all(common.sha(ROOT/p)==h for p,h in freeze['sources'].items()),'SOURCE_IDENTITY')
        need(common.sha(run/'build/bounded_learning_driver')==freeze['binary_sha256'],'BINARY_IDENTITY')
    books={k:Ledger(k.endswith('stress')) for k in ARMS}
    shadow=None;prior=None;weights=[.5]*3;bars=0;active_bars=0;windows=[];updates=0
    nets=[D(0)]*4;streak=[0]*4;safety_latched=False;terminal=None;last=None
    weekly=[];week_equity=dict.fromkeys(ARMS,D(10000));week_end=start+7*86400000
    with Path(csv_path).open() as f,(run/'trace.jsonl').open() as trace:
        reader=csv.DictReader(f)
        for line in trace:
            r=json.loads(line)
            if r.get('terminal'):
                need(terminal is None,'DOUBLE_TERMINAL');terminal=r
                if r['flat']:
                    # A withdrawal exit uses the next open; fixed-end exits use last close.
                    next_open=result['terminal_timestamp_ms']==int(last['timestamp'])+300000 and safety_latched
                    row=next(reader) if next_open else last
                    mark=D(row['mark_open' if next_open else 'mark_close'])
                    price=D(row['open' if next_open else 'price'])
                    if next_open:
                        need(int(row['timestamp'])==r['at'],'NEXT_OPEN_IDENTITY')
                    for book in books.values():
                        if next_open:book.fund(mark,D(row['funding_rate_per_interval']))
                        book.trade(D(0),price,mark);book.last=D(0);book.liquid=book.cash
                else:mark=D(str(r['mark']))
                for k,book in books.items():book.check(r['wallets'][k],mark)
                continue
            need(terminal is None and not safety_latched,'TRACE_AFTER_STOP')
            row=next(reader);last=row;bars+=1;ts=int(row['timestamp']);mark=D(row['mark_close'])
            need(ts==r['timestamp'] and r['active']==(ts>=start),'ROW_IDENTITY')
            need(r['previous_signal']==prior and r['weights']==weights,'CAUSAL_SIGNAL_AND_WEIGHTS')
            if r['active']:
                active_bars+=1
                if shadow is None:
                    need('window_start' in r,'MISSING_CARRIED_STATE')
                    for k in ('adaptive','adaptive_stress'):books[k].check(r['window_start'][k],mark)
                    vectors={'current':list(weights)}
                    for bucket in range(3):
                        for j,w in enumerate((.4,.45,.5,.55,.6)):
                            v=list(weights);v[bucket]=w;vectors[f'{bucket}:{j}']=v
                    shadow={k:[copy.deepcopy(books['adaptive']),copy.deepcopy(books['adaptive_stress'])] for k in vectors}
                    receipts={k:[[],[]] for k in vectors};counts=[[0,0] for _ in range(3)];step=0;locked=None
                fixed=signal_target(prior,[.5]*3);adaptive=signal_target(prior,weights)
                unit=lambda v:D(2500 if v>D('1e-8') else -2500 if v<D('-1e-8') else 0)
                targets=dict(fixed=fixed,adaptive=adaptive,fixed_stress=fixed,adaptive_stress=adaptive,
                             fixed_unit=unit(fixed),adaptive_unit=unit(adaptive))
                for book in books.values():book.fund(D(row['mark_open']),D(row['funding_rate_per_interval']))
                halted=any(books[k].dd>=D('.08') for k in ARMS[:4])
                need(r['boundary']==('OPEN_FUNDING' if halted else 'POST_TRADE_OR_INTRABAR'),'RISK_BOUNDARY')
                for k,book in books.items():
                    close(r['executed_targets'][k],targets[k],'target')
                    receipt=book.step(row,targets[k],funded=True,halt=halted)
                    close(r['receipts'][k],receipt,'receipt')
                    if k=='adaptive':
                        nets[prior['bucket'] if prior else 1]+=receipt;nets[3]+=receipt
                if 'candidate_liquidation' in r:
                    need(not any(books[k].dd>=D('.08') for k in ARMS[:4]),'LEARNING_AFTER_RISK')
                    if prior and abs(prior['trend'])+abs(prior['defensive'])>1e-8:
                        counts[prior['bucket']][0 if step<168 else 1]+=1
                    for key,pair in shadow.items():
                        for i,book in enumerate(pair):
                            receipts[key][i].append(book.step(row,signal_target(prior,vectors[key])))
                            close(r['candidate_liquidation'][key][i],book.liquid,'candidate:'+key)
                    step+=1
                    if step==168:
                        possible=[k for k,(a,_) in enumerate(counts) if a>=10 and a+72>=120]
                        if possible:
                            preferred=prior['bucket'] if prior else 1
                            bucket=preferred if preferred in possible else max(possible,key=lambda k:counts[k][0])
                            keys=[k for k in vectors if k.startswith(str(bucket)+':') and
                                  abs(vectors[k][bucket]-weights[bucket])<=.050000001]
                            locked=max(keys,key=lambda k:(sum(receipts[k][0]),-abs(vectors[k][bucket]-weights[bucket])))
                        need(r['train_lock']==locked,'TRAIN_ONLY_SELECTION')
                    if step==240:
                        w=r['window_result'];need(w['counts']==counts and w['locked']==locked,'WINDOW_LOCK_CAPACITY')
                        need(w['weights_before']==weights,'WINDOW_START_WEIGHTS')
                        for key,values in receipts.items():
                            for name,expected in dict(train=sum(values[0][:168]),holdout=sum(values[0][168:]),
                                    stress_train=sum(values[1][:168]),stress_holdout=sum(values[1][168:])).items():
                                close(w['scores'][key][name],expected,'candidate_score')
                        allowed=False;selection='CAPACITY_INSUFFICIENT'
                        if locked:
                            bucket=int(locked.split(':')[0]);a,h=counts[bucket]
                            if a+h>=120 and min(a,h)>=10:
                                values=receipts[locked];hold=values[0][168:]
                                diff=[x-y for x,y in zip(hold,receipts['current'][0][168:])]
                                risk=any(b.dd>=D('.08') for b in shadow[locked])
                                positive=min(sum(values[0][:168]),sum(hold),sum(values[1][:168]),sum(values[1][168:]))>0
                                significant=statistic(hold)>=1.5 and statistic(diff)>=1.5 and sum(diff)>0
                                unchanged=vectors[locked]==weights
                                selection=('CANDIDATE_REFERENCE_RISK' if risk else 'NO_ABSOLUTE_NET_EDGE' if not positive else
                                    'NET_OR_PAIRED_EVIDENCE_INSUFFICIENT' if not significant else 'UNCHANGED' if unchanged else 'EXECUTION_NET_UPDATE')
                                allowed=selection=='EXECUTION_NET_UPDATE'
                                if not risk:
                                    close(w['net_t'],D(str(statistic(hold))),'net_t')
                                    close(w['paired_t'],D(str(statistic(diff))),'paired_t')
                        need(w['selection_reason']==selection,'SELECTION_VERDICT')
                        for k,net in enumerate(nets):
                            close(w['safety']['net_by_decision_bucket_and_total'][k],net,'safety_net')
                            streak[k]=streak[k]+1 if net<0 else 0
                        safety_latched=any(n>=2 for n in streak)
                        need(w['safety']['streak']==streak and w['safety']['withdrawn']==safety_latched,'SAFETY_LATCH')
                        allowed=allowed and not safety_latched
                        need(w['updated']==allowed,'UPDATE_ADMISSIBILITY')
                        if allowed:weights=list(vectors[locked]);updates+=1
                        need(w['weights_after']==weights,'WEIGHT_UPDATE')
                        windows.append(w);shadow=None;nets=[D(0)]*4
                else:need(any(books[k].dd>=D('.08') for k in ARMS[:4]),'MISSING_CANDIDATE_RECEIPTS')
                if ts+300000>=week_end:
                    current={k:b.liquid for k,b in books.items()}
                    weekly.append({k:current[k]-week_equity[k] for k in ARMS})
                    week_equity=current;week_end+=7*86400000
            for k,book in books.items():book.check(r['wallets'][k],mark)
            if r['signal'] is not None:
                need(r['signal']['ts']==ts+300000 and not r['signal']['updated'] and not r['signal']['withdrawn'],'SIGNAL_TIME_OR_AUTHORITY')
            prior=r['signal'] if r['active'] else None
    need(terminal is not None and bars==result['source_bars'] and active_bars==result['development_bars'],'COVERAGE')
    need(windows==result['windows'] and updates==result['controller_updates'] and weights==result['final_weights'],'RESULT_SELECTION')
    need(len(weekly)==len(result['weekly']),'WEEK_COVERAGE')
    for actual,expected in zip(result['weekly'],weekly):
        for k in ARMS:close(actual[k],expected[k],'weekly')
    mark=D(str(terminal['mark']))
    for k,book in books.items():book.check(result['wallets'][k],mark)
    economics={}
    for k,b in books.items():
        net=b.value(mark)-10000
        economics[k]=dict(net=float(net),gross=float(net+b.fees+b.slip+b.funding),
                         fees=float(b.fees),slippage=float(b.slip),funding=float(b.funding),fills=b.fills)
    return dict(schema='execution_net_independent_audit_v1',status='PASS_OFFLINE_RECEIPTS_ONLY',
        actual_books=6,shadow_books_per_window=32,verified_bars=bars,verified_windows=len(windows),
        verified_updates=updates,economics=economics,paired_uplift=float(books['adaptive'].value(mark)-books['fixed'].value(mark)),
        unit_direction_uplift=float(books['adaptive_unit'].value(mark)-books['fixed_unit'].value(mark)),
        confirmation_runs=0,qualification=False,result_sha256=common.sha(run/'result.json'),trace_sha256=common.sha(run/'trace.jsonl'))

if __name__=='__main__':
    report=audit();path=RUN/'independent-audit.json'
    if path.exists():need(json.loads(path.read_text())==report,'AUDIT_CHANGED')
    else:common.save(path,report)
    print(json.dumps(report,sort_keys=True,allow_nan=False))
