"""Synthetic clocks, delayed intents, paired safety and independent ledger audit."""
import csv
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from audit_native_closed_loop import audit, MS

START=288*MS
FIELDS=('timestamp','open','price','mark_open','mark_high','mark_low','mark_close','funding_rate_per_interval')

def bar(ts,price=50000,close=None,rate=0,high=None,low=None):
    closing=price if close is None else close
    return dict(zip(FIELDS,(ts,price,closing,price,max(price,closing) if high is None else high,
        min(price,closing) if low is None else low,closing,rate)))

class NativeLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.binary=Path(os.environ['NATIVE_CLOSED_LOOP_BINARY']).resolve()
        self.assertTrue(self.binary.is_file())

    def run_case(self,bars,*,bad=None,reason=None,audit_result=True):
        warmup=[bar(i*MS) for i in range(288)]
        warmup[-1]=bar(287*MS,close=45000)
        rows=warmup+bars
        if bad:bad(rows)
        source=self.root/'input.csv';out=self.root/'output'
        with source.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
        result=subprocess.run([str(self.binary),str(source),str(out),str(START),str(START+len(bars)*MS)],
                              text=True,capture_output=True,timeout=120)
        if bad:
            self.assertNotEqual(result.returncode,0)
            self.assertIn('NATIVE_LOOP_ERROR '+reason,result.stderr)
            return result
        self.assertEqual(result.returncode,0,result.stderr)
        if audit_result:return audit(out,source,START,START+len(bars)*MS)
        return out,source

    def events(self,out,name='normal_base'):
        with (out/(name+'.csv')).open() as f:return list(csv.DictReader(f))

    def test_completed_features_delay_and_cap_no_refill(self):
        bars=[bar(START+i*MS,price=p,close=50000,rate=.000123456789 if i%2 else -.00002)
              for i,p in enumerate([50000,60000,50000,51000,50000])]
        out,source=self.run_case(bars,audit_result=False)
        result=audit(out,source,START,START+len(bars)*MS)
        normal=[r for r in self.events(out) if r['event']=='CLOSE']
        delay=[r for r in self.events(out,'delay_base') if r['event']=='CLOSE']
        self.assertAlmostEqual(float(normal[0]['qty']),.05)
        self.assertAlmostEqual(float(normal[1]['qty']),.041)
        self.assertEqual(float(normal[2]['qty']),float(normal[1]['qty']))
        self.assertEqual(float(delay[0]['qty']),0)
        self.assertAlmostEqual(float(delay[1]['qty']),.041)
        self.assertEqual(result['normal']['domain_bars'],5)

    def test_intrabar_exit_next_open_and_paired_stress_stop(self):
        bars=[bar(START,high=67387),bar(START+MS,52000,rate=.0001),bar(START+2*MS)]
        out,source=self.run_case(bars,audit_result=False)
        result=audit(out,source,START,START+len(bars)*MS)
        base=self.events(out);stress=self.events(out,'normal_stress')
        first=lambda records,kind:next(r for r in records if r['event']==kind)
        self.assertLess(float(first(base,'RANGE')['dd']),.08)
        self.assertGreaterEqual(float(first(stress,'RANGE')['dd']),.08)
        self.assertEqual(first(base,'CLOSE')['stopped'],'1')
        self.assertAlmostEqual(float(first(base,'CLOSE')['qty']),.05)
        self.assertEqual(int(first(base,'EXIT')['ts']),START+MS)
        self.assertEqual(result['normal']['economic_status'],'REJECT_NATIVE_RISK')

    def test_opening_funding_stop_waits_for_following_open(self):
        bars=[bar(START),bar(START+MS,rate=.4),bar(START+2*MS,52000,rate=.0001)]
        out,source=self.run_case(bars,audit_result=False)
        result=audit(out,source,START,START+len(bars)*MS)
        events=self.events(out)
        at=[r for r in events if int(r['index'])==2 and r['event']=='EXEC'][0]
        self.assertAlmostEqual(float(at['qty']),.05)
        self.assertEqual(result['normal']['exit_at_ms'],START+2*MS)

    def test_last_signal_not_executed_beyond_terminal(self):
        result=self.run_case([bar(START)])
        self.assertGreater(result['normal']['wal'][0]['fills'],0)
        self.assertEqual(result['delay']['wal'][0]['fills'],0)
        self.assertTrue(result['delay']['terminal_flat'])

    def test_terminal_risk_latch_does_not_create_an_extra_bar(self):
        result=self.run_case([bar(START,high=70000)])
        self.assertEqual(result['normal']['exit_at_ms'],START+MS)
        self.assertEqual(result['normal']['domain_bars'],1)
        self.assertEqual(result['normal']['economic_status'],'REJECT_NATIVE_RISK')

    def test_quiescent_restart_restores_delay_and_economics(self):
        bars=[bar(START+i*MS) for i in range(8641)]
        bars[-3]=bar(START+8638*MS,close=60000)
        out,source=self.run_case(bars,audit_result=False)
        result=audit(out,source,START,START+len(bars)*MS)
        recovered=next(r for r in self.events(out,'delay_base') if r['event']=='RECOVER')
        self.assertEqual((float(recovered['intent']),float(recovered['due'])),(-2500,2500))
        for pair in result.values():
            self.assertEqual(pair['bridge_restarts'],2)
            self.assertEqual(pair['domain_bars'],8641)

    def test_missing_warmup_rejected(self):
        self.run_case([bar(START)],bad=lambda rows:rows.pop(0),reason='BAR_GRID')

    def test_duplicate_clock_rejected(self):
        self.run_case([bar(START),bar(START+MS)],bad=lambda rows:rows[-1].update(timestamp=START),reason='BAR_GRID')

    def test_invalid_mark_range_rejected(self):
        self.run_case([bar(START)],bad=lambda rows:rows[-1].update(mark_low=51000),reason='PRICE_RANGE')

    def test_trace_and_wal_tampering_rejected(self):
        out,source=self.run_case([bar(START)],audit_result=False)
        path=out/'normal_base.csv';original=path.read_text()
        path.write_text(original.replace('OPEN,','CLOSE,',1))
        with self.assertRaises(ValueError):audit(out,source,START,START+MS)
        path.write_text(original)
        journal=out/'normal_base/intent.events'
        journal.write_text(journal.read_text()+'E unknown\n')
        with self.assertRaises(ValueError):audit(out,source,START,START+MS)

    def test_output_directory_not_overwritten(self):
        out,source=self.run_case([bar(START)],audit_result=False)
        before=(out/'normal_base.csv').read_bytes()
        result=subprocess.run([str(self.binary),str(source),str(out),str(START),str(START+MS)],capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(before,(out/'normal_base.csv').read_bytes())

if __name__=='__main__':unittest.main()
