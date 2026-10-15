import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from audit_eth_sol_input import verify_records


class InputAuditTests(unittest.TestCase):
    def test_summary_and_wire_bound_even_if_record_rewritten(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);digest=hashlib.sha256(b'x').hexdigest()
            def save(p,d):p.write_text(json.dumps(d))
            for i in range(2):
                p=root/'identity-attempts'/str(i);p.mkdir(parents=True)
                (p/'response.bin').write_bytes(b'x');save(p/'record.json',dict(status='VALID',wire_sha256=digest))
            p=root/'market-attempts'/'0000';p.mkdir(parents=True)
            (p/'response.bin').write_bytes(b'x')
            r=dict(status='VALID',index=0,request={'fixed':'request'},page_attempt=1,wire_sha256=digest,bytes=1,wait_seconds=0)
            save(p/'record.json',r);save(p/'started.json',dict(index=0,request=r['request'],page_attempt=1))
            c=dict(records={'0':dict(folder=str(p),wire_sha256=digest)},attempts=3,bytes=3,known_errors=0,wait_seconds=0)
            f=dict(requests=[r['request']])
            self.assertEqual(verify_records(root,f,c)['valid_pages'],1)
            (p/'response.bin').write_bytes(b'y');r['wire_sha256']=hashlib.sha256(b'y').hexdigest();save(p/'record.json',r)
            with self.assertRaisesRegex(ValueError,'COLLECTION_RESPONSE_BINDING'):verify_records(root,f,c)


if __name__=='__main__':unittest.main()
