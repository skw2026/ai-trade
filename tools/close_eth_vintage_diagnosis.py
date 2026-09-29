#!/usr/bin/env python3
"""Verify bounded Git-history absence evidence without inferring deleted history."""
import json
from pathlib import Path

import diagnose_eth_supply_vintage as v


def verify_chain(commits,branches,tags,deadlines):
    v.need(isinstance(commits,list) and 0<len(commits)<100,'COMPLETE_SINGLE_PAGE_REQUIRED')
    hashes=[x['sha'] for x in commits]
    v.need(len(hashes)==len(set(hashes)),'DUPLICATE_COMMIT')
    by_sha={x['sha']:x for x in commits}
    for item in commits:
        parents=[p['sha'] for p in item['parents']]
        v.need(len(parents)==len(set(parents)) and all(p in by_sha for p in parents),
               'UNVISITED_PARENT_IN_GRAPH')
    active,visited=set(),set()
    def visit(key):
        v.need(key not in active,'CYCLE_IN_COMMIT_GRAPH')
        if key in visited:
            return
        active.add(key)
        for parent in by_sha[key]['parents']:
            visit(parent['sha'])
        active.remove(key)
        visited.add(key)
    visit(hashes[0])
    v.need(visited==set(hashes),'UNREACHABLE_COMMIT_IN_RESPONSE')
    roots=[x['sha'] for x in commits if not x['parents']]
    v.need(len(roots)==1,'UNPROVEN_SINGLE_ROOT')
    v.need(len(branches)==1 and branches[0]['name']=='master' and
           branches[0]['commit']['sha']==hashes[0],'UNREVIEWED_BRANCH')
    v.need(tags==[],'UNREVIEWED_TAGS')
    reported=[x['commit']['committer']['date'] for x in commits]
    v.need(all(v.ms(date)>v.ms(deadline) for date in reported for deadline in deadlines),
           'POTENTIAL_HISTORICAL_COMMIT_PRESENT')
    return dict(commit_count=len(commits),head_sha=hashes[0],root_sha=roots[0],
                root_reported_committer_time=by_sha[roots[0]]['commit']['committer']['date'],
                oldest_reported_committer_time=min(reported,key=v.ms),
                branches=['master'],tags=[],parent_chain_complete=True,
                merge_commit_count=sum(len(x['parents'])>1 for x in commits),
                cause_of_missing_earlier_history='UNKNOWN',
                historical_publication_proven=False)


def main():
    v.check()
    # Kept within the original acceptance argv for the reviewed single retry.
    import unittest
    suite=unittest.defaultTestLoader.loadTestsFromName('test_close_eth_vintage_diagnosis')
    tests=unittest.TextTestRunner().run(suite)
    v.need(tests.wasSuccessful(),'DAG_SYNTHETIC_REGRESSIONS')
    receipts=[]
    for path in sorted((v.RUN/'receipts').glob('*.json')):
        record=v.strict(path.read_bytes())
        v.need(record['status']==200 and record['error'] is None,'EXPECTED_SUCCESS_RECEIPT')
        v.need(v.sha((v.RUN/record['raw']).read_bytes())==record['sha256'],'RAW_CHANGED')
        receipts.append(record)
    for name in ('repository-history','branches','tags'):
        r=v.strict((v.RUN/'receipts'/f'{name}.json').read_bytes())
        links=[value for key,value in r['headers'].items() if key.lower()=='link']
        v.need(not any('rel="next"' in value for value in links),'UNREAD_NEXT_PAGE')
    read=lambda key:v.strict(v.read(key)[0])
    result=verify_chain(read('repository-history'),read('branches'),read('tags'),v.DEADLINES)
    for i,deadline in enumerate(v.DEADLINES):
        v.need(read(f'history-{i}')==[],'HISTORICAL_VERSION_MAY_EXIST')
        entry=v.strict((v.RUN/f'history-{i}.json').read_bytes())
        v.need(entry==dict(deadline=deadline,choice=None,http_status=200),'WRONG_DATE_RESULT')
    v.need(read('old-repository-history')==[],'OLD_REPOSITORY_HISTORY_PRESENT')
    current=v.commit_choice(v.read('current-file-history')[0])
    v.need(current['sha']==result['head_sha'],'CURRENT_CONTROL_IDENTITY')
    v.need(len(receipts)==len(list((v.RUN/'attempts').glob('*.json')))==8,'DIAGNOSIS_REQUEST_COUNT')
    v.need(not list((v.RUN/'raw').glob('snapshot-*')),'UNEXPECTED_SNAPSHOT_DOWNLOAD')
    result.update(status='NO_TARGET_VINTAGES_IN_REACHABLE_PUBLIC_HISTORY',
                  scope='READ_ONLY_DIAGNOSIS_NOT_REOPENING',deadlines=list(v.DEADLINES),
                  recorded_http_gets=8,visible_document_reads=3,visible_read_units=11,
                  csv_snapshots_downloaded=0,economic_attempts=0,new_price_analysis=0,
                  original_capacity_upper_bound=142,original_minimum_weeks=144,
                  original_status='CAPACITY_INSUFFICIENT',candidate_qualified=False,
                  gate_states_preserved=16,preserved_files=len(v.preserved()['preserved_sha256']),
                  signature=current['verification'],latest_reported_committer_time=current['committer_date'],
                  upstream_public_web_observation='GitLab project URL redirected to sign-in; no authenticated access attempted',
                  root_cause_boundary='Reachable public history starts after target deadlines; reset/import/deletion cause not proven',
                  receipts=[{k:r[k] for k in ('url','status','sha256','bytes','retrieved_utc')} for r in receipts],
                  source_sha256={str(p.relative_to(v.ROOT)):v.sha(p.read_bytes()) for p in
                    (Path(__file__).resolve(),v.ROOT/'tools/test_close_eth_vintage_diagnosis.py')},
                  frozen_source_sha256=v.strict((v.RUN/'freeze.json').read_bytes())['source_sha256'],
                  publication_gate='BLOCKED_UNCHANGED',actual_pushes_this_stage=0,
                  original_failure_id='4d183e021c6743f28c9df8076b52c577',
                  corrected_dag_tests_passed=tests.testsRun)
    v.save_new(v.RUN/'result.json',result)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
