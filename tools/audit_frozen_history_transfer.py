"""Read-only independent Decimal audit; never fits or chooses a new model."""
import csv
from decimal import Decimal as D
import json
import math
from pathlib import Path

from audit_execution_net_learning import Ledger, close
from audit_offline_policy_correction import read_domain, audit_action, sign, lower_bound
from run_bounded_learning import need, sha, save

ROOT=Path(__file__).resolve().parents[1]
RUN=ROOT/'.artifacts/frozen-history-transfer-20261001'
MODE='FROZEN_RESEARCH_DIAGNOSTIC_ONLY'


def audit_trace(path,domain,summary):
    need(sha(path)==summary['trace_sha256'],'TRACE_CHANGED')
    need(summary['coefficients'] in ([0,-4],[1,0]),'FIXED_VECTOR')
    books=[Ledger(False),Ledger(True)]
    reason='COMPLETE';stop_at=exit_at=first_diagnostic=None
    pending=latched=False;active=count=triggered=0
    nets,streak,exposure=[D(0)]*4,[0]*4,[0]*3
    weekly,week_start=[],[D(10000)]*2
    with Path(path).open() as source:
        events=(json.loads(line) for line in source)
        for i,sample in enumerate(domain,1):
            row=sample['row'];ts=int(row['timestamp']);mark=D(row['mark_close'])
            if pending:
                event=next(events)
                need(event['kind']=='EXIT_OPEN' and event['timestamp']==ts,'EXIT_TIME')
                for b in books:
                    b.fund(D(row['mark_open']),D(row['funding_rate_per_interval']))
                    b.trade(D(0),D(row['open']),D(row['mark_open']))
                    b.last,b.liquid=D(0),b.cash
                pending,latched,exit_at=False,True,ts
                need(event['reason']==reason and len(event['wallets'])==2,'EXIT_STATE')
                for b,s in zip(books,event['wallets']):b.check(s,mark)
            elif not latched:
                event=next(events)
                need(event['kind']=='BAR' and event['timestamp']==ts,'ACTIVE_TIME')
                target=audit_action(sample['features'],summary['coefficients'],sign(books[0].last))
                close(event['target'],target,'causal_action')
                for b in books:b.fund(D(row['mark_open']),D(row['funding_rate_per_interval']))
                opening=any(b.dd>=D('.08') for b in books)
                need(opening==event['opening_risk'],'OPENING_RISK')
                receipts=[b.step(row,target,funded=True,halt=opening) for b in books]
                need(len(event['receipts'])==len(event['wallets'])==2,'BOOK_COUNT')
                for actual,expected in zip(event['receipts'],receipts):close(actual,expected,'net_receipt')
                active+=1;assessment=None
                if any(b.dd>=D('.08') for b in books):
                    reason,stop_at,pending='REFERENCE_RISK_STOP',ts+300000,True
                else:
                    count+=1;nets[sample['bucket']]+=receipts[0];nets[3]+=receipts[0]
                    if count==240:
                        streak=[n+1 if p<0 else 0 for n,p in zip(streak,nets)]
                        trigger=any(n>=2 for n in streak);triggered+=int(trigger)
                        if trigger and first_diagnostic is None:first_diagnostic=ts+300000
                        assessment=dict(mode=MODE,streak=streak[:],would_withdraw=trigger,withdrawn=False)
                        need(event['safety'] is not None,'MISSING_DIAGNOSTIC')
                        need(len(event['safety']['net_by_decision_bucket_and_total'])==4,'DIAGNOSTIC_WIDTH')
                        for a,e in zip(event['safety']['net_by_decision_bucket_and_total'],nets):close(a,e,'diagnostic_net')
                        nets,count=[D(0)]*4,0
                if assessment is None:need(event['safety'] is None,'UNEXPECTED_DIAGNOSTIC')
                else:need(all(event['safety'][k]==v for k,v in assessment.items()),'DIAGNOSTIC_STATE')
                need(event['reason']==reason,'STOP_REASON')
                for b,s in zip(books,event['wallets']):b.check(s,mark)
            exposure[sign(books[0].q)+1]+=1
            if i%2016==0:
                values=[b.liquid for b in books]
                weekly.append([v-w for v,w in zip(values,week_start)]);week_start=values
        terminal=next(events)
        need(terminal['kind']=='TERMINAL' and next(events,None) is None,'TERMINAL_OR_EXTRA_EVENTS')
        need(terminal['result']=={k:v for k,v in summary.items() if k not in ('trace_file','trace_sha256')},'SUMMARY_CHANGED')
    row=domain[-1]['row'];mark=D(row['mark_close'])
    if not latched:
        for b in books:
            b.trade(D(0),D(row['price']),mark);b.last,b.liquid=D(0),b.cash
        if any(b.dd>=D('.08') for b in books):
            if stop_at is None:stop_at=int(row['timestamp'])+300000
            reason='REFERENCE_RISK_STOP'
        exit_at=int(row['timestamp'])+300000
    if len(domain)%2016==0:
        for k,b in enumerate(books):weekly[-1][k]+=b.cash-week_start[k]
        week_start=[b.cash for b in books]
    legacy=('REJECT_LEGACY_SAFETY' if first_diagnostic is not None else
            'REJECT_REFERENCE_RISK' if reason!='COMPLETE' else 'PATH_CONSTRAINTS_ONLY_NOT_QUALIFICATION')
    need(summary['safety_mode']==MODE and summary['qualification'] is False,'NO_QUALIFICATION')
    need(len(summary['wallets'])==len(summary['partial_week'])==2 and all(len(w)==2 for w in summary['weekly']),'SUMMARY_BOOK_COUNT')
    need(summary['first_legacy_withdrawal_ms']==first_diagnostic and summary['diagnostic_triggered_windows']==triggered and
         summary['legacy_path_status']==legacy,'LEGACY_DIAGNOSTIC_BINDING')
    need(summary['domain_bars']==len(domain) and summary['active_bars']==active,'COVERAGE')
    need(summary['reason']==reason and summary['stop_at_ms']==stop_at and summary['exit_at_ms']==exit_at and
         summary['terminal_flat'],'TERMINAL_STATE')
    need(summary['exposure_short_flat_long']==exposure and len(summary['weekly'])==len(weekly),'EXPOSURE_OR_WEEKS')
    for pair,expected in zip(summary['weekly'],weekly):
        for a,e in zip(pair,expected):close(a,e,'weekly')
    for k,b in enumerate(books):
        b.check(summary['wallets'][k],mark)
        close(summary['partial_week'][k],b.cash-week_start[k],'partial_week')
        close(sum(w[k] for w in summary['weekly'])+summary['partial_week'][k],b.cash-10000,'pnl_sum')
        need(b.q==0,'OPEN_TERMINAL_POSITION')
    close(summary['objective'],min(b.cash for b in books)-10000,'objective')
    return dict(active_bars=active,domain_bars=len(domain),reason=reason,
        first_legacy_withdrawal_ms=first_diagnostic,diagnostic_triggered_windows=triggered,
        legacy_path_status=legacy,economics=[dict(net=float(b.cash-10000),
        gross=float(b.cash-10000+b.fees+b.slip+b.funding),fees=float(b.fees),
        slippage=float(b.slip),funding=float(b.funding),fills=b.fills) for b in books])


