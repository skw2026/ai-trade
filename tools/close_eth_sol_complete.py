"""Summarize and verify this consumed batch; never run or select another policy."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import subprocess

import eth_sol_complete_batch as batch
from eth_sol_complete_model import metrics
from audit_eth_sol_complete import audit_report_math

ROOT=batch.ROOT
PUBLIC=Path('docs/reviews/2026-10-15-eth-sol-complete-economic-result.json')
REPORT=PUBLIC.with_suffix('.md')


def utc(start,minute):
    return None if minute is None else dt.datetime.fromtimestamp((start+minute*60000)/1000,dt.timezone.utc).isoformat()


def summary():
    batch.guard();batch.verify_model();protected=batch.preserve()
    audit=batch.read(ROOT/'independent-audit.json');market=batch.read(ROOT/'market-result.json')
    batch.need(audit['status']=='INDEPENDENT_AUDIT_PASS' and audit['economic_status']=='NO_GO_RISK','EXPECTED_AUDITED_EXIT')
    batch.need(batch.sha(ROOT/'market-result.json')==audit['market_result_sha256'],'AUDITED_MARKET_CHANGED')
    batch.need(all(batch.sha(p)==h for p,h in {**market['files'],**audit['audit_files']}.items()),'AUDITED_EVIDENCE_CHANGED')
    selected=market['selection']['selected_index'];results={};arms={}
    for label,index in (('baseline',0),('candidate',selected),('latency',None)):
        key='latency' if index is None else 'full-'+str(index)
        r=batch.read(ROOT/(key+'-result.json'));s=batch.read(ROOT/(key+'-snapshot.json'))
        a=batch.read(ROOT/(key+'-independent-audit.json'));results[label]=r;measured=metrics(r)
        arms[label]=dict(parameters=r['parameters'],extra_delay_minutes=r['extra_delay'],
                        calendar_minutes=r['processed_minutes'],stop_utc=utc(r['start'],r['stop_minute']),books={})
        for i,k in enumerate(('base','stress')):
            p=r[k];last_flat=next((e['minute'] for e in reversed(s['paths'][i]['events']) if e['kind']=='flat'),None)
            arms[label]['books'][k]=dict(net_usdt=p['cash']-10000,net_fraction_initial=(p['cash']-10000)/10000,
                gross_usdt=p['gross_cash'],fees_usdt=p['fees'],slippage_usdt=p['slippage'],signed_funding_cost_usdt=p['funding'],
                maximum_reference_drawdown=p['maximum_drawdown'],stop=p['stop'],insufficient=p['insufficient'],
                positions=p['positions'],fills=a[i]['fills'],full_completed_episodes=p['completed_episodes'],
                failed_entries=p['failed_entries'],position_minutes=r['position_minutes'][i],
                average_gross_over_calendar_usdt=r['notional_minutes'][i]/r['requested_minutes'],
                first_two_negative_windows_utc=utc(r['start'],p['first_legacy_withdrawal']),
                last_flat_utc=utc(r['start'],last_flat),evaluation=measured[k])
    diagnostic=dict(status=market['decision']['status'],**{k:metrics(r) for k,r in results.items()})
    audit_report_math(diagnostic,market['confirmation_planning'],results)
    grid=[]
    for i in range(9):
        r=batch.read(ROOT/('q1-'+str(i)+'-result.json'))
        grid.append(dict(index=i,parameters=r['parameters'],base_net_usdt=r['last_liquidation'][0]-10000,
                         stress_net_usdt=r['last_liquidation'][1]-10000,eligible=market['selection']['eligible'][i]))
    state=batch.read(ROOT/'validation-state.json')
    failures=[e for e in state['history'] if e.get('event')=='validation' and e.get('exit_code') not in (None,0)]
    batch.need(len(failures)==2,'FAILURE_HISTORY_COUNT')
    expected={'2061466ee08f4f759b74f5292e7d8bbd','6f9233ab96d140c8885a29b507653a26'}
    retried={e.get('retry_of') for e in state['history'] if e.get('exit_code')==0}
    batch.need(expected<=retried,'ORIGINAL_COMMAND_RETRIES_REQUIRED')
    return dict(schema='complete_development_economic_result_v1',status='CLOSED_NO_GO',reason='NO_GO_RISK',
        experiment='ETH_SOL_2024_COMPLETE_DEVELOPMENT',selected_index=selected,selected_parameters=market['selection']['parameters'],
        q1_grid=grid,arms=arms,market_batches=1,q1_vector_pairs=9,continuation_pairs=2,latency_pairs=1,
        independent_books=24,model_regressions_at_freeze=93,model_test_skips=0,technical_failures=2,
        technical_failures_repaired_without_market_rerun=True,protected_files=protected,
        new_market_requests=0,account_operations=0,actual_fills_verified=False,deployments=0,
        all_history_development_only=True,online_hyperparameter_updates=0,independent_confirmation_attempts=0,
        candidate_count=0,g4='NOT_ACHIEVED',demo_new_positions_allowed=False,observation_started=False,
        confirmation_planning=market['confirmation_planning'],compute_seconds=audit['total_compute_seconds'],
        budget=dict(execution_charged_hours=1,design_reserved_hours=2,total_cap_hours=20,
                    charged_through_utc='2026-10-15T06:44:36+00:00',active_engineering_days=1),
        evidence=dict(model_freeze_sha256=batch.sha(ROOT/'model-freeze.json'),
                      market_result_sha256=batch.sha(ROOT/'market-result.json'),
                      independent_audit_sha256=batch.sha(ROOT/'independent-audit.json')))


def close():
    value=summary();batch.save(ROOT/'closed.json',dict(public=value,summary_source_sha256=batch.sha(__file__)))
    print(json.dumps(value,indent=2,sort_keys=True,allow_nan=False),flush=True)


def local():
    value=summary();closed=batch.read(ROOT/'closed.json')
    batch.need(batch.sha(__file__)==closed['summary_source_sha256'],'CLOSURE_SOURCE_CHANGED')
    batch.need(value==closed['public']==batch.read(PUBLIC),'PUBLIC_RESULT_BINDING')
    batch.need(dt.datetime.now(dt.timezone.utc)<=dt.datetime.fromisoformat(value['budget']['charged_through_utc']),'CHARGED_TIME_EXCEEDED')
    text=REPORT.read_text()
    for target in re.findall(r'\]\(([^)]+)\)',text):
        if not target.startswith(('https://','http://')):batch.need((REPORT.parent/target).exists(),'REPORT_LINK')
    for p in (PUBLIC,REPORT,Path(__file__),Path('tools/test_eth_sol_complete_delivery.py')):
        text=p.read_text();batch.need(text.endswith('\n') and all(v==v.rstrip() for v in text.splitlines()),'WHITESPACE')
    subprocess.run(['git','diff','--check'],check=True)
    batch.save(ROOT/'delivery.json',dict(status='AUDITED_NEGATIVE_RESULT_HANDOFF_PASS',economic_status=value['reason'],
         files={str(p):batch.sha(p) for p in (PUBLIC,REPORT,Path(__file__),Path('tools/test_eth_sol_complete_delivery.py'))},
         protected_files=batch.preserve(),g4=False))
    print('AUDITED_NEGATIVE_RESULT_HANDOFF_PASS; ECONOMIC=NO_GO_RISK; G4=false',flush=True)


def remote(sha):
    batch.guard();batch.verify_model();batch.preserve()
    batch.need(re.fullmatch('[0-9a-f]{40}',sha) is not None,'FULL_SHA_REQUIRED')
    def git(*args):return subprocess.check_output(['git',*args],text=True,timeout=30).strip()
    batch.need(git('branch','--show-current')=='main' and git('rev-parse','HEAD')==sha,'LOCAL_MAIN_SHA')
    batch.need('[skip ci]' in git('show','-s','--format=%B',sha),'SKIP_CI_REQUIRED')
    def api(endpoint,params=()):
        args=['gh','api','--method','GET','repos/skw2026/ai-trade/'+endpoint]
        for v in params:args+=['-f',v]
        return json.loads(subprocess.check_output(args,text=True,timeout=45))
    branch=api('branches/main');runs=api('actions/runs',[f'head_sha={sha}','per_page=100'])
    deployments=api('deployments',[f'sha={sha}','per_page=100'])
    batch.need(branch['commit']['sha']==sha,'REMOTE_MAIN_SHA')
    batch.need(runs['total_count']==0 and runs['workflow_runs']==[] and deployments==[],'UNEXPECTED_RESEARCH_AUTOMATION')
    value=dict(status='EXACT_SHA_SKIP_CI_VERIFIED',sha=sha,actions=0,deployments=0,
               verified_utc=dt.datetime.now(dt.timezone.utc).isoformat(),economic_status='NO_GO_RISK')
    batch.save(ROOT/'publication.json',value);print(json.dumps(value),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=('close','check','remote'));p.add_argument('--sha')
    args=p.parse_args()
    if args.action=='remote':remote(args.sha)
    elif args.action=='close':close()
    else:local()
