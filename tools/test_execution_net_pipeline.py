"""Whole offline runner with synthetic inputs only, including independent audit."""
import csv
import datetime as dt
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import run_execution_net_learning as r
import audit_execution_net_learning as a
from test_run_bounded_learning import synthetic
from test_execution_net_learning import rising

if os.environ.get('EXECUTION_NET_TEST_DRIVER'):
    r.BINARY=Path(os.environ['EXECUTION_NET_TEST_DRIVER'])

def fixture(root,n=900):
    start=1704067200000;path=root/'synthetic.csv'
    with path.open('x') as f:
        writer=csv.writer(f)
        writer.writerow(['timestamp','symbol','open','high','low','price','volume','interval_ms',
                         'funding_rate_per_interval','mark_open','mark_close','mark_high','mark_low'])
        for line in synthetic(n):
            v=line.split();ts=int(v[0]);op,hi,lo,cl,vol=map(float,v[1:6])
            writer.writerow([ts,'BTCUSDT',op,hi,lo,cl,vol,300000,0,op,cl,hi,lo])
    c=dict(started_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
           confirmation=dict(interval=None,maximum_runs=0,learning=False),maximum_market_attempts=1,new_network_requests=0,
           input=dict(path=str(path),development_start=start+100*300000,development_end=start+n*300000),
           maximum_compute_seconds=30,maximum_evaluation_windows=438)
    cp=root/'contract.json';r.save(cp,c);r.save(root/'baseline.json',dict(preserved={}))
    r.save(root/'execution-freeze.json',dict(contract_sha256=r.sha(cp),binary_sha256=r.sha(r.BINARY),
           input_sha256=r.sha(path),sources={}))
    return cp,c,path

class SyntheticSignalProcess:
    """Predictable signal fixture for positive-update and opening-stop branches."""
    def __init__(self,*args,**kwargs):
        self.stdin=self;self.stdout=self;self.stderr=io.StringIO();self.ts=None
    def write(self,text):self.ts=int(text.split()[0])
    def flush(self):pass
    def readline(self):
        return json.dumps(dict(ts=self.ts,bucket=1,trend=2000,defensive=0,updated=False,withdrawn=False))+'\n'
    def close(self):pass
    def wait(self,timeout):return 0

def positive_input(root,path,n,funding_at=None):
    rows=[]
    for i in range(n):
        v=rising(i);v.update(high=v['mark_high'],low=v['mark_low'],volume='100')
        if i==funding_at:v['funding_rate_per_interval']='10'
        rows.append(v)
    with path.open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    frozen=json.loads((root/'execution-freeze.json').read_text());frozen['input_sha256']=r.sha(path)
    (root/'execution-freeze.json').write_text(json.dumps(frozen))

class PipelineTest(unittest.TestCase):
    def test_applied_positive_updates_independently_audited(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);cp,c,path=fixture(root,700);positive_input(root,path,700)
            with patch.object(r,'RUN',root),patch.object(r,'CONTRACT',cp),patch('builtins.print'),\
                 patch.object(r.subprocess,'Popen',SyntheticSignalProcess):
                r.execute();result=json.loads((root/'result.json').read_text())
                self.assertGreaterEqual(result['controller_updates'],1)
                audited=a.audit(root,path,c['input']['development_start'])
                self.assertEqual(audited['verified_updates'],result['controller_updates'])
                self.assertGreater(audited['paired_uplift'],0)
                self.assertEqual(result['candidate_status'],'NO_QUALIFIED_CANDIDATE')

    def test_open_funding_stop_no_new_fill_no_learning(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);cp,c,path=fixture(root,700);positive_input(root,path,700,funding_at=102)
            with patch.object(r,'RUN',root),patch.object(r,'CONTRACT',cp),patch('builtins.print'),\
                 patch.object(r.subprocess,'Popen',SyntheticSignalProcess):
                r.execute();result=json.loads((root/'result.json').read_text())
                self.assertEqual(result['stop_reason'],'REFERENCE_RISK_STOP')
                self.assertFalse(result['terminal_flat'])
                records=[json.loads(x) for x in (root/'trace.jsonl').read_text().splitlines()]
                self.assertEqual(records[-2]['boundary'],'OPEN_FUNDING')
                self.assertNotIn('candidate_liquidation',records[-2])
                self.assertIsNone(records[-2]['signal'])
                for k in r.ARMS:self.assertEqual(records[-2]['wallets'][k]['fills'],records[-3]['wallets'][k]['fills'])
                a.audit(root,path,c['input']['development_start'])

    def test_whole_runner_decimal_and_single_attempt(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);cp,c,path=fixture(root)
            with patch.object(r,'RUN',root),patch.object(r,'CONTRACT',cp),patch('builtins.print'):
                r.execute();result=json.loads((root/'result.json').read_text())
                audited=a.audit(root,path,c['input']['development_start'])
                self.assertEqual(audited['actual_books'],6);self.assertGreater(audited['verified_windows'],0)
                self.assertEqual(result['confirmation_runs'],0)
                self.assertTrue(result['terminal_flat'])
                self.assertTrue(all(w['qty']==0 for w in result['wallets'].values()))
                with self.assertRaises(FileExistsError):r.execute()
                self.assertEqual(result,json.loads((root/'result.json').read_text()))

    def test_fixed_short_end_and_audit_detects_tampered_receipt(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);cp,c,path=fixture(root,230)
            with patch.object(r,'RUN',root),patch.object(r,'CONTRACT',cp),patch('builtins.print'):
                r.execute();result=json.loads((root/'result.json').read_text())
                self.assertEqual(result['stop_reason'],'COMPLETE')
                self.assertEqual(result['windows'],[])
                a.audit(root,path,c['input']['development_start'])
                trace=root/'trace.jsonl';records=[json.loads(x) for x in trace.read_text().splitlines()]
                records[101]['receipts']['adaptive']+=1
                trace.write_text(''.join(json.dumps(x)+'\n' for x in records))
                result['trace_sha256']=r.sha(trace);(root/'result.json').write_text(json.dumps(result))
                with self.assertRaisesRegex(ValueError,'DECIMAL_MISMATCH:receipt'):
                    a.audit(root,path,c['input']['development_start'])

    def test_contract_rejects_time_confirmation_and_extra_attempt(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);cp,c,path=fixture(root,5)
            with patch.object(r,'RUN',root),patch.object(r,'CONTRACT',cp):
                for field,value,reason in [('maximum_market_attempts',2,'BATCH_LIMIT'),
                    ('new_network_requests',1,'BATCH_LIMIT'),
                    ('confirmation',dict(interval='seen-history',maximum_runs=1,learning=False),'CONFIRMATION'),
                    ('started_utc',(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=9)).isoformat(),'EIGHT_HOUR')]:
                    changed=dict(c);changed[field]=value;cp.write_text(json.dumps(changed))
                    with self.assertRaisesRegex(ValueError,reason):r.contract()

    def test_identity_failure_before_market_attempt(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);cp,c,path=fixture(root,5)
            cp.write_text(json.dumps(c,indent=4))
            with patch.object(r,'RUN',root),patch.object(r,'CONTRACT',cp):
                with self.assertRaisesRegex(ValueError,'IDENTITY_CHANGED'):r.execute()
                self.assertFalse((root/'market-attempt.json').exists())

if __name__=='__main__':unittest.main()