def audit_concentration(path,summary,reported):
    # Separate implementation over independently audited receipts, not replay().
    episodes=[];entry=None;previous=[dict(qty=0,cash=10000,fills=0)]*2
    with Path(path).open() as source:
        for line in source:
            e=json.loads(line);terminal=e['kind']=='TERMINAL'
            b=e['result']['wallets'] if terminal else e['wallets']
            ts=e['result']['exit_at_ms'] if terminal else e['timestamp']
            if b[0]['qty']!=0:
                if entry is None:entry=dict(start_ms=ts,before=previous,bars=0)
                entry['bars']+=1
            elif entry is not None:
                episodes.append(dict(start_ms=entry['start_ms'],end_ms=ts,bars=entry['bars'],
                    net=[D(str(x['cash']))-D(str(y['cash'])) for x,y in zip(b,entry['before'])],
                    fills=[x['fills']-y['fills'] for x,y in zip(b,entry['before'])]))
                entry=None
            previous=b
    need(entry is None and len(episodes)==reported['episode_count']==len(reported['episodes']),'EPISODE_COUNT')
    clusters=[]
    for e,r in zip(episodes,reported['episodes']):
        need(all(e[k]==r[k] for k in ('start_ms','end_ms','bars','fills')),'EPISODE_IDENTITY')
        for a,b in zip(r['net'],e['net']):close(a,b,'episode_net')
        if not clusters or e['start_ms']-clusters[-1]['end_ms']>=604800000:
            clusters.append(dict(start_ms=e['start_ms'],end_ms=e['end_ms'],net=[D(0),D(0)],episodes=0))
        cluster=clusters[-1];cluster['end_ms']=e['end_ms'];cluster['episodes']+=1
        cluster['net']=[a+b for a,b in zip(cluster['net'],e['net'])]
    need(len(clusters)==reported['separated_clusters']==len(reported['clusters']),'CLUSTER_COUNT')
    for e,r in zip(clusters,reported['clusters']):
        need(all(e[k]==r[k] for k in ('start_ms','end_ms','episodes')),'CLUSTER_IDENTITY')
        for a,b in zip(r['net'],e['net']):close(a,b,'cluster_net')
    need(sum(e['bars'] for e in episodes)==sum(summary['exposure_short_flat_long'][::2]),'EXPOSURE_SUM')
    for k in (0,1):
        close(sum(e['net'][k] for e in episodes),D(str(summary['wallets'][k]['cash']))-10000,'episode_total')
        need(sum(e['fills'][k] for e in episodes)==summary['wallets'][k]['fills'],'FILL_SUM')
        close(reported['leave_largest_cluster_net'][k],sum((e['net'][k] for e in clusters),D(0))-
              max((e['net'][k] for e in clusters),default=D(0)),'leave_largest')
    need(reported['nonzero_full_blocks']==sum(any(v!=0 for v in w) for w in summary['weekly']),'ACTIVE_BLOCKS')


