import copy
import unittest
import aave_complete_input as a


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        self.row = {'transactionHash': '0x'+'1'*64, 'blockNumber': '0x42',
                    'transactionIndex': '0x0', 'logIndex': '0x3',
                    'address': '0x'+'2'*40, 'data': '0x'+'0'*256,
                    'topics': ['0x'+'3'*64], 'timeStamp': '0x1234'}
        log = dict(self.row, blockHash='0x'+'4'*64, removed=False)
        self.receipt = dict(transactionHash=self.row['transactionHash'], blockNumber='0x42',
                            transactionIndex='0x0', blockHash=log['blockHash'], status='0x1', logs=[log])
        self.block = dict(number='0x42', hash=log['blockHash'], timestamp='0x1234',
                          transactions=[self.row['transactionHash']])

    def test_receipt_matches(self):
        self.assertEqual(a.validate_receipt(self.receipt, self.row), 1)

    def test_block_matches(self):
        a.validate_block(self.block, self.row, self.receipt)

    def test_receipt_rejections(self):
        changes = [('status','0x0'), ('transactionHash','0x'+'9'*64),
                   ('blockNumber','0x43'), ('transactionIndex','0x1'),
                   ('blockHash','bad'), ('logs',[])]
        for key,value in changes:
            with self.subTest(key=key):
                receipt=copy.deepcopy(self.receipt); receipt[key]=value
                with self.assertRaises(ValueError): a.validate_receipt(receipt,self.row)

    def test_native_log_rejections(self):
        for key,value in [('removed',True),('removed',None),('logIndex','bad'),
                          ('address','0x'+'8'*40),('data','0x01'),('topics',[]),
                          ('transactionHash','0x'+'9'*64),('blockHash','0x'+'8'*64)]:
            with self.subTest(key=key,value=value):
                receipt=copy.deepcopy(self.receipt); receipt['logs'][0][key]=value
                with self.assertRaises(ValueError): a.validate_receipt(receipt,self.row)

    def test_duplicate_and_missing_event(self):
        receipt=copy.deepcopy(self.receipt); receipt['logs'] *= 2
        with self.assertRaises(ValueError): a.validate_receipt(receipt,self.row)
        receipt=copy.deepcopy(self.receipt); receipt['logs'][0]['logIndex']='0x4'
        with self.assertRaises(ValueError): a.validate_receipt(receipt,self.row)

    def test_null_receipt(self):
        with self.assertRaises(ValueError): a.validate_receipt(None,self.row)

    def test_block_rejections(self):
        for key,value in [('number','0x43'),('hash','0x'+'8'*64),('timestamp','0x1235'),
                          ('transactions',[]),('transactions',['0x'+'9'*64,self.row['transactionHash']])]:
            with self.subTest(key=key):
                block=copy.deepcopy(self.block); block[key]=value
                with self.assertRaises(ValueError): a.validate_block(block,self.row,self.receipt)

    def test_empty_native_data_is_allowed_for_other_log(self):
        receipt=copy.deepcopy(self.receipt)
        log=copy.deepcopy(receipt['logs'][0]); log.update(logIndex='0x4', data='0x', topics=[])
        receipt['logs'].append(log)
        self.assertEqual(a.validate_receipt(receipt,self.row),2)


if __name__ == '__main__':
    unittest.main()
