#!/usr/bin/env python3
"""Verify the explicit engineering/data boundary; never turn data FAIL into PASS.

Consumes archived GitHub job observations and exact-run downloaded reports.
No network, credentials, orders, archive repair or gate-state modification.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'config/engineering_release_boundary_v1.json'
CONTRACT_SHA256 = '11d19c45c54cfeb3e2d4a87f7704e590169e85d80ffa05f0b6fcfae5b5c809b3'
AUTHORITY = ('promotion_authority', 'demo_activation_authorized', 'live_activation_authorized')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def read(path):
    with path.open('rb') as handle:
        raw = handle.read(16 * 1024 * 1024 + 1)
    require(len(raw) <= 16 * 1024 * 1024, 'REPORT_BYTE_BUDGET')
    result = json.loads(raw)
    require(isinstance(result, dict), 'REPORT_OBJECT_REQUIRED')
    return result


def workflow(snapshot, sha, name, event, conclusion, steps):
    runs = [r for r in snapshot['runs']['workflow_runs'] if r['name'] == name]
    require(len(runs) == 1, 'EXPECTED_ONE_RUN:' + name)
    run = runs[0]
    require(run['head_sha'] == sha and run['head_branch'] == 'main' and run['event'] == event,
            'RUN_IDENTITY:' + name)
    require(run['status'] == 'completed' and run['conclusion'] == conclusion, 'RUN_RESULT:' + name)
    jobs = snapshot['jobs'].get(str(run['id']), {}).get('jobs', [])
    require(jobs and all(j['status'] == 'completed' for j in jobs), 'INCOMPLETE_JOBS:' + name)
    if conclusion == 'success':
        require(all(j['conclusion'] == 'success' for j in jobs), 'JOB_NOT_SUCCESS:' + name)
    else:
        require(any(j['conclusion'] == 'failure' for j in jobs), 'DATA_FAILURE_NOT_OBSERVED:' + name)
        require(all(j['conclusion'] in ('success', 'failure') for j in jobs), 'UNEXPECTED_DATA_JOB:' + name)
    actual = {s['name']: s['conclusion'] for j in jobs for s in j.get('steps', [])}
    for step, expected in steps.items():
        require(actual.get(step) == expected, 'REQUIRED_STEP_RESULT:' + step)
    return {'name': name, 'run_id': run['id'], 'run_attempt': run['run_attempt'], 'conclusion': conclusion}


def no_authority(report):
    require(all(report.get(k) is False for k in AUTHORITY), 'FORBIDDEN_OR_MISSING_AUTHORITY')


def evaluate(snapshot, sha, freshness, runtime, closure, archive, v4):
    require(re.fullmatch('[0-9a-f]{40}', sha), 'EXPECTED_SHA_INVALID')
    require(snapshot['target_sha'] == sha, 'SNAPSHOT_SHA_MISMATCH')
    require(hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == CONTRACT_SHA256, 'ENGINEERING_BOUNDARY_DRIFT')
    contract = read(CONTRACT)
    no_authority(contract)
    engineering = [workflow(snapshot, sha, name, item['event'], 'success',
                            {step:'success' for step in item['steps']})
                   for name, item in contract['engineering_workflows'].items()]
    data_runs = [
        workflow(snapshot, sha, 'Option Archive Lifecycle Audit', 'workflow_run', 'success', {
            'Audit aggregate lifecycle coverage on ECS':'success', 'Download aggregate report':'success',
            'Validate aggregate report':'success'}),
        workflow(snapshot, sha, 'Option Lifecycle V4 Gate', 'workflow_run', 'failure', {
            'Audit checksum-bound lifecycle on ECS':'failure', 'Download aggregate audit report':'success',
            'Summarize aggregate audit diagnostics':'success', 'Validate aggregate audit report':'failure'}),
    ]
    require(freshness['status'] == 'PASS' and freshness['container_status'] == 'running'
            and freshness['container_restart_count'] == 0, 'DEPLOYMENT_NOT_FRESH_AND_HEALTHY')
    require(all(freshness[k] == sha for k in ('expected_sha','release_git_sha','container_image_revision')),
            'DEPLOYMENT_REVISION_MISMATCH')
    require(runtime['verdict'] in ('PASS','PASS_WITH_ACTIONS') and runtime['protection_status'] == 'PASS'
            and runtime['account_sync_status'] == 'OK', 'SMOKE_NOT_PROTECTED_AND_SYNCHRONIZED')
    require(closure['schema_version'] == 'option_frozen_closure_engineering_v1'
            and closure['decision'] == 'FROZEN_CLOSED_CANDIDATE_VERIFIED'
            and closure['verification_sha'] == sha, 'FROZEN_CLOSURE_IDENTITY')
    require(closure['registry_canonical_sha256'] == contract['closure_registry_canonical_sha256']
            and closure['closure_anchor']['artifact_zip_sha256'] == contract['closure_anchor_zip_sha256'],
            'CLOSURE_ANCHOR_DRIFT')
    require(closure['closure_latched'] is True and closure['closure_evidence_verified'] is True
            and closure['current_data_evaluated'] is False and closure['economic_evidence'] is False
            and closure['demo_review_eligible'] is False, 'CLOSURE_SCOPE_OR_LATCH_INVALID')
    require(all(closure.get(k) is False for k in ('profitability_claim_allowed', 'sharpe_claim_allowed',
                                                 'drawdown_claim_allowed')), 'CLOSURE_CLAIM_FORBIDDEN')
    for report in (closure, archive, v4):
        no_authority(report)
    for report in (archive, v4):
        require(report['identities']['executed_release_sha'] == sha, 'DATA_REPORT_RELEASE_MISMATCH')
        require(report['economic_evidence'] is False, 'UNEXPECTED_ECONOMIC_CLAIM')
        require(report['archive_integrity']['root_present'] is True, 'DATA_ROOT_MISSING')
    require(archive['schema_version'] == 'option_archive_lifecycle_audit_v1'
            and archive['decision'] == contract['archive_expected_decision']
            and archive['archive_integrity']['invalid_segment_count'] == 0, 'UNEXPECTED_ARCHIVE_RESULT')
    require(archive['identities']['case_canonical_sha256'] == contract['archive_case_canonical_sha256']
            and archive['identities']['policy_canonical_sha256'] == contract['archive_policy_canonical_sha256'],
            'ARCHIVE_CONTRACT_DRIFT')
    require(v4['schema_version'] == 'option_lifecycle_audit_v4'
            and v4['decision'] == 'INVALID_OPTION_LIFECYCLE_ARCHIVE'
            and v4['reason_code'] == 'ARCHIVE_INTEGRITY_FAILURE', 'UNEXPECTED_V4_RESULT')
    require(v4['identities']['policy_canonical_sha256'] == contract['v4_policy_canonical_sha256']
            and v4['identities']['manifest_canonical_sha256'] == contract['v4_manifest_canonical_sha256'],
            'V4_CONTRACT_DRIFT')
    integrity = v4['archive_integrity']
    require(integrity['invalid_segment_count'] == 1
            and integrity['invalid_segment_details_truncated'] is False
            and integrity['invalid_segment_reason_counts'] == {'POLL_LATENCY_EXCEEDS_CONTRACT':1}
            and integrity['invalid_segments'] == [contract['known_v4_failure']], 'UNEXPLAINED_DATA_FAILURE')
    return {
        'schema_version':'engineering_release_result_v1',
        'boundary':contract['scope'], 'release_sha':sha,
        'decision':'ENGINEERING_PASS_DATA_NOT_QUALIFIED',
        'engineering':{'status':'PASS', 'runs':engineering, 'closure_latched':True},
        'runtime':{k:runtime[k] for k in ('verdict','protection_status','execution_status','account_sync_status')},
        'data_quality':{'status':'FAIL', 'archive_decision':archive['decision'], 'v4_decision':v4['decision'],
                        'known_failure':contract['known_v4_failure'], 'runs':data_runs},
        'historical_failure_reclassified_as_pass':False, 'business_qualified':False,
        **{k:False for k in AUTHORITY},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('snapshot','freshness','runtime','closure','archive','v4','output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--expected-sha', required=True)
    args = parser.parse_args()
    names = ('snapshot','freshness','runtime','closure','archive','v4')
    paths = {name:getattr(args,name) for name in names}
    require(args.output.resolve() not in {p.resolve() for p in paths.values()}, 'OUTPUT_OVERWRITES_INPUT')
    payload = {k:read(v) for k,v in paths.items()}
    result = evaluate(payload['snapshot'], args.expected_sha, payload['freshness'], payload['runtime'],
                      payload['closure'], payload['archive'], payload['v4'])
    result['input_sha256'] = {k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()}
    result['boundary_file_sha256'] = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
    with args.output.open('x') as handle:
        json.dump(result, handle, indent=2, sort_keys=True)
        handle.write('\n')
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
