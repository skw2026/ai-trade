import copy
from decimal import Decimal as D
import unittest
import bybit_linear_last_known_cash as c
import bybit_linear_depth_endpoint as old
from test_bybit_linear_depth_endpoint import book,instrument,END


def inputs(bid='103000'):
    s=book().state();f=book(old.SYMBOLS[0],bid,str(D(bid)+1)).state()
    previous=dict(symbol=f['symbol'],status='INPUT_INSUFFICIENT',reasons=['PUBLICATION_SKEW'])
    return s,f,instrument(),END,previous


class ConditionalTests(unittest.TestCase):
    def test_fresh_cases_equal_frozen_kernel(self):
        a=c.pair(*inputs());b=old.economic(book(),book(old.SYMBOLS[0],'103000','103001'),instrument(),END)
        self.assertEqual(a['cases'],b['cases']);self.assertEqual(len(a['cases']),36)

    def test_stale_clock_retained_not_faked(self):
        args=list(inputs());args[1]['quote']['ts']-=6000;args[1]['quote']['cts']-=6000;args[1]['quote']['mono']-=6000
        before=copy.deepcopy(args);r=c.pair(*args)
        self.assertEqual(args,before);self.assertEqual(r['time_controls']['publication_age_ms']['future'],6100)
        self.assertEqual(r['original_execution_reasons'],['PUBLICATION_SKEW'])
        self.assertEqual(len(r['cases']),36);self.assertFalse(r['execution_admitted'])

    def test_protocol_taint_still_blocks(self):
        for key,value in [('issue','SEQUENCE_REGRESSION'),('initialized',False),('snapshots',2),('quote',None)]:
            args=list(inputs());args[1][key]=value;r=c.pair(*args)
            self.assertEqual(r['status'],'INPUT_INSUFFICIENT');self.assertEqual(r['cases'],[])

    def test_future_price_clock_rejected(self):
        args=list(inputs());args[1]['quote']['ts']=END+1
        with self.assertRaisesRegex(ValueError,'CLOCK_ORDER'):c.pair(*args)

    def test_grid_identity_still_enforced(self):
        args=list(inputs());args[2]['lotSizeFilter']['qtyStep']='0.01'
        with self.assertRaisesRegex(ValueError,'GRID'):c.pair(*args)
        args=list(inputs());args[1]['symbol']=old.SYMBOLS[1]
        with self.assertRaisesRegex(ValueError,'PAIR_IDENTITY'):c.pair(*args)

    def test_insufficient_pressure_depth_not_filled(self):
        args=list(inputs());args[0]['asks']=[['100000','0.02504']];r=c.pair(*args)
        self.assertEqual(r['status'],'INPUT_INSUFFICIENT');self.assertEqual(len(r['cases']),18)
        self.assertEqual(r['reasons'],['stress:DEPTH_INSUFFICIENT'])

    def test_empty_and_crossed_books(self):
        args=list(inputs());args[1]['bids']=[];self.assertEqual(c.pair(*args)['cases'],[])
        args=list(inputs());args[1]['bids']=[['104000','1']]
        with self.assertRaisesRegex(ValueError,'CROSSED'):c.pair(*args)

    def test_pressure_negative_base_positive_is_not_support(self):
        r=c.pair(*inputs('100350'));self.assertGreater(D(r['base_cash_range'][1]),0)
        self.assertLess(D(r['stress_cash_range'][1]),0)
        self.assertEqual(r['status'],'LAST_KNOWN_CONDITIONAL_CASH_UNSUPPORTED')

    def test_conditional_positive_is_not_risk_or_qualification(self):
        r=c.pair(*inputs());self.assertEqual(r['status'],'LAST_KNOWN_CONDITIONAL_CASH_MARGIN')
        self.assertFalse(r['qualified']);self.assertFalse(r['reference_terminal_margin_supported'])
        self.assertFalse(r['quote_age_risk_budget_certified']);self.assertEqual(r['additional_cash_cost_budget'],r['stress_cash_range'][0])

    def test_full_grid_and_opportunity_ranges(self):
        r=c.pair(*inputs());keys={(x['cost'],x['index_multiple'],x['deviation'],x['delivery_rate']) for x in r['cases']}
        self.assertEqual(len(keys),36)
        for rate in ('6','12'):
            vals=[D(x['excess_occupied_'+rate]) for x in r['cases'] if x['cost']=='stress']
            self.assertEqual(list(map(D,r['stress_excess_occupied_'+rate+'_range'])),[min(vals),max(vals)])

    def test_all_eight_no_selection(self):
        s,f,meta,end,previous=inputs();symbols=old.SYMBOLS
        original=dict(endpoint_mono_ms=120000,endpoint_wall_ms=end,qualified=False,books={'BTCUSDT':s},results=[])
        instruments={}
        for symbol in symbols:
            fb=copy.deepcopy(f);fb['symbol']=symbol;original['books'][symbol]=fb
            prior=copy.deepcopy(previous);prior['symbol']=symbol;original['results'].append(prior)
            instruments[symbol]=instrument(symbol)
        r=c.panel(original,instruments);self.assertEqual([x['symbol'] for x in r['results']],list(symbols))
        self.assertEqual(sum(len(x['cases']) for x in r['results']),288);self.assertFalse(r['qualified'])
        original['results'].pop()
        with self.assertRaisesRegex(ValueError,'FIXED_TERMS'):c.panel(original,instruments)

if __name__=='__main__':unittest.main()
