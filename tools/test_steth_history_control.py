import copy
import unittest

import steth_history_control as s


def fixture():
    return [dict(jsonrpc='2.0', id=i, result=v) for i, v in enumerate([
        '0x1', dict(number=s.BLOCK, hash='0x'+'a'*64, timestamp=hex(1710000000)),
        '0x'+format(100, '064x'), '0x'+format(90, '064x')], 1)]


class ControlTests(unittest.TestCase):
    def test_unordered_batch_by_id(self):
        r = s.validate(fixture()[::-1])
        self.assertEqual(r['last_request_id'], 100)
        self.assertFalse(r['whole_history_admitted'])

    def test_no_account_or_write_methods(self):
        self.assertEqual([v['method'] for v in s.REQUESTS], ['eth_chainId', 'eth_getBlockByNumber', 'eth_call', 'eth_call'])
        for req in s.REQUESTS[2:]:
            self.assertEqual(req['params'][0]['to'], s.QUEUE)
            self.assertEqual(req['params'][1], s.BLOCK)

    def test_wrong_chain_rejected(self):
        v = fixture(); v[0]['result'] = '0x2'
        with self.assertRaisesRegex(ValueError, 'MAINNET'): s.validate(v)

    def test_missing_duplicate_and_boolean_ids_rejected(self):
        for v in (fixture()[:-1], fixture()+[fixture()[0]], [fixture()[0]]*4):
            with self.assertRaises(ValueError): s.validate(v)
        v = fixture(); v[0]['id'] = True
        with self.assertRaisesRegex(ValueError, 'ENVELOPE'): s.validate(v)

    def test_rpc_errors_are_not_empty_success(self):
        v = fixture(); v[2]['error'] = dict(code=-32000)
        with self.assertRaisesRegex(ValueError, 'ENVELOPE'): s.validate(v)

    def test_exact_block_identity(self):
        for key, value in (('number', hex(20000001)), ('hash', '0x'), ('timestamp', hex(1800000000))):
            v = fixture(); v[1]['result'][key] = value
            with self.assertRaises(ValueError): s.validate(v)

    def test_empty_call_and_inconsistent_queue_rejected(self):
        for value in ('0x', '0x'+format(101, '064x')):
            v = fixture(); v[3]['result'] = value
            with self.assertRaises(ValueError): s.validate(v)


if __name__ == '__main__':
    unittest.main()
