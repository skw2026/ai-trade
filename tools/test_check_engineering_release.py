#!/usr/bin/env python3
"""Synthetic boundary checks: known data FAIL is explicit; surprises still block."""
import copy
import json
from pathlib import Path
import unittest

import check_engineering_release as check


class EngineeringBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.sha = 'a' * 40
        c = check.read(check.CONTRACT)
        self.contract = c
        self.snapshot = {'target_sha':self.sha, 'runs':{'workflow_runs':[]}, 'jobs':{}}
        specs = [(name, item['event'], 'success', {s:'success' for s in item['steps']})
                 for name,item in c['engineering_workflows'].items()]
        specs += [('Option Archive Lifecycle Audit','workflow_run','success', {
            'Audit aggregate lifecycle coverage on ECS':'success', 'Download aggregate report':'success',
            'Validate aggregate report':'success'}),
            ('Option Lifecycle V4 Gate','workflow_run','failure', {
                'Audit checksum-bound lifecycle on ECS':'failure','Download aggregate audit report':'success',
                'Summarize aggregate audit diagnostics':'success','Validate aggregate audit report':'failure'})]
        for index,(name,event,conclusion,steps) in enumerate(specs,1):
            self.snapshot['runs']['workflow_runs'].append({'id':index,'name':name,'event':event,
                'head_sha':self.sha,'head_branch':'main','status':'completed','conclusion':conclusion,'run_attempt':1})
            self.snapshot['jobs'][str(index)] = {'jobs':[{'status':'completed','conclusion':conclusion,
                'steps':[{'name':s,'conclusion':v} for s,v in steps.items()]}]}
        self.freshness = {k:self.sha for k in ('expected_sha','release_git_sha','container_image_revision')}
        self.freshness.update(status='PASS',container_status='running',container_restart_count=0)
        self.runtime = dict(verdict='PASS_WITH_ACTIONS', protection_status='PASS', execution_status='NOT_EVALUATED',account_sync_status='OK')
        authority = {k:False for k in check.AUTHORITY}
        self.closure = {**authority,'schema_version':'option_frozen_closure_engineering_v1',
            'decision':'FROZEN_CLOSED_CANDIDATE_VERIFIED','verification_sha':self.sha,
            'registry_canonical_sha256':c['closure_registry_canonical_sha256'],
            'closure_anchor':{'artifact_zip_sha256':c['closure_anchor_zip_sha256']},
            'closure_latched':True,'closure_evidence_verified':True,'current_data_evaluated':False,
            'economic_evidence':False,'demo_review_eligible':False,
            'profitability_claim_allowed':False,'sharpe_claim_allowed':False,'drawdown_claim_allowed':False}
        self.archive = {**authority,'schema_version':'option_archive_lifecycle_audit_v1',
            'decision':c['archive_expected_decision'],'economic_evidence':False,
            'archive_integrity':{'root_present':True,'invalid_segment_count':0},
            'identities':{'executed_release_sha':self.sha,'case_canonical_sha256':c['archive_case_canonical_sha256'],
                          'policy_canonical_sha256':c['archive_policy_canonical_sha256']}}
        self.v4 = {**authority,'schema_version':'option_lifecycle_audit_v4',
            'decision':'INVALID_OPTION_LIFECYCLE_ARCHIVE','reason_code':'ARCHIVE_INTEGRITY_FAILURE','economic_evidence':False,
            'identities':{'executed_release_sha':self.sha,'policy_canonical_sha256':c['v4_policy_canonical_sha256'],
                          'manifest_canonical_sha256':c['v4_manifest_canonical_sha256']},
            'archive_integrity':{'root_present':True,'invalid_segment_count':1,
                                 'invalid_segment_details_truncated':False,
                                 'invalid_segment_reason_counts':{'POLL_LATENCY_EXCEEDS_CONTRACT':1},
                                 'invalid_segments':[copy.deepcopy(c['known_v4_failure'])]}}

    def evaluate(self):
        return check.evaluate(self.snapshot,self.sha,self.freshness,self.runtime,self.closure,self.archive,self.v4)

    def test_engineering_pass_keeps_data_fail_and_no_authority(self):
        result=self.evaluate()
        self.assertEqual(result['engineering']['status'],'PASS')
        self.assertEqual(result['data_quality']['status'],'FAIL')
        self.assertEqual(result['data_quality']['v4_decision'],'INVALID_OPTION_LIFECYCLE_ARCHIVE')
        self.assertFalse(result['business_qualified'])
        self.assertFalse(result['historical_failure_reclassified_as_pass'])
        for key in check.AUTHORITY:
            self.assertFalse(result[key])

    def test_stale_or_missing_run_is_not_fixed_engineering_evidence(self):
        self.snapshot['runs']['workflow_runs'][0]['head_sha']='b'*40
        with self.assertRaisesRegex(ValueError,'RUN_IDENTITY'):
            self.evaluate()
        self.snapshot['runs']['workflow_runs'].pop(0)
        with self.assertRaisesRegex(ValueError,'EXPECTED_ONE_RUN'):
            self.evaluate()

    def test_skipped_or_failed_engineering_step_still_blocks(self):
        step=self.snapshot['jobs']['1']['jobs'][0]['steps'][0]
        for state in ('skipped','failure',None):
            step['conclusion']=state
            with self.assertRaisesRegex(ValueError,'REQUIRED_STEP_RESULT'):
                self.evaluate()

    def test_missing_or_unknown_data_diagnostics_still_block(self):
        integrity=self.v4['archive_integrity']
        integrity['invalid_segments']=[]
        with self.assertRaisesRegex(ValueError,'UNEXPLAINED_DATA_FAILURE'):
            self.evaluate()
        integrity['invalid_segments']=[copy.deepcopy(self.contract['known_v4_failure'])]
        integrity['invalid_segments'][0]['report_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'UNEXPLAINED_DATA_FAILURE'):
            self.evaluate()

    def test_new_invalid_segment_is_not_silently_accepted(self):
        self.v4['archive_integrity']['invalid_segment_count']=2
        with self.assertRaisesRegex(ValueError,'UNEXPLAINED_DATA_FAILURE'):
            self.evaluate()

    def test_failed_data_transport_is_not_business_failure(self):
        steps=self.snapshot['jobs']['6']['jobs'][0]['steps']
        next(s for s in steps if s['name']=='Download aggregate audit report')['conclusion']='failure'
        with self.assertRaisesRegex(ValueError,'REQUIRED_STEP_RESULT'):
            self.evaluate()

    def test_deployment_or_report_revision_mismatch_blocks(self):
        self.freshness['release_git_sha']='b'*40
        with self.assertRaisesRegex(ValueError,'DEPLOYMENT_REVISION_MISMATCH'):
            self.evaluate()
        self.freshness['release_git_sha']=self.sha
        self.v4['identities']['executed_release_sha']='b'*40
        with self.assertRaisesRegex(ValueError,'DATA_REPORT_RELEASE_MISMATCH'):
            self.evaluate()

    def test_anchor_cannot_claim_new_data_or_activation(self):
        self.closure['current_data_evaluated']=True
        with self.assertRaisesRegex(ValueError,'CLOSURE_SCOPE'):
            self.evaluate()
        self.closure['current_data_evaluated']=False
        self.closure['live_activation_authorized']=True
        with self.assertRaisesRegex(ValueError,'AUTHORITY'):
            self.evaluate()

    def test_bad_data_cannot_be_reclassified_as_pass(self):
        self.v4['decision']='PASS_FIRST_COMPLETE_LIFECYCLE_FOR_PAYOFF_RECONSTRUCTION_ONLY'
        with self.assertRaisesRegex(ValueError,'UNEXPECTED_V4_RESULT'):
            self.evaluate()


if __name__ == '__main__':
    unittest.main()
