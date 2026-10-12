import copy
import unittest

import aave_weth_boundary as b
import test_aave_reserve_identity as fixtures
from test_aave_weth_flow import address_topic,data


class WethBoundaryTests(unittest.TestCase):
    def setUp(self):
        fixture=fixtures.ReserveIdentityTests();fixture.setUp()
        self.row,self.receipt,self.block,self.reserve=fixture.row,fixture.receipt,fixture.block,data(*fixture.values)
        self.liquidator=fixture.liquidator
        self.block['transactions']=[self.row['transactionHash']]
        self.usdc='0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'
        self.pool=b.f.pool_address(self.usdc,b.c.WETH,3000)
        template=copy.deepcopy(self.receipt['logs'][0])
        def log(address,index,topics,raw):return dict(template,address=address,logIndex=hex(index),topics=topics,data=raw)
        self.receipt['logs'].append(log(b.c.WETH,4,[b.f.TRANSFER,address_topic(self.liquidator),address_topic(self.pool)],data(150)))
        self.receipt['logs'].append(log(self.pool,5,[b.f.V3_SWAP,address_topic(self.liquidator),address_topic(self.liquidator)],data(-100,150,2**96,10000,0)))
        self.receipt['logs'].append(log(b.c.WETH,6,[b.f.topic('Withdrawal(address,uint256)'),address_topic(self.liquidator)],data(50)))
        self.receipt['logs'].insert(0,log(self.usdc,1,[b.f.TRANSFER,address_topic(self.pool),address_topic(self.liquidator)],data(100)))

    def classify(self):return b.classify(self.receipt,[self.row],self.block,self.reserve)

    def append(self,changes):
        log=copy.deepcopy(self.receipt['logs'][-1]);log.update(logIndex=hex(int(log['logIndex'],16)+1),**changes)
        self.receipt['logs'].append(log)

    def test_closed_weth_with_unwrap(self):
        result=self.classify()
        self.assertEqual(result['status'],'WETH_BOUNDARY_VERIFIED')
        self.assertEqual(result['weth_dex_net_sale_wei'],150)
        self.assertEqual(result['net_weth_inventory_deltas_wei'][self.liquidator],0)
        self.assertEqual(result['native_eth_global_flow'],'NOT_EVALUATED')
        self.assertEqual(result['collateral_unit_provenance'],'UNKNOWN')
        self.assertFalse(result['signal_admitted'])
        self.assertIsNone(result['trade_action'])

    def test_unknown_weth_edge_blocks_whole_not_zero(self):
        self.append({'topics':[b.f.TRANSFER,address_topic(self.liquidator),address_topic('0x'+'e'*40)],'data':data(1)})
        result=self.classify()
        self.assertEqual(result['status'],'WETH_BOUNDARY_UNKNOWN')
        self.assertIsNone(result['weth_dex_net_sale_wei'])
        self.assertEqual(result['decoded_v3_component_wei'],150)

    def test_non_weth_unrecognized_swap_is_not_global_eth_gate(self):
        self.append({'address':'0x'+'e'*40,'topics':[b.f.V3_SWAP,address_topic(self.liquidator),address_topic(self.liquidator)],'data':data(-1,1,2**96,10000,0)})
        result=self.classify()
        self.assertEqual(result['status'],'WETH_BOUNDARY_VERIFIED')
        self.assertEqual(result['pool_discovery']['unresolved_emitters'],['0x'+'e'*40])

    def test_known_pool_does_not_explain_unknown_other_end(self):
        for unknown_swap in (False,True):
            with self.subTest(unknown_swap=unknown_swap):
                self.setUp()
                self.receipt['logs'][3]['topics'][1]=address_topic('0x'+'e'*40)
                if unknown_swap:
                    self.append({'address':'0x'+'e'*40,'topics':[b.f.V3_SWAP,
                                 address_topic(self.liquidator),address_topic(self.pool)],
                                 'data':data(100,-150,2**96,10000,0)})
                result=self.classify()
                self.assertEqual(result['status'],'WETH_BOUNDARY_UNKNOWN')
                self.assertIsNone(result['weth_dex_net_sale_wei'])

    def test_liquidator_role_cannot_hide_unknown_swap(self):
        self.append({'address':self.liquidator,'topics':[b.f.V3_SWAP,
                     address_topic(self.liquidator),address_topic(self.pool)],
                     'data':data(100,-150,2**96,10000,0)})
        self.assertEqual(self.classify()['status'],'WETH_BOUNDARY_UNKNOWN')

    def test_outgoing_pool_edge_also_checks_other_end(self):
        self.append({'topics':[b.f.TRANSFER,address_topic(self.pool),address_topic('0x'+'e'*40)],'data':data(200)})
        self.append({'address':self.pool,'topics':[b.f.V3_SWAP,address_topic(self.liquidator),address_topic(self.liquidator)],'data':data(120,-200,2**96,10000,0)})
        self.assertEqual(self.classify()['status'],'WETH_BOUNDARY_UNKNOWN')

    def test_two_authenticated_pools_cancel_internal_weth_edge(self):
        other_pool=b.f.pool_address(self.usdc,b.c.WETH,500)
        self.append({'topics':[b.f.TRANSFER,address_topic(self.pool),address_topic(other_pool)],'data':data(200)})
        self.append({'address':self.pool,'topics':[b.f.V3_SWAP,address_topic(self.liquidator),address_topic(self.liquidator)],'data':data(120,-200,2**96,10000,0)})
        self.append({'address':other_pool,'topics':[b.f.V3_SWAP,address_topic(self.liquidator),address_topic(self.liquidator)],'data':data(-120,200,2**96,10000,0)})
        result=self.classify()
        self.assertEqual(result['status'],'WETH_BOUNDARY_VERIFIED')
        self.assertEqual(result['weth_dex_net_sale_wei'],150)
        self.assertEqual(len(result['pool_discovery']['identities']),2)

    def test_flash_or_lp_event_not_hidden_by_netting(self):
        self.append({'address':self.pool,'topics':[b.f.topic('Flash(address,address,uint256,uint256,uint256,uint256)')],'data':data(0)})
        self.assertEqual(self.classify()['status'],'WETH_BOUNDARY_UNKNOWN')

    def test_pool_transfer_mismatch_unknown(self):
        self.receipt['logs'][3]['data']=data(149)
        self.assertEqual(self.classify()['status'],'WETH_BOUNDARY_UNKNOWN')

    def test_missing_collateral_identity_unknown(self):
        values=b.f.words(self.reserve,15);values[8]=int('e'*40,16);self.reserve=data(*values)
        self.assertEqual(self.classify()['status'],'WETH_BOUNDARY_UNKNOWN')

    def test_buybacks_are_netted(self):
        self.append({'topics':[b.f.TRANSFER,address_topic(self.pool),address_topic(self.liquidator)],'data':data(200)})
        self.append({'address':self.pool,'topics':[b.f.V3_SWAP,address_topic(self.liquidator),address_topic(self.liquidator)],'data':data(120,-200,2**96,10000,0)})
        self.assertEqual(self.classify()['weth_dex_net_sale_wei'],-50)

    def test_conversions_into_pool_not_assumed_swap(self):
        self.append({'topics':[b.f.topic('Deposit(address,uint256)'),address_topic(self.pool)],'data':data(1)})
        self.assertEqual(self.classify()['status'],'WETH_BOUNDARY_UNKNOWN')

    def test_rewrap_included_in_inventory(self):
        self.append({'topics':[b.f.topic('Deposit(address,uint256)'),address_topic(self.liquidator)],'data':data(20)})
        result=self.classify()
        self.assertEqual(result['weth_dex_net_sale_wei'],150)
        self.assertEqual(result['net_weth_inventory_deltas_wei'][self.liquidator],20)

    def test_zero_transfer_and_approval_not_net_flow(self):
        for topic,amount in [(b.f.TRANSFER,0),(b.f.APPROVAL,999)]:
            self.append({'topics':[topic,address_topic(self.liquidator),address_topic('0x'+'e'*40)],'data':data(amount)})
        self.assertEqual(self.classify()['weth_dex_net_sale_wei'],150)

    def test_native_block_binding_mandatory(self):
        self.block['hash']='0x'+'9'*64
        with self.assertRaisesRegex(ValueError,'BLOCK_IDENTITY'):self.classify()

    def test_duplicate_liquidations_require_two_transfers(self):
        other=copy.deepcopy(self.row);other['logIndex']='0x8'
        target=copy.deepcopy(self.receipt['logs'][2]);target.update(other)
        self.receipt['logs'].append(target)
        result=b.classify(self.receipt,[self.row,other],self.block,self.reserve)
        self.assertEqual(result['status'],'WETH_BOUNDARY_UNKNOWN')
        transfer=copy.deepcopy(self.receipt['logs'][1]);transfer['logIndex']='0x7'
        self.receipt['logs'].insert(-1,transfer)
        result=b.classify(self.receipt,[self.row,other],self.block,self.reserve)
        self.assertEqual(result['status'],'WETH_BOUNDARY_VERIFIED')


if __name__=='__main__':unittest.main()
