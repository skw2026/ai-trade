"""One frozen formation baseline, with two costs and independent cash audit.

A predeclared economic rejection is a result, not technical PASS or G4.
"""
import argparse
from array import array
from collections import deque
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

import eth_sol_intake as intake
import eth_sol_market as market
from eth_sol_model import PairLedger, estimate
from mvp_reference_inputs import strict_json

ROOT=intake.ROOT
need,sha,save=intake.need,intake.sha,intake.save
SOURCE_PATHS=('tools/eth_sol_model.py','tools/test_eth_sol_model.py','tools/run_eth_sol_baseline.py',
    'tools/audit_eth_sol_baseline.py','tools/test_eth_sol_baseline.py','tools/execution_net_learning.py',
    'tools/run_bounded_learning.py','tools/audit_eth_sol_input.py','tools/test_eth_sol_input_audit.py',
    'src/core/types.h','src/risk/risk_engine.cpp')


def sources():return {p:sha(p) for p in SOURCE_PATHS}


def guard():
    need(strict_json((ROOT/'validation-state.json').read_bytes())['status']=='RUNNING','PROGRAM_GATE_NOT_RUNNING')
    c=strict_json(market.CONTRACT.read_bytes())
    f=strict_json((ROOT/'market-freeze.json').read_bytes())
    market.verify_freeze(f)
    need(dt.datetime.now(dt.timezone.utc)<intake.START+dt.timedelta(hours=12.25),'BASELINE_CUMULATIVE_14H_BUDGET')
    if (ROOT/'model-freeze.json').exists():
        mf=strict_json((ROOT/'model-freeze.json').read_bytes())
        need(mf['sources']==sources(),'MODEL_SOURCE_CHANGED')
        need(mf['input_audit_sha256']==sha(ROOT/'input-audit.json'),'COMPILED_INPUT_CHANGED')
    return c


def freeze():
    os.umask(0o077)
    c=guard();intake.preserve()
    from validation_gate import command_hash
    expected=command_hash(['python3','-m','unittest','discover','-s','tools','-p','test_eth_sol*.py','-v'],str(Path.cwd().resolve()))
    evidence=[e for e in strict_json((ROOT/'validation-state.json').read_bytes())['history']
        if e.get('event')=='validation' and e.get('exit_code')==0 and e.get('command_sha256')==expected]
    need(evidence,'SYNTHETIC_EXECUTION_REGRESSIONS_REQUIRED')
    a=strict_json((ROOT/'input-audit.json').read_bytes())
    binding=strict_json((ROOT/'collection-audit.json').read_bytes())
    need(binding['status']=='RAW_COLLECTION_BINDING_PASS' and all(sha(p)==h for p,h in binding['files'].items()),'RAW_COLLECTION_CHANGED')
    need(a['status']=='G0_INPUT_COMPLETE_DEVELOPMENT_ONLY','G0_INPUT_NOT_COMPLETE')
    need(all(sha(p)==h for p,h in a['files'].items()),'COMPILED_INPUT_HASH')
    save(ROOT/'model-freeze.json',dict(sources=sources(),input_audit_sha256=sha(ROOT/'input-audit.json'),
        contract_sha256=sha(market.CONTRACT),frozen_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        economic_attempts_before_freeze=0,parameters=c['selection']['baseline'],
        regression_evidence=evidence[-1],
        implementation_notes=dict(ohlc_risk='conservative favorable/adverse joint envelope, not a realizable joint tick path',
            causal_order='completed prior data decisions, funding, precommitted fills, then newly observed intraminute-risk response',
            reference_liquidation_distance='1x zero-maintenance reference cushion/absolute mark notional; below 8 percent withdraws; actual exchange liquidation remains UNKNOWN'),
        maximum_economic_seconds=1800,private_result_only=True))
    print(json.dumps(dict(status='G1_REFERENCE_MODEL_FROZEN',actual_execution_verified=False,economic_attempts=0)),flush=True)


