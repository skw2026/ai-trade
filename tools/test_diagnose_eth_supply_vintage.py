#!/usr/bin/env python3
import hashlib
import json
import unittest
import diagnose_eth_supply_vintage as v


class VintageDiagnosisTest(unittest.TestCase):
    def test_empty_history_is_not_api_failure(self):
        self.assertIsNone(v.commit_choice('[]',v.DEADLINES[0]))

    def test_commit_time_is_not_publication_proof(self):
        row=dict(sha='a'*40,commit=dict(author=dict(date='2023-09-18T01:00:00Z'),
            committer=dict(date='2023-09-18T01:00:00Z'),verification=dict(verified=True)))
        r=v.commit_choice(json.dumps([row]),v.DEADLINES[0])
        self.assertFalse(r['historical_publication_proven'])
        row['commit']['committer']['date']='2023-09-19T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'AFTER_DEADLINE'):
            v.commit_choice(json.dumps([row]),v.DEADLINES[0])

    def test_commit_identity_rejected(self):
        with self.assertRaises(ValueError):
            v.commit_choice('[{"sha":"main"}]')

    def test_official_url_only(self):
        self.assertTrue(v.allowed(v.API+'commits'))
        for bad in ('http://api.github.com/repos/coinmetrics/data/commits',
                    'https://api.github.com.evil.invalid/repos/coinmetrics/data/commits',
                    'https://api.github.com/repos/other/repo/commits',
                    v.API+'commits#fragment'):
            self.assertFalse(v.allowed(bad))

    def blob(self,raw):
        return hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()

    def test_supply_only_extraction_not_price_sample(self):
        raw=b'time,SplyCur,PriceUSD\n2023-09-16,1000,999\n'
        result=v.extract(raw,self.blob(raw),v.DEADLINES[0])
        self.assertEqual(result['status'],'TARGET_DAYS_INCOMPLETE')
        self.assertNotIn('PriceUSD',result['selected']['2023-09-16'])
        self.assertFalse(result['historical_publication_proven'])

    def test_blob_tampering_rejected(self):
        with self.assertRaisesRegex(ValueError,'BLOB_MISMATCH'):
            v.extract(b'time,SplyCur\n', '0'*40,v.DEADLINES[0])

    def test_missing_columns(self):
        raw=b'time,PriceUSD\n2023-09-16,100\n'
        self.assertEqual(v.extract(raw,self.blob(raw),v.DEADLINES[0])['status'],'REQUIRED_COLUMNS_ABSENT')

    def test_duplicate_target_days_rejected(self):
        raw=b'time,SplyCur\n2023-09-16,100\n2023-09-16,101\n'
        with self.assertRaisesRegex(ValueError,'DUPLICATE_TARGET'):
            v.extract(raw,self.blob(raw),v.DEADLINES[0])


if __name__=='__main__':
    unittest.main()