def audit_verdict(learned,fixed,clusters):
    if learned['reason']!='COMPLETE':return 'REJECT_REFERENCE_RISK',None
    if any(a['cash']<=10000 or a['cash']<=b['cash'] for a,b in zip(learned['wallets'],fixed['wallets'])):
        return 'REJECT_NET_EDGE',None
    if clusters['separated_clusters']<5:return 'INSUFFICIENT_EVENT_CAPACITY',None
    if any(n<=0 for n in clusters['leave_largest_cluster_net']):return 'INSUFFICIENT_CONCENTRATION',None
    bounds=dict(learned_stress=lower_bound([w[1] for w in learned['weekly']]),
        paired_base=lower_bound([a[0]-b[0] for a,b in zip(learned['weekly'],fixed['weekly'])]),
        paired_stress=lower_bound([a[1]-b[1] for a,b in zip(learned['weekly'],fixed['weekly'])]))
    if any(v is None or not v>0 for v in bounds.values()):return 'INSUFFICIENT_STATISTICAL_SUPPORT',bounds
    return 'HISTORICAL_TRANSFER_SUPPORTED_NOT_DEPLOYABLE',bounds


def compare_bounds(actual,expected):
    """Numeric audit only: same keys, finite values, decision signs and <=8 ULP."""
    if actual is None or expected is None:
        need(actual is expected,'STATISTICS_NONE');return
    need(actual.keys()==expected.keys(),'STATISTICS_KEYS')
    for key,value in expected.items():
        observed=actual[key]
        if value is None or observed is None:
            need(value is observed,'STATISTICS_NONE');continue
        need(type(value) in (int,float) and type(observed) in (int,float) and
             math.isfinite(value) and math.isfinite(observed),'STATISTICS_FINITE')
        need((value>0)-(value<0)==(observed>0)-(observed<0),'STATISTICS_SIGN')
        need(abs(observed-value)<=8*max(math.ulp(value),math.ulp(observed)),'STATISTICS_ULP')


