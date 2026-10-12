import copy
import unittest

import aave_reserve_identity as q
import test_aave_complete_input as native_fixtures
from test_aave_weth_flow import data,address_topic


class ReserveIdentityTests(unittest.TestCase):
    def setUp(self):
        self.block={'number':'0x42','hash':'0x'+'4'*64,'timestamp':'0x1234'}
        self.atoken='0x'+'a'*40
        self.liquidator='0x'+'b'*40
        self.values=[0,1,2,3,4,5,6,7,int(self.atoken,16),9,10,11,12,13,14]
        self.identity=q.decode(data(*self.values),self.block)
        fixture=native_fixtures.NativeControlTests();fixture.setUp()
        self.row=fixture.row
        self.row.update(address=q.c.POOL,topics=[q.c.TOPIC,address_topic(q.c.WETH),
                address_topic('0x'+'c'*40),address_topic('0x'+'d'*40)],
                data=data(100,200,int(self.liquidator,16),0))
        self.receipt=fixture.receipt
        self.receipt['logs'][0].update(self.row)
        transfer=copy.deepcopy(self.receipt['logs'][0])
        transfer.update(address=q.c.WETH,logIndex='0x2',topics=[q.f.TRANSFER,
                       address_topic(self.atoken),address_topic(self.liquidator)],data=data(200))
        self.receipt['logs'].insert(0,transfer)

    def test_selector_and_fixed_call(self):
        self.assertEqual(q.f.topic('getReserveData(address)')[:10],q.SELECTOR)
        self.assertEqual(q.request('0x42'),[{'to':q.c.POOL,'data':q.SELECTOR+'0'*24+q.c.WETH[2:]},'0x42'])

    def test_latest_and_invalid_block_rejected(self):
        for value in ['latest','pending','0x0',42,None]:
            with self.subTest(value=value),self.assertRaises(ValueError):q.request(value)

    def test_reserve_identity(self):
        self.assertEqual(self.identity['atoken'],self.atoken)
        self.assertEqual(self.identity['state_position'],'END_OF_BOUND_BLOCK')

    def test_bad_shape_width_address_clock(self):
        for index,value in [(1,2**128),(6,2**40),(6,0x1235),(7,2**16),(8,0),(8,2**160)]:
            v=self.values[:];v[index]=value
            with self.subTest(index=index,value=value),self.assertRaises(ValueError):q.decode(data(*v),self.block)
        with self.assertRaises(ValueError):q.decode(data(*self.values[:-1]),self.block)

    def test_collateral_transfer(self):
        bound=q.collateral_transfer(self.receipt,self.row,self.identity)
        self.assertEqual(bound['amount_wei'],200)
        self.assertEqual(bound['sold_unit_provenance'],'UNKNOWN')

    def test_historical_binding_required(self):
        for field,value in [('block_number','0x43'),('block_hash','0x'+'5'*64),
                            ('asset','0x'+'f'*40),('pool','0x'+'f'*40)]:
            with self.subTest(field=field),self.assertRaises(ValueError):
                q.collateral_transfer(self.receipt,self.row,dict(self.identity,**{field:value}))

    def test_different_atoken_not_matched(self):
        with self.assertRaisesRegex(ValueError,'COLLATERAL_TRANSFER_NOT_UNIQUE'):
            q.collateral_transfer(self.receipt,self.row,dict(self.identity,atoken='0x'+'f'*40))

    def test_amount_and_recipient_must_match(self):
        for field,value in [('data',data(199)),('topics',[q.f.TRANSFER,address_topic(self.atoken),address_topic('0x'+'e'*40)])]:
            receipt=copy.deepcopy(self.receipt);receipt['logs'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):q.collateral_transfer(receipt,self.row,self.identity)

    def test_call_allowlist_rejects_other_contract_or_latest(self):
        for params in [[q.request('0x42')[0],'latest'],[{'to':q.c.WETH,'data':'0x'},'0x42']]:
            with self.subTest(params=params),self.assertRaisesRegex(ValueError,'HISTORICAL_RESERVE_CALL_ONLY'):
                q.r.fetch('never','eth_call',params,lambda _:None)


if __name__=='__main__':unittest.main()
