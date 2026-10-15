import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import close_eth_sol_complete as delivery


class DeliveryTests(unittest.TestCase):
    def test_utc_minute_and_absent_stop(self):
        self.assertIsNone(delivery.utc(0,None))
        self.assertEqual(delivery.utc(1704067200000,292107),'2024-07-21T20:27:00+00:00')

    def test_remote_rejects_short_sha_before_external_io(self):
        with patch.object(delivery.batch,'guard'),patch.object(delivery.batch,'verify_model'),patch.object(delivery.batch,'preserve'), \
             patch.object(delivery.subprocess,'check_output') as external:
            with self.assertRaisesRegex(ValueError,'FULL_SHA_REQUIRED'):delivery.remote('1234')
            external.assert_not_called()

    def test_rewritten_public_result_rejected(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(delivery,'ROOT',Path(folder)), \
             patch.object(delivery,'PUBLIC',Path(folder)/'public.json'),patch.object(delivery,'summary',return_value={'status':'CLOSED_NO_GO'}):
            delivery.batch.save(Path(folder)/'closed.json',dict(public={'status':'CLOSED_NO_GO'},summary_source_sha256=delivery.batch.sha(delivery.__file__)))
            delivery.batch.save(Path(folder)/'public.json',{'status':'PASS'})
            with self.assertRaisesRegex(ValueError,'PUBLIC_RESULT_BINDING'):delivery.local()


if __name__=='__main__':unittest.main()