def audit_check(runner):
    """One reviewed audit-only source repair; execution freeze stays immutable.

    The unmodified economic runner still rejects the changed audit source, so
    this receipt-audit allowance cannot restart collection or economic runs.
    """
    import datetime as dt
    c=runner.strict_json(runner.CONTRACT.read_bytes());f=runner.strict_json((RUN/'freeze.json').read_bytes())
    review_path=ROOT/'docs/reviews/2026-10-01-frozen-history-transfer-audit-failure.json'
    review=runner.strict_json(review_path.read_bytes());repair=review['auditor_repair']
    gate=runner.strict_json((RUN/'validation-state.json').read_bytes())
    need(gate['status']=='RUNNING' and any(e['event']=='review' and
         e.get('failure_id')=='02ba1dbef33e4f58913ad64114cfccdc' and e.get('decision')=='retry' and
         e.get('review_sha256')==sha(review_path) for e in gate['history']),'AUDIT_REPAIR_REVIEW')
    name='tools/audit_frozen_history_transfer.py'
    need(repair['source']==name and repair['original_sha256']==f['sources'][name] and
         sha(RUN/'auditor-before-numeric-repair.py')==repair['original_sha256'] and
         sha(ROOT/name)==repair['repaired_sha256'],'EXACT_AUDITOR_REPAIR')
    need(sha(ROOT/'tools/test_frozen_history_audit_repair.py')==repair['test_sha256'],'REPAIR_TEST_IDENTITY')
    need(all(sha(ROOT/p)==h for p,h in f['sources'].items() if p!=name),'EXECUTION_SOURCE_CHANGED')
    need(sha(runner.CONTRACT)==f['contract_sha256'] and sha(runner.PLAN)==f['plan_sha256'],'CONTRACT_CHANGED')
    need(sha(runner.prep.MODEL)==c['model_sha256']==f['model_sha256'] and sha(runner.BINARY)==f['binary_sha256'],'MODEL_OR_BINARY_CHANGED')
    elapsed=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(c['started_utc'])).total_seconds()
    need(0<=elapsed<c['maximum_hours']*3600,'STAGE_DEADLINE')
    return c,f


def audit_inputs():
    import plan_frozen_history_intake as prep
    from mvp_reference_inputs import strict_json
    read=lambda name:strict_json((RUN/name).read_bytes())
    manifest,proof=read('manifest.json'),read('input-proof.json')
    need(proof['manifest_sha256']==sha(RUN/'manifest.json') and proof['csv_sha256']==sha(RUN/'replay.csv'),'INPUT_HASH')
    need(proof['complete'] and not proof['synthetic'] and proof['prior_probes_matched']==6,'REAL_INPUT')
    pages=manifest['pages'];requests=prep.canonical_requests();data={k:{} for k in ('trade','mark','funding')}
    need(len(pages)==len(requests)==216,'PAGE_COUNT');total=0
    for i,(r,q) in enumerate(zip(pages,requests)):
        need(r['request_index']==i and r['url']==q['url'] and r['kind']==q['kind'] and
             r['http_status']==200 and r['error'] is None,'REQUEST_IDENTITY')
        need(Path(r['file']).name==r['file'],'RAW_PATH')
        path=RUN/'raw'/r['file'];need(not path.is_symlink() and sha(path)==r['sha256'],'RAW_IDENTITY')
        raw=path.read_bytes();total+=len(raw)
        need(prep.validate_page(raw,q,r['received_at_ms'])==r['rows'],'ROW_COUNT')
        need(read('receipts/'+r['file'])==r,'RECEIPT_BINDING')
        attempt=read('attempts/'+r['file']);need(attempt['request']==q and attempt['request_index']==i,'ATTEMPT_BINDING')
        for row in strict_json(raw)['result']['list']:
            ts=int(row['fundingRateTimestamp'] if q['kind']=='funding' else row[0])
            need(ts not in data[q['kind']],'DUPLICATE_ROW');data[q['kind']][ts]=row
    need(total<=268435456,'BYTE_BUDGET')
    for kind in data:
        step=28800000 if kind=='funding' else 300000
        need(sorted(data[kind])==list(range(1609372800000,1640822400000,step)),'RAW_GRID')
    count=0
    with (RUN/'replay.csv').open() as source:
        for row in csv.DictReader(source):
            ts=1609372800000+count*300000;count+=1
            need(int(row['timestamp'])==ts and row['symbol']=='BTCUSDT' and int(row['interval_ms'])==300000,'CSV_CLOCK')
            need(int(row['execution_enabled'])==int(ts>=1609459200000),'WARMUP')
            for kind,mapping in (('trade',{'open':1,'high':2,'low':3,'price':4,'volume':5}),
                                 ('mark',{'mark_open':1,'mark_high':2,'mark_low':3,'mark_close':4})):
                for key,j in mapping.items():need(D(row[key])==D(data[kind][ts][j]),'CSV_VALUE:'+key)
            rate=data['funding'][ts]['fundingRate'] if ts in data['funding'] else '0'
            need(D(row['funding_rate_per_interval'])==D(rate),'CSV_FUNDING')
    need(count==proof['source_bars']==proof['mark_bars']==104832 and proof['funding_events']==1092,'INPUT_COUNTS')
    attempts=list((RUN/'attempts').glob('*.json'))
    need(len(attempts)==manifest['attempted_gets']<=220,'GET_BUDGET')
    need(all((RUN/'receipts'/p.name).is_file() for p in attempts),'ATTEMPT_RECEIPTS')
    return dict(source_bars=count,evaluation_bars=104544,funding_events=1092,attempted_gets=len(attempts))


