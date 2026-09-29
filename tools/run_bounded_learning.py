"""One predeclared development comparison. Final confirmation is unavailable.

No transport, training subprocess search, account access, or promotion path.
The private attempt is exclusive, including interrupted/failed attempts.
"""
import argparse
from collections import Counter
import csv
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT/'.artifacts/bounded-learning-20260929'
CONTRACT = ROOT/'docs/plans/2026-09-29-bounded-learning.contract.json'
BINARY = RUN/'bounded_learning_driver'
SOURCES = ['tools/bounded_learning_driver.cpp', 'tools/run_bounded_learning.py',
           'src/evolution/self_evolution_controller.cpp', 'src/evolution/self_evolution_controller.h',
           'src/strategy/strategy_engine.cpp', 'src/strategy/strategy_engine.h',
           'src/regime/regime_engine.cpp', 'src/regime/regime_engine.h',
           'src/research/online_feature_engine.cpp', 'src/research/online_feature_engine.h',
           'src/research/time_series_operators.cpp', 'src/research/time_series_operators.h',
           'src/oms/account_state.cpp', 'src/oms/account_state.h',
           'src/core/config.h', 'src/core/types.h', 'src/core/log.cpp', 'src/core/log.h']
ARMS = ('fixed','adaptive','fixed_stress','adaptive_stress','fixed_unit','adaptive_unit')

