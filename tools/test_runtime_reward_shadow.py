"""Independent cash-flow audit of fixed full-runtime synthetic fixtures only."""
import argparse
from collections import defaultdict
import copy
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

ZERO = D(0)
TOL = D('0.00000005')

def near(actual, expected):
    value = D(str(actual))
    assert value.is_finite() and abs(value - expected) < TOL, (actual, str(expected))

def audit(rows):
    cash, fees, funding, realized = D(10000), ZERO, ZERO, ZERO
    qty, avg, marks, quotes = [defaultdict(lambda: ZERO) for _ in range(4)]
    previous = None
    for index, r in enumerate(rows):
        assert r['valid'] and r['seq'] == index
        if r['market']:
            m = r['market']; symbol = m['symbol']
            quotes[symbol] = D(str(m['price']))
            marks[symbol] = D(str(m['mark'])) or quotes[symbol]
        if r['fill']:
            f = r['fill']; symbol = f['symbol']
            size, price, fee = (D(str(f[k])) for k in ('qty', 'price', 'fee'))
            signed = size * f['side']; old = qty[symbol]; new = old + signed
            cash -= signed * price + fee  # Economic cash, not native margin cash.
            fees += fee
            if old * signed >= 0:
                avg[symbol] = (abs(old)*avg[symbol] + size*price)/abs(new)
            else:
                realized += min(size, abs(old))*(price-avg[symbol])*(1 if old>0 else -1)
                if new == 0: avg[symbol] = ZERO
                elif new*old<0: avg[symbol] = price
            qty[symbol] = new
            marks[symbol] = price  # These fixtures use the actual non-reference runtime.
        if r['event'] == 3:
            symbol = r['funding_symbol']; rate = D(str(r['funding_rate']))
            paid = qty[symbol]*marks[symbol]*rate if abs(rate)>D('1e-9') else ZERO
            near(r['funding_paid'], paid)
            cash -= paid; funding += paid
        equity = cash + sum(q*marks[s] for s,q in qty.items())
        margin_cash = cash + sum(q*avg[s] for s,q in qty.items())
        liquidation = cash
        positions = {p['symbol']:p for p in r['positions']}
        assert set(positions) == {s for s,q in qty.items() if abs(q)>D('1e-9')}
        for symbol, p in positions.items():
            q = qty[symbol]; side = -1 if q>0 else 1
            ticks = quotes[symbol]*(1 + D(side)*D('.0001'))/D('.1')
            exit_price = (ticks - D(side)*D('1e-9')).to_integral_value(
                rounding=ROUND_CEILING if side>0 else ROUND_FLOOR)*D('.1')
            exit_fee = abs(q)*exit_price*D('.00055')
            for k,v in dict(qty=q,average=avg[symbol],mark=marks[symbol],quote=quotes[symbol],
                            exit_price=exit_price,exit_fee=exit_fee).items(): near(p[k],v)
            liquidation += q*exit_price - exit_fee
        for k,v in dict(cash=margin_cash,equity=equity,liquidation=liquidation,
                        exit_reserve=equity-liquidation,fees=fees,funding=funding,realized=realized).items():
            near(r[k],v)
        if previous is None: assert r['reward'] is None and r['event']==0
        else: near(r['reward'], liquidation-previous)
        previous = liquidation
    return dict(receipts=len(rows),final_liquidation=str(previous),fees=str(fees),funding=str(funding))

class RuntimeAudit(unittest.TestCase):
    rows = None
    def test_independent_cash_flow(self):
        for rows in self.rows.values(): audit(rows)
    def test_reward_tamper_rejected(self):
        rows = copy.deepcopy(self.rows['async-sequence']); rows[-1]['reward'] += 1
        with self.assertRaises(AssertionError): audit(rows)
    def test_fee_tamper_rejected(self):
        rows = copy.deepcopy(self.rows['async-sequence']); rows[-1]['fees'] += 1
        with self.assertRaises(AssertionError): audit(rows)
    def test_exit_reserve_tamper_rejected(self):
        rows = copy.deepcopy(self.rows['multi-symbol']); rows[-1]['exit_reserve'] += 1
        with self.assertRaises(AssertionError): audit(rows)
    def test_sequence_gap_rejected(self):
        rows = copy.deepcopy(self.rows['async-sequence']); del rows[2]
        with self.assertRaises(AssertionError): audit(rows)
    def test_invalid_stream_rejected(self):
        rows = copy.deepcopy(self.rows['legacy-no-fill']); rows[-1]['valid'] = False
        with self.assertRaises(AssertionError): audit(rows)

def main():
    p=argparse.ArgumentParser(); p.add_argument('--driver',required=True); p.add_argument('--output')
    args=p.parse_args()
    result=subprocess.run([str(Path(args.driver).resolve())],capture_output=True,text=True,timeout=30)
    rows=defaultdict(list)
    for line in result.stdout.splitlines():
        if line.startswith('SHADOW_JSON '):
            r=json.loads(line[len('SHADOW_JSON '):]); rows[r['stream']].append(r)
    summary=dict(status='FAIL',driver_returncode=result.returncode,
                 stdout_sha256=hashlib.sha256(result.stdout.encode()).hexdigest(),
                 driver_sha256=hashlib.sha256(Path(args.driver).read_bytes()).hexdigest())
    diagnostics = [line for line in (result.stdout+result.stderr).splitlines() if '[ERROR]' in line]
    summary['unexpected_runtime_errors'] = len(diagnostics)
    success=False
    if result.returncode==0:
        assert 'RUNTIME_SHADOW_CASES 23' in result.stdout
        assert set(rows)=={'full-runtime','async-sequence','multi-symbol','legacy-no-fill'}
        RuntimeAudit.rows=rows
        tests=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeAudit))
        success=tests.wasSuccessful() and not diagnostics
        summary.update(cpp_cases=23,audit_tests=tests.testsRun,failures=len(tests.failures),errors=len(tests.errors))
        if success:
            summary.update(status='RUNTIME_REWARD_SHADOW_SYNTHETIC_PASS',streams={k:audit(v) for k,v in rows.items()})
    else: print(result.stdout[-12000:]+result.stderr,file=sys.stderr)
    if diagnostics: print('\n'.join(diagnostics),file=sys.stderr)
    if args.output:
        path=Path(args.output)
        if path.exists(): path=path.with_name(path.stem+'-retry'+path.suffix)
        with path.open('x') as f: json.dump(summary,f,indent=2,sort_keys=True); f.write('\n')
        with path.with_suffix('.log').open('x') as f: f.write(result.stdout); f.write(result.stderr)
    print(json.dumps(summary,sort_keys=True))
    return 0 if success else 1
if __name__=='__main__': sys.exit(main())