def audit():
    import run_frozen_history_transfer as runner
    import unittest
    import test_frozen_history_audit_repair as repair_tests
    c,f=audit_check(runner)
    tests=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(repair_tests))
    need(tests.wasSuccessful() and tests.testsRun==8,'AUDIT_REPAIR_GUARDS')
    preserved=runner.preservation();inputs=audit_inputs()
    read=lambda name:json.loads((RUN/name).read_text())
    result=read('result.json');attempt=read('economic-attempt.json');access=read('data-access-start.json')
    need(f['frozen_utc']<=access['at']<=attempt['at']<=result['completed_utc'],'FREEZE_ORDER')
    need(attempt['freeze_sha256']==access['freeze_sha256']==read('manifest.json')['freeze_sha256']==sha(RUN/'freeze.json'),'FREEZE_IDENTITY')
    need(result['input_proof_sha256']==attempt['input_proof_sha256']==sha(RUN/'input-proof.json'),'INPUT_BINDING')
    need(result['model_sha256']==attempt['model_sha256']==c['model_sha256'],'MODEL_BINDING')
    need(result['learned']['coefficients']==[0,-4] and result['fixed']['coefficients']==[1,0],'FROZEN_COEFFICIENTS')
    need(result['training_vectors']==0 and result['economic_attempts']==result['historical_confirmation_attempts']==1 and
         result['prospective_confirmation_attempts']==0 and result['compute_seconds']<=1800 and
         result['candidate_status']=='NO_QUALIFIED_CANDIDATE' and result['qualification'] is False,'BUDGET_OR_AUTHORITY')
    domain=read_domain(RUN/'replay.csv',c['evaluation_start_ms'],c['end_exclusive_ms']);audited={}
    for name in ('learned','fixed'):
        summary=result[name];need(summary['trace_file']==name+'.jsonl','TRACE_PATH')
        audited[name]=audit_trace(RUN/summary['trace_file'],domain,summary)
        audit_concentration(RUN/summary['trace_file'],summary,read(name+'-concentration.json'))
        need(len(summary['weekly'])==51,'FIXED_FULL_BLOCKS')
    decision,bounds=audit_verdict(result['learned'],result['fixed'],read('learned-concentration.json'))
    need(result['verdict']==decision,'VERDICT');compare_bounds(result['hac_lower'],bounds)
    return dict(schema='frozen_history_transfer_independent_audit_v1',status='PASS_EXISTING_RECEIPTS_ONLY',
        verdict=decision,inputs=inputs,decimal_books=4,preserved_files=preserved,**audited,
        audit_repair_tests=tests.testsRun,independent_hac_lower=bounds,
        audit_repair_review_sha256=sha(ROOT/'docs/reviews/2026-10-01-frozen-history-transfer-audit-failure.json'),
        qualification=False,result_sha256=sha(RUN/'result.json'))


if __name__=='__main__':
    report=audit();save(RUN/'independent-audit.json',report)
    print(json.dumps(report,allow_nan=False))
