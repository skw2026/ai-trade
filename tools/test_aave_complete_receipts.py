import copy
import unittest
from unittest.mock import patch
import aave_complete_receipts as r
import test_aave_complete_input as fixtures_module


class AnnualNativeTests(unittest.TestCase):
    def setUp(self):
        fixtures=fixtures_module.NativeControlTests();fixtures.setUp()
        self.row=fixtures.row
        self.row['address']=r.c.POOL
        self.row['topics']=[r.c.TOPIC,'0x'+'0'*24+r.c.WETH[2:],'0x'+'0'*64,'0x'+'0'*64]
        self.receipt=fixtures.receipt
        self.receipt['logs'][0].update(address=self.row['address'],topics=self.row['topics'])

    def test_full_tx(self):
        self.assertEqual(r.validate_transaction(self.receipt,[self.row]),1)

    def test_missing_catalog_target(self):
        extra=copy.deepcopy(self.receipt['logs'][0]);extra['logIndex']='0x4'
        self.receipt['logs'].append(extra)
        with self.assertRaisesRegex(ValueError,'NATIVE_TARGETS_DIFFER_FROM_CATALOG'):
            r.validate_transaction(self.receipt,[self.row])

    def test_multi_liquidation_tx(self):
        other=copy.deepcopy(self.row);other['logIndex']='0x4'
        extra=copy.deepcopy(self.receipt['logs'][0]);extra['logIndex']='0x4'
        self.receipt['logs'].append(extra)
        self.assertEqual(r.validate_transaction(self.receipt,[self.row,other]),2)

    def test_group_unique_tx(self):
        other=copy.deepcopy(self.row);other['logIndex']='0x4'
        self.assertEqual(len(r.grouped([other,self.row])),1)
        self.assertEqual(r.grouped([other,self.row])[self.row['transactionHash']][0],self.row)

    def test_mixed_tx_reject(self):
        other=copy.deepcopy(self.row);other['transactionHash']='0x'+'9'*64
        with self.assertRaisesRegex(ValueError,'MIXED_TRANSACTION_GROUP'):
            r.validate_transaction(self.receipt,[self.row,other])

    def test_envelope_rejections(self):
        for value in [None,{'jsonrpc':'2.0','id':True,'result':{}},
                      {'jsonrpc':'2.0','id':1,'error':{},'result':{}},
                      {'jsonrpc':'1.0','id':1,'result':{}}]:
            with self.subTest(value=value),self.assertRaises(ValueError):r.envelope(value)

    def test_empty_rows_reject(self):
        with self.assertRaisesRegex(ValueError,'EMPTY_TRANSACTION_GROUP'):
            r.validate_transaction(self.receipt,[])

    def test_no_write_method(self):
        with self.assertRaisesRegex(ValueError,'READ_ONLY_ALLOWLIST'):
            r.fetch('never','eth_sendRawTransaction',[],lambda _:None)


if __name__=='__main__':unittest.main()
