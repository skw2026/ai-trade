import copy
import unittest

import aave_weth_flow as f


def address_topic(value):return '0x'+'0'*24+value[2:]
def data(*values):return '0x'+''.join(format(v%(2**256),'064x') for v in values)


class WethFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity={'factory':f.V3_FACTORY,'token0':'0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
                      'token1':f.WETH,'fee':3000}
        cls.pool=f.pool_address(cls.identity['token0'],cls.identity['token1'],3000)

    def setUp(self):
        self.sender='0x'+'1'*40
        self.swap={'address':self.pool,'logIndex':'0x1','topics':[f.V3_SWAP,address_topic(self.sender),address_topic(self.sender)],
                   'data':data(-100,50,2**96,10000,-1)}
        self.transfer={'address':f.WETH,'logIndex':'0x2','topics':[f.TRANSFER,address_topic(self.sender),address_topic(self.pool)],
                       'data':data(50)}

    def test_ethereum_empty_keccak(self):
        self.assertEqual(f.keccak(b'').hex(),'c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470')

    def test_known_signature_hashes(self):
        self.assertEqual(f.topic('Swap(address,address,int256,int256,uint160,uint128,int24)'),f.V3_SWAP)
        self.assertEqual(f.topic('Transfer(address,address,uint256)'),f.TRANSFER)

    def test_create2_known_mainnet_pool(self):
        self.assertEqual(self.pool,'0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8')

    def test_identity_rejects_wrong_pool_factory_fee(self):
        for address,changes in [('0x'+'2'*40,{}),(self.pool,{'factory':'0x'+'3'*40}),
                                (self.pool,{'fee':500})]:
            with self.subTest(address=address,changes=changes),self.assertRaises(ValueError):
                f.validate_pool(address,dict(self.identity,**changes))

    def test_token_order_and_fee_width(self):
        for token0,token1,fee in [(f.WETH,self.identity['token0'],3000),
                                 (f.WETH,f.WETH,3000),(self.identity['token0'],f.WETH,2**24),
                                 (self.identity['token0'],f.WETH,True)]:
            with self.subTest(fee=fee),self.assertRaises(ValueError):f.pool_address(token0,token1,fee)

    def test_swap_sign_and_units(self):
        result=f.parse_v3_swap(self.swap,self.identity)
        self.assertEqual(result['weth_pool_delta_wei'],50)
        self.assertEqual(result['tick'],-1)
        self.swap['data']=data(100,-50,2**96,10000,1)
        self.assertEqual(f.parse_v3_swap(self.swap,self.identity)['weth_pool_delta_wei'],-50)

    def test_invalid_swap_layout(self):
        for raw in [data(1,1,2**96,0,0),data(-1,-1,2**96,0,0),
                    data(-1,1,2**160,0,0),data(-1,1,2**96,2**128,0),
                    data(-1,1,2**96,0,2**24-1),data(-1,1)]:
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                f.parse_v3_swap(dict(self.swap,data=raw),self.identity)

    def test_wrong_topic_padding(self):
        log=copy.deepcopy(self.swap);log['topics'][1]='0x'+'1'*64
        with self.assertRaises(ValueError):f.parse_v3_swap(log,self.identity)

    def test_weth_events(self):
        self.assertEqual(f.parse_weth(self.transfer)['amount_wei'],50)
        for signature,kind in [('Deposit(address,uint256)','DEPOSIT'),('Withdrawal(address,uint256)','WITHDRAWAL')]:
            log={'address':f.WETH,'topics':[f.topic(signature),address_topic(self.sender)],'data':data(7)}
            self.assertEqual(f.parse_weth(log),{'kind':kind,'account':self.sender,'amount_wei':7})

    def test_component_never_admits_total_flow(self):
        result=f.inspect_receipt({'status':'0x1','logs':[self.swap,self.transfer]},{self.pool:self.identity})
        self.assertEqual(result['known_v3_net_weth_sale_wei'],50)
        self.assertTrue(result['pool_settlements'][0]['matched'])
        self.assertEqual(result['transaction_total_dex_net_flow'],'UNKNOWN')
        self.assertIsNone(result['trade_action'])

    def test_discovery_requires_exact_create2_match(self):
        token_log={'address':self.identity['token0'],'topics':[]}
        receipt={'status':'0x1','logs':[self.swap,self.transfer,token_log]}
        result=f.discover_weth_pools(receipt)
        self.assertEqual(result['identities'],{self.pool:self.identity})
        self.assertEqual(result['unresolved_emitters'],[])
        forged=dict(self.swap,address='0x'+'2'*40)
        result=f.discover_weth_pools(dict(receipt,logs=[forged,self.transfer,token_log]))
        self.assertEqual(result['identities'],{})
        self.assertEqual(result['unresolved_emitters'],[forged['address']])

    def test_missing_candidate_or_other_fee_stays_unknown(self):
        result=f.discover_weth_pools({'status':'0x1','logs':[self.swap,self.transfer]})
        self.assertEqual(result['identities'],{})
        self.assertEqual(result['unresolved_emitters'],[self.pool])
        token_log={'address':self.identity['token0'],'topics':[]}
        different_fee=dict(self.swap,address=f.pool_address(self.identity['token0'],f.WETH,2500))
        result=f.discover_weth_pools({'status':'0x1','logs':[different_fee,self.transfer,token_log]})
        self.assertEqual(result['identities'],{})
        self.assertEqual(result['unresolved_emitters'],[different_fee['address']])

    def test_buyback_netting(self):
        buyback=dict(self.swap,logIndex='0x3',data=data(120,-60,2**96,10000,0))
        result=f.inspect_receipt({'status':'0x1','logs':[self.swap,buyback]}, {self.pool:self.identity})
        self.assertEqual(result['known_v3_net_weth_sale_wei'],-10)
        self.assertFalse(result['pool_settlements'][0]['matched'])

    def test_unverified_pool_not_zero_evidence(self):
        result=f.inspect_receipt({'status':'0x1','logs':[self.swap]}, {})
        self.assertEqual(result['unverified_v3_emitters'],[self.pool])
        self.assertEqual(result['transaction_total_dex_net_flow'],'UNKNOWN')

    def test_withdrawal_not_assumed_profit(self):
        withdraw={'address':f.WETH,'logIndex':'0x3','topics':[f.topic('Withdrawal(address,uint256)'),address_topic(self.sender)],'data':data(1)}
        result=f.inspect_receipt({'status':'0x1','logs':[self.swap,self.transfer,withdraw]},{self.pool:self.identity})
        self.assertTrue(result['native_unwrap_observed'])
        self.assertEqual(result['liquidation_collateral_provenance'],'NOT_PROVED')
        self.assertIsNone(result['trade_action'])

    def test_duplicate_log(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE_LOG_INDEX'):
            f.inspect_receipt({'status':'0x1','logs':[self.swap,self.swap]}, {self.pool:self.identity})

    def test_null_or_failed_receipt(self):
        for value in [None,{'status':'0x0','logs':[]},{'status':'0x1'}]:
            with self.subTest(value=value),self.assertRaises(ValueError):f.inspect_receipt(value,{})


if __name__=='__main__':unittest.main()
