import copy
from decimal import Decimal as D
import unittest
import bybit_linear_prefunded_tail as m
import bybit_linear_last_known_cash as cash
from test_bybit_linear_last_known_cash import inputs


def fixture():return cash.pair(*inputs())


class PrefundTests(unittest.TestCase):
    def test_one_fixed_reserve_not_terminal_hindsight(self):
        r=m.analyze_term(fixture());self.assertEqual(len(r['cases']),36)
        self.assertEqual(len({c['fixed_margin'] for c in r['cases']}),1)
        self.assertTrue(r['reference_terminal_nonnegative']);self.assertEqual(r['zero_headroom_cases'],3)

    def test_profit_unchanged_and_input_not_mutated(self):
        f=fixture();original=copy.deepcopy(f);r=m.analyze_term(f)
        self.assertEqual(f,original)
        self.assertEqual([c['profit'] for c in r['cases']],[c['profit'] for c in f['cases']])

    def test_added_capital_does_not_create_cash(self):
        f=fixture();r=m.analyze_term(f)
        for before,after in zip(f['cases'],r['cases']):
            self.assertEqual(D(after['occupied_capital']),D(before['occupied_capital'])+D(after['additional_margin']))
            self.assertEqual(D(after['remaining_capital'])+D(after['occupied_capital']),D(10000))
            self.assertLessEqual(D(after['excess_occupied_6']),D(before['excess_occupied_6']))

    def test_budget_not_relaxed(self):
        f=fixture()
        for c in f['cases']:c['occupied_capital']='9999'
        r=m.analyze_term(f);self.assertFalse(r['budget_ok']);self.assertEqual(r['status'],'REFERENCE_BUDGET_INSUFFICIENT')

    def test_already_funded_does_not_withdraw_reserve(self):
        f=fixture()
        for c in f['cases']:c['initial_margin_reference']='6000';c['occupied_capital']='9000'
        r=m.analyze_term(f);self.assertEqual(D(r['fixed_margin']),6000)
        self.assertEqual(list(map(D,r['additional_margin_range'])),[D(0),D(0)])

    def test_missing_duplicate_changed_grid(self):
        for mode in ('missing','duplicate','altered'):
            f=fixture()
            if mode=='missing':f['cases'].pop()
            elif mode=='duplicate':f['cases'].append(copy.deepcopy(f['cases'][0]))
            else:f['cases'][0]['index_multiple']='3'
            with self.assertRaisesRegex(ValueError,'COMPLETE_GRID'):m.analyze_term(f)

    def test_no_new_execution_qualification(self):
        f=fixture();r=m.analyze_term(f)
        for k in ('qualified','execution_admitted','path_validated','actual_margin_rule_certified'):self.assertFalse(r[k])
        self.assertEqual(r['original_execution_reasons'],f['original_execution_reasons'])

    def test_unsafe_input_is_not_analyzed(self):
        f=fixture();f['reasons']=['stress:DEPTH_INSUFFICIENT'];r=m.analyze_term(f)
        self.assertEqual(r['status'],'INPUT_INSUFFICIENT');self.assertEqual(r['cases'],[])

    def test_identity_clocks_and_quantity(self):
        for key,value,error in [('q','0.026','FIXED_QUANTITY'),('index','60000','FIXED_SPOT_REFERENCE'),('days','0','FIXED_POSITIVE_DAYS')]:
            f=fixture();f['cases'][0][key]=value
            with self.assertRaisesRegex(ValueError,error):m.analyze_term(f)

    def test_nonfinite_and_non_decimal(self):
        for value in ('NaN','Infinity',1.1,True):
            f=fixture();f['cases'][0]['profit']=value
            with self.assertRaises(ValueError):m.analyze_term(f)

    def test_break_even_rate_identity(self):
        r=m.analyze_term(fixture())
        for c in r['cases']:
            rate=D(c['break_even_simple_capital_rate_pct'])/100
            recovery=D(c['occupied_capital'])*rate*D(c['days'])/365
            self.assertAlmostEqual(recovery,D(c['profit']),places=22)

    def test_margin_cannot_use_unallocated_buffer(self):
        r=m.analyze_term(fixture())
        self.assertGreater(D(r['required_terminal_margin']),0)
        self.assertGreater(D(r['additional_margin_range'][0]),0)
        self.assertEqual(D(r['terminal_headroom_range'][0]),0)


if __name__=='__main__':unittest.main()
