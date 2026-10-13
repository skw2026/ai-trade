import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import aave_complete_receipts as r


class RateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ops = self.root/'operations'
        self.ops.mkdir()
        self.review = self.root/'review.json'
        self.name = 'native-receipt-'+r.RATE_TX[2:]
        self.old = self.ops/self.name
        self.old.mkdir()
        (self.old/'request.json').write_bytes(b'original-request')
        (self.old/'response.json').write_bytes(b'original-rate-error')
        observation = self.ops/'rate-recovery-observation-1'
        observation.mkdir()
        (observation/'response.json').write_bytes(b'diagnostic-response')
        self.config = {'failure_id':r.RATE_FAILURE, 'decision':'retry',
                       'corrected_source_sha256':r.a.digest(Path(r.__file__).read_bytes()),
                       'original_request_sha256':r.a.digest(b'original-request'),
                       'original_response_sha256':r.a.digest(b'original-rate-error'),
                       'observation_response_sha256':r.a.digest(b'diagnostic-response')}
        self.review.write_text(json.dumps(self.config))
        self.review_hash = r.a.digest(self.review.read_bytes())
        self.gate('RUNNING')
        for target, value in [('ROOT',self.root)]:
            p = patch.object(r.a,target,value);p.start();self.addCleanup(p.stop)
        p = patch.object(r,'RATE_REVIEW',self.review);p.start();self.addCleanup(p.stop)
        p = patch.object(r,'_next_request_not_before',0.0);p.start();self.addCleanup(p.stop)

    def gate(self, status):
        (self.root/'validation-state.json').write_text(json.dumps({'status':status,
            'active':{'failure_id':r.RATE_FAILURE,'review_sha256':self.review_hash}}))

    def test_first_corrective_mapping_requires_same_running_gate(self):
        self.assertEqual(r.rate_correction_name(),self.name+'-rate-corrected')
        self.gate('BLOCKED')
        with self.assertRaisesRegex(ValueError,'RATE_SAME_GATE_RETRY_REQUIRED'):
            r.rate_correction_name()

    def test_original_failure_cannot_change(self):
        (self.old/'response.json').write_bytes(b'replaced')
        with self.assertRaisesRegex(ValueError,'RATE_ORIGINAL_EVIDENCE_CHANGED'):
            r.rate_correction_name()

    def test_observation_cannot_change(self):
        (self.ops/'rate-recovery-observation-1'/'response.json').write_bytes(b'replaced')
        with self.assertRaisesRegex(ValueError,'RATE_RECOVERY_OBSERVATION_CHANGED'):
            r.rate_correction_name()

    def test_changed_source_not_admitted(self):
        self.config['corrected_source_sha256']='0'*64
        self.review.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError,'RATE_CORRECTION_REVIEW_BINDING'):
            r.rate_correction_name()

    def test_corrected_cache_still_requires_review_identity(self):
        out=self.ops/(self.name+'-rate-corrected');out.mkdir()
        record={'retry_of':r.RATE_FAILURE,'correction_review_sha256':self.review_hash}
        (out/'record.json').write_text(json.dumps(record))
        self.gate('READY')
        self.assertEqual(r.rate_correction_name(),out.name)
        record['retry_of']='another-failure'
        (out/'record.json').write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError,'RATE_CORRECTED_CACHE_BINDING'):
            r.rate_correction_name()

    def test_cross_process_uses_last_send_and_same_endpoint(self):
        now=r.a.dt.datetime(2026,10,12,12,0,5,tzinfo=r.a.dt.timezone.utc)
        (self.old/'record.json').write_text(json.dumps({'endpoint':r.a.ENDPOINT,
            'request_sent_utc':'2026-10-12T12:00:04+00:00',
            'ended_utc':'2026-10-12T12:00:05+00:00'}))
        with patch.object(r.a,'now',return_value=now),patch.object(r.time,'monotonic',return_value=100),\
             patch.object(r.time,'sleep') as sleep:
            r.wait_request_slot()
            sleep.assert_called_once_with(2.0)
            self.assertEqual(r._next_request_not_before,103)

    def test_old_record_uses_end_time_conservatively(self):
        now=r.a.dt.datetime(2026,10,12,12,0,5,tzinfo=r.a.dt.timezone.utc)
        (self.old/'record.json').write_text(json.dumps({'endpoint':r.a.ENDPOINT,
            'ended_utc':'2026-10-12T12:00:04.5+00:00'}))
        with patch.object(r.a,'now',return_value=now),patch.object(r.time,'monotonic',return_value=100),\
             patch.object(r.time,'sleep') as sleep:
            r.wait_request_slot()
            sleep.assert_called_once_with(2.5)

    def test_clock_backwards_rejected_before_sleep(self):
        now=r.a.dt.datetime(2026,10,12,12,0,5,tzinfo=r.a.dt.timezone.utc)
        (self.old/'record.json').write_text(json.dumps({'endpoint':r.a.ENDPOINT,
            'ended_utc':'2026-10-12T13:00:00+00:00'}))
        with patch.object(r.a,'now',return_value=now),patch.object(r.time,'sleep') as sleep:
            with self.assertRaisesRegex(ValueError,'RPC_CLOCK_MOVED_BACKWARDS'):
                r.wait_request_slot()
            sleep.assert_not_called()


if __name__=='__main__':unittest.main()
