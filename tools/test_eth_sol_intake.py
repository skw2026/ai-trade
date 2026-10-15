import copy
import json
import unittest

from eth_sol_intake import SYMBOLS, WIRE, validate_identity
from aave_market_recovery import unpack


def fixture(symbol='ETHUSDT'):
    return dict(retCode=0,result=dict(category='linear',list=[dict(symbol=symbol,
        baseCoin=symbol[:-4],quoteCoin='USDT',settleCoin='USDT',status='Trading',
        contractType='LinearPerpetual',launchTime='1600000000000',
        priceFilter=dict(tickSize='0.01'),lotSizeFilter=dict(qtyStep='0.01',minOrderQty='0.01',minNotionalValue='5'))]))


class IntakeTests(unittest.TestCase):
    def check(self,obj,symbol='ETHUSDT'):
        return validate_identity(json.dumps(obj).encode(),symbol)

    def test_both_identities(self):
        for s in SYMBOLS:
            r=self.check(fixture(s),s)
            self.assertEqual(r['symbol'],s)
            self.assertFalse(r['historical_filters_verified'])

    def test_wrong_symbol(self):
        with self.assertRaisesRegex(ValueError,'IDENTITY'): self.check(fixture('SOLUSDT'))

    def test_no_other_symbol(self):
        with self.assertRaisesRegex(ValueError,'SCOPE'): self.check(fixture('BTCUSDT'),'BTCUSDT')

    def test_wrong_market(self):
        v=fixture();v['result']['category']='spot'
        with self.assertRaisesRegex(ValueError,'CARDINALITY'): self.check(v)

    def test_no_censored_identity(self):
        v=fixture();v['result']['list']*=2
        with self.assertRaisesRegex(ValueError,'CARDINALITY'): self.check(v)

    def test_reject_nontrading_or_expiry(self):
        for field,value in [('status','Settling'),('contractType','LinearFutures'),('settleCoin','USDC')]:
            v=fixture();v['result']['list'][0][field]=value
            with self.assertRaises(ValueError): self.check(v)

    def test_launch_before_history(self):
        v=fixture();v['result']['list'][0]['launchTime']='1735689600000'
        with self.assertRaisesRegex(ValueError,'LAUNCH'): self.check(v)

    def test_finite_positive_filters(self):
        for bad in ['0','-1','NaN','Infinity']:
            v=fixture();v['result']['list'][0]['lotSizeFilter']['qtyStep']=bad
            with self.assertRaises((ValueError,ArithmeticError)): self.check(v)

    def test_api_error(self):
        v=fixture();v['retCode']=10016
        with self.assertRaisesRegex(ValueError,'API'): self.check(v)

    def test_http_parser_accepts_official_underscore_header(self):
        body=json.dumps(fixture()).encode()
        wire=b'HTTP/2 200\r\nret_code: 0\r\ncontent-type: application/json\r\n\r\n'+body+b'\n200'
        status,headers,_,raw=unpack(wire,WIRE)
        self.assertEqual(status,200);self.assertEqual(headers['ret_code'],'0')
        self.assertEqual(raw,body)

    def test_bad_wire_status(self):
        with self.assertRaisesRegex(ValueError,'STATUS_MISMATCH'):
            unpack(b'HTTP/2 403\r\n\r\n{}\n200',WIRE)


if __name__=='__main__': unittest.main()
