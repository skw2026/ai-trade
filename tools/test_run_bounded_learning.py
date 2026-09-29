"""Synthetic-only regression; never opens the retained historical development CSV."""
import json
import math
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import run_bounded_learning as b

if os.environ.get('BOUNDED_LEARNING_TEST_DRIVER'):
    b.BINARY=Path(os.environ['BOUNDED_LEARNING_TEST_DRIVER'])

def synthetic(n, reverse_at=None):
    rows=[]
    previous=100.0
    for i in range(n):
        price=100+.015*i+math.sin(i*.18)
        if reverse_at is not None and i>=reverse_at:
            price=200-price
        rows.append(' '.join(map(str,[1704067200000+i*300000,previous,max(price,previous)+.1,
            min(price,previous)-.1,price,100,price,0,int(i>=100),0,10000]))+'\n')
        previous=price
    return rows

def driver(rows, mode=None):
    r=subprocess.run([str(b.BINARY),*(mode or ['development'])],input=''.join(rows),
                     text=True,capture_output=True,timeout=15)
    if r.returncode:
        raise ValueError(r.stderr)
    return [json.loads(x) for x in r.stdout.splitlines()]

class BoundedLearningTest(unittest.TestCase):
    def test_next_open_target_only_from_completed_signal(self):
        s=dict(trend=2500,defensive=-2500,weight=.55)
        t=b.targets(s)
        self.assertAlmostEqual(t['adaptive'],250)
        self.assertEqual(t['fixed'],0)
        self.assertEqual(t['adaptive_unit'],2500)

    def test_same_direction_unit_removes_scaling(self):
        t=b.targets(dict(trend=2500,defensive=0,weight=.6))
        self.assertNotEqual(t['adaptive'],t['fixed'])
        self.assertEqual(t['adaptive_unit'],t['fixed_unit'])

    def test_weight_and_nominal_rejected(self):
        for s in [dict(trend=2500,defensive=0,weight=.7),
                  dict(trend=99999,defensive=0,weight=.5),
                  dict(trend=float('nan'),defensive=0,weight=.5)]:
            with self.assertRaises(ValueError):b.targets(s)

    def test_flat_roundtrip_costs_cash(self):
        w=b.Wallet();w.trade(2500,100,100);w.trade(0,100,100)
        self.assertEqual(w.qty,0)
        self.assertAlmostEqual(10000-w.cash,w.fees+w.slippage)
        self.assertEqual(w.fills,2)
        self.assertGreater(w.drawdown,0)

    def test_short_funding_credit(self):
        w=b.Wallet(qty=-2,cash=10200)
        w.fund(100,.001)
        self.assertAlmostEqual(w.cash,10200.2)
        self.assertAlmostEqual(w.funding,-.2)

    def test_old_holder_funding_before_new_entry(self):
        w=b.Wallet();w.fund(100,.01);w.trade(2500,100,100)
        self.assertEqual(w.funding,0)
        q=w.qty;w.fund(100,.01);w.trade(0,100,100)
        self.assertAlmostEqual(w.funding,q)

    def test_quantity_rounds_toward_zero(self):
        for target in (123.456,-123.456):
            w=b.Wallet();w.trade(target,101,101)
            self.assertLessEqual(abs(w.qty)*101,abs(target))
            self.assertAlmostEqual(w.qty*1000,round(w.qty*1000))

    def test_stress_not_less_expensive(self):
        a=b.Wallet();z=b.Wallet(2)
        for target,price in [(2500,100.03),(-2500,101.04),(0,99.91)]:
            a.trade(target,price,price);z.trade(target,price,price)
        self.assertLess(z.cash,a.cash)

    def test_intrabar_envelope_detects_hidden_risk(self):
        w=b.Wallet(qty=25,cash=7500)
        w.observe(130,70)
        self.assertGreater(w.drawdown,.08)
        self.assertEqual(w.equity(100),10000)

    def test_sample_capacity_and_absolute_economics(self):
        self.assertIsNone(b.hac_lower([100]*25))
        self.assertEqual(b.hac_lower([100]*26),100)
        wallets={k:b.Wallet(cash=9999) for k in b.ARMS}
        weeks=[dict(fixed=-2,adaptive=-1,adaptive_stress=-1,fixed_unit=-2,adaptive_unit=-1)]*26
        self.assertEqual(b.decide('COMPLETE',wallets,2,weeks),'NO_DEVELOPMENT_EVIDENCE')

    def test_scaling_only_cannot_pass(self):
        wallets={k:b.Wallet(cash=10010) for k in b.ARMS}
        weeks=[dict(fixed=1,adaptive=2,adaptive_stress=1,fixed_unit=1,adaptive_unit=1)]*26
        self.assertEqual(b.decide('COMPLETE',wallets,2,weeks),'NO_DEVELOPMENT_EVIDENCE')

    def test_risk_and_withdrawal_are_terminal(self):
        for reason in ('REFERENCE_RISK_STOP','SAFETY_WITHDRAWAL'):
            self.assertEqual(b.decide(reason,{},0,[]),reason)

    def test_real_production_causal_prefix(self):
        a=driver(synthetic(620));z=driver(synthetic(620,480))
        self.assertEqual(a[:480],z[:480])
        self.assertNotEqual(a[-1]['trend'],z[-1]['trend'])

    def test_frozen_weights_never_learn(self):
        rows=driver(synthetic(850),['frozen','.55','.45','.5'])
        for r in rows:
            self.assertFalse(r['updated']);self.assertFalse(r['withdrawn'])
            self.assertEqual(r['weights'],[.55,.45,.5])
            self.assertEqual(r['weight'],[.55,.45,.5][r['bucket']])
            self.assertEqual(r['action'],'')

    def test_frozen_bad_grid_and_confirmation_command_rejected(self):
        for mode in (['frozen','.56','.5','.5'],['frozen','nan','.5','.5'],['confirmation']):
            with self.assertRaises(ValueError):driver([],mode)

    def test_clock_reversal_rejected(self):
        rows=synthetic(3);rows[2]=rows[0]
        with self.assertRaisesRegex(ValueError,'clock'):driver(rows)

    def test_native_weight_bounds_and_withdrawal_latch(self):
        rows=driver(synthetic(1200));previous=[.5]*3
        for r in rows:
            for old,w in zip(previous,r['weights']):
                self.assertGreaterEqual(w,.4-1e-9);self.assertLessEqual(w,.6+1e-9)
                self.assertLessEqual(abs(old-w),.05+1e-9)
            previous=r['weights']
        withdrawal=[i for i,r in enumerate(rows) if r['withdrawn']]
        if withdrawal:
            for r in rows[withdrawal[0]+1:]:
                self.assertFalse(r['updated']);self.assertTrue(r['withdrawn'])

    def test_protocol_clock_is_bar_close(self):
        row=dict(timestamp='1704067200000',open=100,high=101,low=99,price=100,volume=1,
                 mark_close=100,funding_rate_per_interval=0)
        fields=b.protocol(row,False,b.Wallet()).split()
        self.assertEqual(int(fields[0]),1704067500000)
        self.assertEqual(fields[8],'0')

    def test_isolated_whole_runner_and_attempt_consumption(self):
        # Deliberately small synthetic development interval; no retained-market reads.
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);start=1704067200000
            path=root/'synthetic.csv'
            fields=['timestamp','symbol','open','high','low','price','volume','interval_ms',
                    'funding_rate_per_interval','mark_open','mark_close','mark_high','mark_low']
            with path.open('x') as f:
                f.write(','.join(fields)+'\n')
                for line in synthetic(900):
                    v=line.split();ts=int(v[0]);op,hi,lo,close,vol=map(float,v[1:6])
                    f.write(','.join(map(str,[ts,'BTCUSDT',op,hi,lo,close,vol,300000,0,op,close,hi,lo]))+'\n')
            contract={'input':dict(csv=str(path),development_start_ms=start+100*300000,
                development_end_exclusive_ms=start+900*300000),'maximum_compute_seconds':30}
            cp=root/'contract.json';b.save(cp,contract)
            b.save(root/'execution-freeze.json',dict(contract_sha256=b.sha(cp),
                   binary_sha256=b.sha(b.BINARY),sources={}))
            with patch.object(b,'RUN',root),patch.object(b,'CONTRACT',cp),\
                 patch.object(b,'read_contract',return_value=contract),patch('builtins.print'):
                b.execute()
                result=json.loads((root/'result.json').read_text())
                self.assertEqual(result['confirmation_runs'],0)
                self.assertEqual(result['candidate_status'],'NO_QUALIFIED_CANDIDATE')
                self.assertTrue(result['terminal_flat'])
                self.assertTrue(all(w['qty']==0 for w in result['wallets'].values()))
                with self.assertRaises(FileExistsError):b.execute()
                self.assertEqual(json.loads((root/'result.json').read_text()),result)

if __name__=='__main__':unittest.main()
