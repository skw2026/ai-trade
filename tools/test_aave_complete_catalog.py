import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import aave_complete_catalog as c


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.month={'index':0,'low':1,'high':10,'start_utc':'2024-01-01T00:00:00Z',
                    'end_utc_exclusive':'2024-02-01T00:00:00Z'}
        self.row={'address':c.POOL,'topics':[c.TOPIC,'0x'+'0'*24+c.WETH[2:],'0x'+'0'*64,'0x'+'0'*64],
                  'data':'0x'+'0'*256,'transactionHash':'0x'+'1'*64,'blockNumber':'0x1',
                  'transactionIndex':'0x0','logIndex':'0x1','timeStamp':hex(1704067200)}

    def value(self,rows=None):
        return {'status':'1','message':'OK','result':[self.row] if rows is None else rows}

    def test_valid(self):
        self.assertEqual(c.validate_indexed(self.value(),self.month,1,10),[self.row])

    def test_empty(self):
        self.assertEqual(c.validate_indexed({'status':'0','message':'No logs found','result':[]},self.month,1,10),[])

    def test_bad_status(self):
        for value in ({'status':'0','result':[]},{'status':'0','message':'No logs found','result':[self.row]},
                      {'status':'1','result':'No logs found'}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                c.validate_indexed(value,self.month,1,10)

    def test_bad_fields(self):
        for key,value in [('address','0x'+'9'*40),('topics',[]),('data','0x00'),
                          ('blockNumber','0xb'),('timeStamp',hex(1706745600)),('transactionHash','bad')]:
            row=copy.deepcopy(self.row);row[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                c.validate_indexed(self.value([row]),self.month,1,10)

    def test_duplicates(self):
        with self.assertRaises(ValueError):c.validate_indexed(self.value([self.row,self.row]),self.month,1,10)

    def test_parent_match(self):
        c.check_parent([self.row],[self.row])

    def test_parent_missing_or_mutated(self):
        changed=copy.deepcopy(self.row);changed['data']='0x'+'1'*256
        for children in ([],[changed],[self.row,self.row]):
            with self.subTest(children=children),self.assertRaises(ValueError):c.check_parent([self.row],children)

    def test_quota(self):
        result=c.quota('Date: Mon, 12 Oct 2026 10:01:57 GMT\r\nx-ratelimit-limit: 10\r\nx-ratelimit-remaining: 2\r\nx-ratelimit-reset: 3483103\r\n')
        self.assertEqual(result['remaining'],2)
        self.assertEqual(dt.datetime.fromisoformat(result['not_before_utc']),dt.datetime(2026,10,12,11,0,6,103000,tzinfo=dt.timezone.utc))

    def test_missing_quota(self):
        with self.assertRaises(ValueError):c.quota('Date: Mon, 12 Oct 2026 10:00:00 GMT')

    def test_split_left_first(self):
        with patch.object(c,'CAP',1),patch.object(c,'cached',side_effect=[[self.row],None]):
            rows,missing=c.walk(self.month,1,10)
        self.assertEqual(missing,{'month':0,'low':1,'high':5,'depth':1})

    def test_single_block_cap(self):
        with patch.object(c,'CAP',1),patch.object(c,'cached',return_value=[self.row]),self.assertRaises(ValueError):
            c.walk(self.month,1,1)

    def test_depth_cap(self):
        with patch.object(c,'CAP',1),patch.object(c,'cached',return_value=[self.row]),self.assertRaises(ValueError):
            c.walk(self.month,1,10,24)

    def test_cached_request_binding(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(c.a,'ROOT',Path(directory)):
            month=dict(self.month,index=8)
            out=Path(directory)/'operations'/c.node_name(month,1,10)
            out.mkdir(parents=True)
            c.a.save(out/'response.json',self.value())
            for request,correct_hash in [({'method':'GET','url':c.request_url(1,10)},True),
                                         ({'method':'GET','url':c.request_url(1,9)},True),
                                         ({'method':'GET','url':c.request_url(1,10)},False)]:
                with self.subTest(request=request,correct_hash=correct_hash):
                    (out/'request.json').write_text(json.dumps(request))
                    record={'status':'CATALOG_VALID','month':8,'low':1,'high':10,
                            'response_sha256':c.a.digest((out/'response.json').read_bytes()),
                            'request_sha256':c.a.digest((out/'request.json').read_bytes()) if correct_hash else 'wrong'}
                    (out/'record.json').write_text(json.dumps(record))
                    if correct_hash and request['url']==c.request_url(1,10):
                        self.assertEqual(c.cached(month,1,10),[self.row])
                    else:
                        with self.assertRaisesRegex(ValueError,'CATALOG_REQUEST_CHANGED'):c.cached(month,1,10)

    def test_final_output_cap_and_exclusive(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(c.a,'ROOT',Path(directory)):
            root=Path(directory)
            config={'imported_input_bytes':0,'max_input_bytes':1}
            (root/'control.json').write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError,'FINAL_CATALOG_BYTE_BUDGET'):c.save_catalog({'events':[]})
            self.assertFalse((root/'annual-catalog.json').exists())
            config['max_input_bytes']=10000
            (root/'control.json').write_text(json.dumps(config))
            c.save_catalog({'events':[]})
            with self.assertRaises(FileExistsError):c.save_catalog({'events':[]})


if __name__=='__main__':unittest.main()
