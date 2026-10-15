"""Verify the negative-result handoff, optionally its exact-SHA skip-CI delivery."""
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import subprocess

import eth_sol_intake as intake
from mvp_reference_inputs import strict_json
from run_eth_sol_baseline import guard, sources, ROOT

PUBLIC=Path('docs/reviews/2026-10-15-eth-sol-baseline-result.json')
REPORT=PUBLIC.with_suffix('.md')


def local():
    guard();intake.preserve()
    closed=strict_json((ROOT/'closed.json').read_bytes());public=strict_json(PUBLIC.read_bytes())
    expected=dict(closed,arms={k:{n:v for n,v in arm.items() if n!='last_three_safety_checks'} for k,arm in closed['arms'].items()})
    intake.need(all(public[k]==v for k,v in expected.items()),'PUBLIC_RESULT_DIFFERS_FROM_AUDIT')
    intake.need(public['status']=='CLOSED_NO_GO' and public['candidate_count']==public['learned_vectors']==0 and
        not public['forward_scheduled'] and public['g4']=='NOT_ACHIEVED','NEGATIVE_BOUNDARY_LOST')
    collection=strict_json((ROOT/'market-collection.json').read_bytes())
    intake.need(public['input']['public_request_attempts']==collection['attempts'] and
        public['input']['new_wire_bytes']==collection['bytes'] and public['input']['new_pages']==len(collection['records']), 'PUBLIC_INPUT_COUNTS')
    need_until=dt.datetime.fromisoformat(public['budget']['charged_through_utc'].replace('Z','+00:00'))
    intake.need(dt.datetime.now(dt.timezone.utc)<=need_until,'DELIVERY_EXCEEDED_CHARGED_TIME')
    intake.need(intake.sha('tools/close_eth_sol_batch.py')==closed['summary_source_sha256'],'CLOSURE_SOURCE_CHANGED')
    mf=strict_json((ROOT/'model-freeze.json').read_bytes());intake.need(mf['sources']==sources(),'FROZEN_MODEL_CHANGED')
    state=strict_json((ROOT/'validation-state.json').read_bytes())
    failures=[h for h in state['history'] if h.get('event')=='validation' and h.get('exit_code') not in (None,0)]
    intake.need(len(failures)==1 and any(h.get('retry_of')=='87ecc721c1a746b8a36f6bfd8eb2f3f2' and h.get('exit_code')==0
        for h in state['history']),'FAILURE_OR_EXACT_RETRY_HISTORY_LOST')
    text=REPORT.read_text()
    for target in re.findall(r'\]\(([^)]+)\)',text):
        if not target.startswith(('https://','http://')):intake.need((REPORT.parent/target).exists(),'REPORT_LINK')
    for p in list(Path('tools').glob('*eth_sol*.py'))+[PUBLIC,REPORT,
        Path('docs/plans/2026-10-15-eth-sol-execution.md'),Path('docs/plans/2026-10-15-eth-sol-economic.contract.json')]:
        raw=p.read_text();intake.need(raw.endswith('\n') and all(line==line.rstrip() for line in raw.splitlines()),'HANDOFF_WHITESPACE')
    subprocess.run(['git','diff','--check'],check=True)
    print(json.dumps(dict(status='NEGATIVE_RESULT_HANDOFF_VALIDATED',economic_status=closed['reason'],
        protected_files=intake.preserve(),fixed_baselines=1,learning_searches=0,confirmation_attempts=0)),flush=True)


def remote(sha):
    guard()
    intake.need(re.fullmatch('[0-9a-f]{40}',sha) is not None,'FULL_SHA_REQUIRED')
    def git(*args):return subprocess.check_output(['git',*args],text=True,timeout=30).strip()
    intake.need(git('branch','--show-current')=='main' and git('rev-parse','HEAD')==sha,'LOCAL_MAIN_SHA')
    intake.need('[skip ci]' in git('show','-s','--format=%B',sha),'RESEARCH_SKIP_CI_REQUIRED')
    def api(endpoint,params=()):
        command=['gh','api','--method','GET','repos/skw2026/ai-trade/'+endpoint]
        for value in params:command+=['-f',value]
        return json.loads(subprocess.check_output(command,text=True,timeout=45))
    branch=api('branches/main');runs=api('actions/runs',[f'head_sha={sha}','per_page=100'])
    deployments=api('deployments',[f'sha={sha}','per_page=100'])
    intake.need(branch['commit']['sha']==sha,'REMOTE_MAIN_SHA')
    intake.need(runs['total_count']==0 and runs['workflow_runs']==[] and deployments==[],'UNEXPECTED_RESEARCH_AUTOMATION')
    result=dict(status='EXACT_SHA_SKIP_CI_VERIFIED',sha=sha,actions=0,deployments=0,
        verified_utc=dt.datetime.now(dt.timezone.utc).isoformat(),economic_status='CLOSED_NO_GO')
    intake.save(ROOT/'publication.json',result);print(json.dumps(result),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--sha');args=p.parse_args()
    remote(args.sha) if args.sha else local()