def need(ok, why):
    if not ok:
        raise ValueError(why)

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def save(p, value):
    with p.open('x') as f:
        json.dump(value,f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def read_contract():
    c = json.loads(CONTRACT.read_text())
    start=dt.datetime(2026,9,29,14,40,40,tzinfo=dt.timezone.utc)
    need(0<=(dt.datetime.now(dt.timezone.utc)-start).total_seconds()<=16*3600,'SHARED_STAGE_BUDGET')
    need(c['confirmation']['maximum_runs'] == 0 and not c['confirmation']['learning_allowed']
         and c['confirmation']['interval'] is None, 'CONFIRMATION_NOT_ADMITTED')
    b = json.loads((RUN/'baseline.json').read_text())
    for p,h in b['preserved'].items():
        need(sha(ROOT/p) == h, 'PRESERVED_CHANGED:'+p)
    return c

def preflight():
    from mvp_reference_inputs import compile_archive
    c = read_contract()
    compiled, proof = compile_archive(ROOT/'.artifacts/mvp-reference-history-20260921/archive/manifest.json')
    need(hashlib.sha256(compiled).hexdigest() == c['input']['sha256'], 'RAW_TO_CSV_MISMATCH')
    need(proof == json.loads((ROOT/c['input']['proof']).read_text()), 'OLD_INPUT_PROOF_CHANGED')
    # Do not create a replacement consumption ledger. All this input is development.
    identities = {p:sha(ROOT/p) for p in SOURCES}
    save(RUN/'execution-freeze.json', dict(contract_sha256=sha(CONTRACT), sources=identities,
         binary_sha256=sha(BINARY), input_sha256=c['input']['sha256'], frozen_utc=now(),
         raw_pages=proof['raw_pages'], bars=proof['bars'], funding_events=proof['funding_events'],
         scope='DEVELOPMENT_ONLY', confirmation_runs=0))
    print('FROZEN raw pages',proof['raw_pages'],'bars',proof['bars'],'confirmation NOT_ADMITTED')

@dataclass
class Wallet:
    multiplier: int = 1
    cash: float = 10000.0
    qty: float = 0.0
    peak: float = 10000.0
    drawdown: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    funding: float = 0.0
    turnover: float = 0.0
    fills: int = 0

    def equity(self, mark):
        return self.cash+self.qty*mark

    def observe(self, high, low):
        a,b = self.equity(high),self.equity(low)
        self.peak = max(self.peak,a,b)
        self.drawdown = max(self.drawdown,1-min(a,b)/self.peak)

    def fund(self, mark, rate):
        paid = self.qty*mark*rate
        self.cash -= paid
        self.funding += paid
        self.observe(mark,mark)

    def trade(self, target, price, mark):
        desired = math.trunc(target/price*1000)/1000
        delta = desired-self.qty
        if abs(delta) < 1e-10:
            return
        side = 1 if delta > 0 else -1
        adverse = price*(1+side*self.multiplier*.0001)
        fill = (math.ceil(adverse*10-1e-9) if side>0 else math.floor(adverse*10+1e-9))/10
        fee = abs(delta)*fill*self.multiplier*.00055
        self.cash -= delta*fill+fee
        self.qty = desired
        self.fees += fee
        self.slippage += delta*(fill-price)
        self.turnover += abs(delta)*fill
        self.fills += 1
        self.observe(mark,mark)

    def snapshot(self, mark):
        return dict(equity=self.equity(mark),cash=self.cash,qty=self.qty,
                    max_drawdown=self.drawdown,fees=self.fees,slippage=self.slippage,
                    funding_paid=self.funding,turnover=self.turnover,fills=self.fills)

def targets(signal):
    t,d,w = signal['trend'],signal['defensive'],signal['weight']
    need(all(math.isfinite(x) for x in (t,d,w)) and .4-1e-9<=w<=.6+1e-9, 'SIGNAL_WEIGHT')
    fixed, adaptive = .5*(t+d), w*t+(1-w)*d
    need(max(abs(fixed),abs(adaptive)) <= 2500+1e-8,'NOMINAL_CAP')
    unit = lambda x: 2500.0 if x>1e-8 else -2500.0 if x < -1e-8 else 0.0
    return dict(fixed=fixed,adaptive=adaptive,fixed_stress=fixed,adaptive_stress=adaptive,
                fixed_unit=unit(fixed),adaptive_unit=unit(adaptive))

def hac_lower(values):
    if len(values)<26:
        return None
    n=len(values); mean=statistics.mean(values); centered=[v-mean for v in values]
    long_variance=sum(x*x for x in centered)/n
    for lag in range(1,5):
        covariance=sum(centered[i]*centered[i-lag] for i in range(lag,n))/n
        long_variance += 2*(1-lag/5)*covariance
    return mean-1.6448536269514722*math.sqrt(max(0,long_variance)/n)

def decide(reason, wallets, updated, weekly):
    if reason != 'COMPLETE':
        return reason
    paired=[r['adaptive']-r['fixed'] for r in weekly]
    unit=[r['adaptive_unit']-r['fixed_unit'] for r in weekly]
    lower=[hac_lower([r['adaptive_stress'] for r in weekly]),hac_lower(paired),hac_lower(unit)]
    positive = all(wallets[k].cash>10000 for k in ('adaptive','adaptive_stress'))
    if updated and positive and all(x is not None and x>0 for x in lower):
        return 'DEVELOPMENT_SUPPORTED_CONFIRMATION_UNAVAILABLE'
    return 'NO_DEVELOPMENT_EVIDENCE'

def protocol(row, active, wallet):
    return ' '.join(str(x) for x in [int(row['timestamp'])+300000,row['open'],row['high'],
        row['low'],row['price'],row['volume'],row['mark_close'],row['funding_rate_per_interval'],
        int(active),wallet.drawdown,wallet.equity(float(row['mark_close']))])+'\n'

def execute():
    c=read_contract(); identity=json.loads((RUN/'execution-freeze.json').read_text())
    need(identity['contract_sha256']==sha(CONTRACT) and identity['binary_sha256']==sha(BINARY),'EXECUTION_IDENTITY')
    need(all(sha(ROOT/p)==h for p,h in identity['sources'].items()),'SOURCE_CHANGED')
    started=time.monotonic()
    save(RUN/'market-attempt.json',dict(started_utc=now(),execution_freeze_sha256=sha(RUN/'execution-freeze.json'),
                                      maximum_attempts=1,confirmation_runs=0))
    wallets={k:Wallet(2 if k.endswith('stress') else 1) for k in ARMS}
    pending=dict.fromkeys(ARMS,0.0); actions=Counter(); updates=0; seen=0; last_signal=None
    reason='COMPLETE'; boundary=None; week_start=c['input']['development_start_ms']; week_ms=7*86400000
    weekly=[]; prior_week=dict.fromkeys(ARMS,10000.0); last_row=None; rows_processed=0
    terminal_flat=False; settlement_time=None; value_mark=0
    p=subprocess.Popen([str(BINARY),'development'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE,text=True,bufsize=1)
    try:
        with (ROOT/c['input']['csv']).open() as f, (RUN/'trace.jsonl').open('x') as trace:
            reader=csv.DictReader(f)
            for row in reader:
                ts=int(row['timestamp'])
                if ts>=c['input']['development_end_exclusive_ms']:
                    break
                need(time.monotonic()-started<c['maximum_compute_seconds'],'COMPUTE_BUDGET')
                active=ts>=c['input']['development_start_ms']
                op,mo,mc,rate=map(float,(row['open'],row['mark_open'],row['mark_close'],row['funding_rate_per_interval']))
                last_row=row; rows_processed+=1;value_mark=mc
                if active:
                    seen+=1
                    for k,w in wallets.items():
                        w.fund(mo,rate)
                    if any(wallets[k].drawdown>=.08 for k in ARMS[:4]):
                        reason='REFERENCE_RISK_STOP';boundary='OPEN_FUNDING'
                    if reason=='COMPLETE':
                        for k,w in wallets.items():
                            w.trade(pending[k],op,mo)
                            w.observe(float(row['mark_high']),float(row['mark_low']))
                        if any(wallets[k].drawdown>=.08 for k in ARMS[:4]):
                            reason='REFERENCE_RISK_STOP';boundary='POST_TRADE_OR_INTRABAR'
                if reason!='COMPLETE':
                    trace.write(json.dumps(dict(timestamp=ts,boundary=boundary,signal=None,
                        wallets={k:w.snapshot(mc) for k,w in wallets.items()}),allow_nan=False)+'\n')
                    break  # No controller call on or after a risk-failed bar.
                need(p.stdin is not None and p.stdout is not None,'DRIVER_PIPES')
                p.stdin.write(protocol(row,active,wallets['adaptive']));p.stdin.flush()
                raw=p.stdout.readline();need(bool(raw),'DRIVER_TERMINATED')
                s=json.loads(raw);need(s['ts']==ts+300000,'SIGNAL_TIME')
                last_signal=s
                next_target=targets(s) if active else dict.fromkeys(ARMS,0.0)
                if s['action']:
                    actions[s['action']]+=1
                updates+=int(s['updated'])
                trace.write(json.dumps(dict(timestamp=ts,signal=s,executed_targets=pending,
                    wallets={k:w.snapshot(mc) for k,w in wallets.items()}),allow_nan=False)+'\n')
                pending=next_target
                if active and ts+300000 >= week_start+week_ms:
                    values={k:w.equity(mc) for k,w in wallets.items()}
                    weekly.append({k:values[k]-prior_week[k] for k in ARMS})
                    prior_week=values;week_start+=week_ms
                if s['withdrawn']:
                    reason='SAFETY_WITHDRAWAL';break
            # A known safety withdrawal can flatten at the immediately following open.
            # Risk ambiguity never invents an executable intrabar liquidation.
            if reason=='SAFETY_WITHDRAWAL':
                next_row=next(reader,None)
                need(next_row is not None and int(next_row['timestamp'])==int(last_row['timestamp'])+300000,
                     'SAFETY_NEXT_OPEN_MISSING')
                settlement_time=int(next_row['timestamp']);value_mark=float(next_row['mark_open'])
                for w in wallets.values():
                    w.fund(value_mark,float(next_row['funding_rate_per_interval']))
                if any(wallets[k].drawdown>=.08 for k in ARMS[:4]):
                    reason='REFERENCE_RISK_STOP';boundary='SAFETY_EXIT_OPEN'
                else:
                    for w in wallets.values():
                        w.trade(0,float(next_row['open']),value_mark)
                    terminal_flat=True
            elif reason=='COMPLETE':
                need(last_row is not None,'EMPTY_REPLAY')
                need(int(last_row['timestamp'])+300000==c['input']['development_end_exclusive_ms'],
                     'INCOMPLETE_DEVELOPMENT_COVERAGE')
                settlement_time=int(last_row['timestamp'])+300000
                for w in wallets.values():
                    w.trade(0,float(last_row['price']),value_mark)
                terminal_flat=True
            if terminal_flat and any(wallets[k].drawdown>=.08 for k in ARMS[:4]):
                reason='REFERENCE_RISK_STOP';boundary='TERMINAL_FEES'
            trace.write(json.dumps(dict(terminal=True,settlement_timestamp_ms=settlement_time,
                reason=reason,terminal_flat=terminal_flat,value_mark=value_mark,
                wallets={k:w.snapshot(value_mark) for k,w in wallets.items()}),allow_nan=False)+'\n')
    finally:
        if p.stdin: p.stdin.close()
        try:
            code=p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill();p.wait();raise ValueError('DRIVER_EXIT_TIMEOUT')
        stderr=p.stderr.read() if p.stderr else ''
        if p.stdout:p.stdout.close()
        if p.stderr:p.stderr.close()
        need(code==0,'DRIVER_FAILURE:'+stderr[:500])
    need(last_row is not None and last_signal is not None,'EMPTY_REPLAY')
    result=dict(schema='bounded_learning_development_result_v1',verdict=decide(reason,wallets,updates,weekly),
        scope='DEVELOPMENT_ONLY',candidate_status='NO_QUALIFIED_CANDIDATE',confirmation_status='NOT_ADMITTED',
        market_runs=1,confirmation_runs=0,model_training_runs=0,controller_updates=updates,actions=dict(actions),
        evaluated_bars=seen,processed_bars=rows_processed,stop_timestamp_ms=int(last_row['timestamp']),
        stop_reason=reason,risk_boundary=boundary,terminal_flat=terminal_flat,full_development_period=reason=='COMPLETE',
        wallets={k:w.snapshot(value_mark) for k,w in wallets.items()},settlement_timestamp_ms=settlement_time,
        final_weights=last_signal['weights'],complete_weekly_blocks=len(weekly),weekly=weekly,
        execution_freeze_sha256=sha(RUN/'execution-freeze.json'),trace_sha256=sha(RUN/'trace.jsonl'),
        completed_utc=now(),compute_seconds=time.monotonic()-started,account_access=0,network_requests=0,
        publication_status='BLOCKED_UNCHANGED')
    save(RUN/'result.json',result)
    print(json.dumps(result,sort_keys=True,allow_nan=False))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('preflight','run'))
    args=p.parse_args()
    preflight() if args.action=='preflight' else execute()
