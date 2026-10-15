import copy
import unittest
import btc_old_output_model as m

def path(net=300,params=(1,0,1)):
    b=dict(net=net,quantity=0,insufficient=None,stopped=None,turnover=100,
        days=[dict(t=m.millis('2025-07-01'),equity=10000+net/2)],
        cycles=[dict(net=net/20,complete=True) for _ in range(20)],
        weeks={str(i):net/10 for i in range(10)},active_weeks=list(range(10)))
    return dict(parameters=list(params),books=[copy.deepcopy(b),copy.deepcopy(b)])

class DecisionTests(unittest.TestCase):
    def paths(self):return [path(),path(100,(-1,0,1)),path(100,(0,1,1)),path(400),path(250),path(250)]
    def test_positive_development_not_qualification(self):
        r=m.verdict(self.paths());self.assertEqual(r['status'],'DEVELOPMENT_SUPPORTED');self.assertTrue(r['learned'])
    def test_cash_cannot_succeed(self):
        p=self.paths();p[0]=path(0)
        self.assertEqual(m.verdict(p)['status'],'NO_GO_ECONOMIC')
    def test_risk_before_economic(self):
        p=self.paths();p[4]['books'][1]['stopped']={'t':1}
        self.assertEqual(m.verdict(p)['status'],'NO_GO_RISK')
    def test_residual_before_return_claim(self):
        p=self.paths();p[0]['books'][0]['quantity']=.1
        self.assertEqual(m.verdict(p)['status'],'INSUFFICIENT')
    def test_only_less_loss_is_not_success(self):
        p=self.paths();p[0]=path(-10);p[2]=path(-100)
        self.assertEqual(m.verdict(p)['status'],'NO_GO_ECONOMIC')
    def test_both_costs_must_show_increment(self):
        p=self.paths();p[2]['books'][0]['net']=260
        self.assertIn('PRICE_INCREMENT',m.verdict(p)['reasons'])
    def test_event_diversity(self):
        p=self.paths();p[0]['books'][1]['cycles']=p[0]['books'][1]['cycles'][:2]
        self.assertEqual(m.verdict(p)['status'],'INSUFFICIENT_DIVERSITY')
    def test_single_best_week_cannot_carry(self):
        p=self.paths();p[0]['books'][1]['weeks']={'best':300,'rest':0}
        self.assertEqual(m.verdict(p)['status'],'INSUFFICIENT_DIVERSITY')
    def test_fixed_winner_has_no_learning_claim(self):
        p=self.paths();p[0]['parameters']=[-1,0,1];p[1]=copy.deepcopy(p[0])
        self.assertFalse(m.verdict(p)['learned'])

if __name__=='__main__':unittest.main()