class Archive:
    def __init__(self):
        a=strict_json((ROOT/'input-audit.json').read_bytes())
        need(a['byteorder']==sys.byteorder,'ARCHIVE_BYTEORDER')
        need(all(sha(p)==h for p,h in a['files'].items()),'ARCHIVE_HASH')
        self.start=a['start_ms'];self.end=a['end_ms'];self.arrays={}
        for symbol in intake.SYMBOLS:
            for kind in ('trade','mark'):
                p=ROOT/'compiled'/f'{symbol}-{kind}.f64';values=array('d')
                with p.open('rb') as stream:values.fromfile(stream,p.stat().st_size//8)
                self.arrays[symbol,kind]=values
        self.funding={s:{int(k):float(v) for k,v in d.items()} for s,d in
            strict_json((ROOT/'compiled'/'funding.json').read_bytes()).items()}
        warm=strict_json((ROOT/'compiled'/'warmup.json').read_bytes())
        times=sorted(map(int,warm['ETHUSDT']))
        self.warmup=[tuple(float(warm[s][str(t)]) for s in intake.SYMBOLS) for t in times]

    def bars(self,t,kind):
        need(self.start<=t<self.end and (t-self.start)%60000==0,'ARCHIVE_TIME')
        index=(t-self.start)//60000;width=5 if kind=='trade' else 4
        return [list(self.arrays[s,kind][index*width:(index+1)*width]) for s in intake.SYMBOLS]

    def rates(self,t):return {i:self.funding[s][t] for i,s in enumerate(intake.SYMBOLS) if t in self.funding[s]}


def simulate(archive,start,end,parameters,maximum_seconds=1800):
    """Fixed order: completed data decision; funding; committed fills; end-bar risk.

    Intraminute adverse prices cannot cancel previously committed same-minute fills.
    """
    paths=[PairLedger(False,*parameters),PairLedger(True,*parameters)]
    hours=deque(archive.warmup,maxlen=1440);need(len(hours)==1440,'WARMUP_LENGTH')
    maximum_minutes=(end-start)//60000
    model=None;stop_at=None;weekly=[];timer=time.monotonic();last_liquid=[10000.,10000.]
    minute=-1
    for minute in range(maximum_minutes):
        if minute%1440==0:need(time.monotonic()-timer<maximum_seconds,'ECONOMIC_COMPUTE_BUDGET')
        t=start+minute*60000
        previous_trade=archive.bars(t-60000,'trade');previous_mark=archive.bars(t-60000,'mark')
        if minute and minute%60==0:hours.append(tuple(b[3] for b in previous_trade))
        if minute%1440==0:model=estimate(hours)
        if minute and minute%5==0:
            for path in paths:path.close_minute(minute,[b[3] for b in previous_mark],True)
        if any(p.stopped or p.insufficient for p in paths) and stop_at is None:stop_at=minute
        if stop_at is not None:
            for path in paths:
                if not path.stopped:path.stop(minute,'PEER_PATH_REJECTED')
        if minute%5==0 and stop_at is None:
            for path in paths:path.decision(minute,[b[3] for b in previous_trade],model,maximum_minutes)
        trade,mark=archive.bars(t,'trade'),archive.bars(t,'mark')
        rates=archive.rates(t)
        for path in paths:
            path.fund(minute,rates,[b[0] for b in mark])
            prior_reasons=path.risk(minute,mark,defer=True)
            path.process(minute,trade)
            # These are end-of-minute observations. New orders cannot act until
            # the next minute; decisions earlier in this minute used only T-1.
            for reason in prior_reasons:path.stop(minute,reason)
            path.risk(minute,mark)
        last_liquid=[p.close_minute(minute+1,[b[3] for b in mark]) for p in paths]
        if any(p.stopped or p.insufficient for p in paths) and stop_at is None:
            stop_at=minute+1
            for path in paths:
                if not path.stopped:path.stop(minute,'PEER_PATH_REJECTED')
        # Week boundary: UTC Monday start (epoch Thursday, offset three days).
        if ((t+60000)//60000+3*1440)%(7*1440)==0:
            weekly.append(dict(end_ms=t+60000,liquidation_equity=last_liquid))
        if any(p.insufficient for p in paths):break
        if stop_at is not None and all(p.flat and not p.queue for p in paths):break
    for path in paths:
        if minute+1==maximum_minutes and maximum_minutes%5==0:
            path.close_minute(maximum_minutes,[b[3] for b in mark],True)
        if not path.flat or path.queue:
            path.insufficient=path.insufficient or 'DOMAIN_ENDED_WITH_RESIDUAL_OR_PENDING'
    passed=stop_at is None and all(p.flat and not p.queue and p.cash>10000 and p.episodes>0 and
        not p.stopped and not p.insufficient for p in paths)
    status=('INSUFFICIENT_EXECUTION' if any(p.insufficient for p in paths) else
            'REJECT_RISK_OR_SAFETY' if any(p.stopped for p in paths) else
            'FIXED_FORMATION_BASELINE_SUPPORTED' if passed else 'REJECT_FIXED_BASELINE_NET_OR_ACTIVITY')
    result=dict(status=status,parameters=list(parameters),formation_start_ms=start,formation_end_ms=end,
        processed_minutes=minute+1,requested_minutes=maximum_minutes,stop_minute=stop_at,
        base=paths[0].result(),stress=paths[1].result(),terminal_liquidation_equity=last_liquid,
        weekly=weekly,passed_to_learning=passed,compute_seconds=time.monotonic()-timer,
        actual_fills_verified=False,confirmation_started=False,trained_vectors=1,evaluation_attempts=0)
    return result,paths


def baseline():
    os.umask(0o077);c=guard();intake.preserve()
    need((ROOT/'model-freeze.json').exists(),'MODEL_NOT_FROZEN')
    need(not (ROOT/'baseline-attempt.json').exists(),'BASELINE_ALREADY_STARTED')
    save(ROOT/'baseline-attempt.json',dict(started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        parameters=c['selection']['baseline'],model_freeze_sha256=sha(ROOT/'model-freeze.json')))
    result,paths=simulate(Archive(),market.ms(c['formation_start_utc']),market.ms(c['formation_end_utc']),c['selection']['baseline'])
    event_hashes={}
    for name,path in zip(('base','stress'),paths):
        p=ROOT/f'baseline-{name}-events.json';save(p,path.events);event_hashes[str(p)]=sha(p)
    result['event_hashes']=event_hashes;save(ROOT/'baseline-result.json',result)
    intake.preserve()
    print(json.dumps({k:result[k] for k in ('status','processed_minutes','requested_minutes','passed_to_learning','compute_seconds')},sort_keys=True),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('freeze','baseline'))
    args=p.parse_args();{'freeze':freeze,'baseline':baseline}[args.action]()
