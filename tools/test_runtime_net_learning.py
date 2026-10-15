"""Fixed synthetic acceptance, never market research or strategy qualification."""
import argparse
import copy
import gzip
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import unittest

from execution_net_learning import TRAIN, WINDOW, target
from runtime_net_learning import RuntimeLedger, RuntimeSession, RuntimeLearningWindow
from test_native_execution_reward import fixture, row, unit_target

DRIVER=None
TAPE=None
RESULTS={}
SESSIONS=[]

def session():
    s=RuntimeSession(DRIVER,TAPE); SESSIONS.append(s); return s

def advance(book,r,signal,weights):
    return book.step(r,book.decision(signal,weights))

def control(mode):
    with session() as s:
        actual=[RuntimeLedger(s,m) for m in (1,2)]
        legacy=[RuntimeLedger(s,m,legacy=True) for m in (1,2)]
        weights=[.5]*3; window=RuntimeLearningWindow(weights,*actual)
        units=None; locked=None; prefix=hashlib.sha256(); unit_gain=[0.,0.]
        for i in range(WINDOW):
            r,signal=fixture(i,mode)
            if i<TRAIN: prefix.update(json.dumps([r,signal],sort_keys=True).encode())
            window.step(r,signal)
            for j,b in enumerate(actual):
                advance(b,r,signal,weights); advance(legacy[j],r,signal,weights)
                shadow=window.books['current'][j]
                for k in ('qty','drawdown','fills','orders'):
                    assert abs(b.state[k]-shadow.state[k])<5e-8,(i,k)
                assert abs(b.liquidation-shadow.liquidation)<5e-8
                assert b.version==0
            if i==TRAIN-1:
                locked=window.locked; assert locked is not None
                selected=window.vectors[locked]
                units=[(b.copy(),b.copy()) for b in actual]
            elif i>=TRAIN:
                assert window.locked==locked
                for j,(a,b) in enumerate(units):
                    a.decision(signal,selected); b.decision(signal,weights)
                    unit_gain[j]+=a.step(r,unit_target(signal,selected))-b.step(r,unit_target(signal,weights))
        result=window.finish()
        classification=('DIRECTIONAL_EXECUTION_GAIN' if result['updated'] and min(unit_gain)>0 else
            'EXPOSURE_SCALING_ONLY' if result['updated'] and max(abs(x) for x in unit_gain)<1e-8 else
            'NO_ACCEPTED_UPDATE')
        if mode=='positive': assert result['updated'] and classification=='DIRECTIONAL_EXECUTION_GAIN',result
        if mode in ('cost','reverse'): assert not result['updated'],result
        if mode=='scale': assert result['updated'] and classification=='EXPOSURE_SCALING_ONLY',result
        current_boundary=[b.liquidation for b in actual]
        legacy_boundary=[b.liquidation for b in legacy]
        fixed=[b.copy() for b in actual]
        for b in actual: b.version=1 if result['updated'] else 0
        for i in range(WINDOW,2*WINDOW):
            r,signal=fixture(i,mode)
            for a,b,l in zip(actual,fixed,legacy):
                advance(a,r,signal,result['weights_after']); advance(b,r,signal,weights)
                advance(l,r,signal,weights)
        final=[b.settle() for b in actual]; fixed_final=[b.settle() for b in fixed]
        legacy_final=[b.settle() for b in legacy]
        gain=[a-b for a,b in zip(final,fixed_final)]
        if mode=='positive': assert min(gain)>0
        if not result['updated']: assert max(abs(x) for x in gain)<5e-8
        out=dict(mode=mode,classification=classification,result=result,unit_gain=unit_gain,
            train_input_sha256=prefix.hexdigest(),locked_at=TRAIN,applied_first_at=WINDOW+1 if result['updated'] else None,
            evaluation_steps=WINDOW,next_steps=WINDOW,synthetic_only=True,
            next_window_net=[x-y for x,y in zip(final,current_boundary)],
            next_window_vs_fixed=gain,
            legacy_next_window_net=[x-y for x,y in zip(legacy_final,legacy_boundary)],
            new_vs_legacy_total=[a-b for a,b in zip(final,legacy_final)],
            final_liquidation=final,legacy_final_liquidation=legacy_final,
            legacy_final_weights=[b.state['legacy_weight'] for b in legacy],
            legacy_updates=[b.state['legacy_updates'] for b in legacy],
            audited_receipts=s.audit_count,commands_sha256=s.chain,application_branches=len(s.books))
    return out


class RuntimeNetLearningTest(unittest.TestCase):
    def test_01_explicit_local_mode(self):
        self.assertEqual(subprocess.run([DRIVER],capture_output=True,timeout=5).returncode,2)

    def test_02_first_entry_close_and_real_wal(self):
        with session() as s:
            b=RuntimeLedger(s); before=b.liquidation
            reward=b.step(row(),1000)
            self.assertLess(reward,0); self.assertEqual(b.state['fills'],2)
            last=b.liquidation; b.settle()
            self.assertAlmostEqual(last,b.liquidation,places=8)
            self.assertAlmostEqual(reward,b.liquidation-before,places=8)
            self.assertEqual(b.state['qty'],0); self.assertEqual(b.state['fills'],4)
            s.check(b.book)

    def test_03_prefix_carries_pending_and_late_old_origin(self):
        with session() as s:
            b=RuntimeLedger(s); b.mode=2; b.version=3; b.bucket=2; b.weight=.6
            b.step(row(),1000); self.assertTrue(b.state['pending_late'])
            child=b.copy(); self.assertEqual(child.state['qty'],b.state['qty'])
            prior_seq=s.books[child.book].seq
            child.mode=1; child.version=4; child.bucket=0; child.weight=.4
            child.step(row(1,10010,10010,.0001),500)
            receipts=child.state['receipts']
            late=next(r for r in receipts if r['fill'])
            self.assertEqual(late['origin']['version'],'v3')
            self.assertEqual(late['origin']['bucket'],2)
            self.assertEqual(late['origin']['weight'],.6)
            self.assertGreaterEqual(late['seq'],prior_seq)
            self.assertGreater(late['clock'],1000000)
            self.assertTrue(b.state['pending_late']); self.assertFalse(child.state['pending_late'])
            self.assertGreater(s.books[child.book].funding,0)
            # 1000 / 10000 * 60% was filled BEFORE the new funding observation.
            self.assertAlmostEqual(float(s.books[child.book].funding),.06*10010*.0001,places=10)
            child.settle(); s.check(child.book)

    def test_04_no_fill_no_reward_but_old_virtual_moves(self):
        with session() as s:
            b=RuntimeLedger(s,legacy=True); b.mode=0
            for i in range(12):
                r=row(i,10000+i*10,10010+i*10)
                advance(b,r,dict(bucket=1,trend=1000,defensive=0),[.5]*3)
            self.assertEqual(b.state['fills'],0); self.assertEqual(b.liquidation,10000)
            self.assertGreater(b.state['legacy_virtual'],0)

    def test_05_manual_lock_and_copy(self):
        with session() as s:
            b=RuntimeLedger(s,manual=True); b.step(row(),1000)
            child=b.copy(); child.step(row(1),-1000)
            self.assertEqual(child.state['orders'],0); self.assertEqual(child.state['qty'],0)
            self.assertTrue(child.state['manual_lock'])

    def test_06_hard_lock_persists_across_copy_recovery(self):
        with session() as s:
            b=RuntimeLedger(s); b.step(row(0,10000,5000),2500)
            self.assertTrue(b.state['hard_lock']); child=b.copy()
            child.step(row(1,5000,10000),2500)
            self.assertEqual(child.state['qty'],0)
            child.step(row(2),2500); self.assertEqual(child.state['qty'],0)
            self.assertGreaterEqual(child.state['drawdown'],.08)

    def test_07_non_synthetic_and_nonfinite_rejected(self):
        with session() as s:
            b=RuntimeLedger(s); before=s.audit_count
            bad=row(); bad['_synthetic']=False
            with self.assertRaises(ValueError): b.step(bad,10)
            with self.assertRaises(ValueError): b.step(row(),float('nan'))
            self.assertEqual(s.audit_count,before)

    def test_08_invalid_window_inputs_do_not_advance(self):
        with session() as s:
            a,b=RuntimeLedger(s),RuntimeLedger(s,2)
            w=RuntimeLearningWindow([.5]*3,a,b); before=s.audit_count
            with self.assertRaises(ValueError): w.step(row(),dict(bucket=3,trend=1,defensive=0))
            with self.assertRaises(Exception): w.finish()
            self.assertEqual(s.audit_count,before); self.assertEqual(w.steps,0)

    def test_09_receipt_corruption_rejected(self):
        with session() as s:
            b=RuntimeLedger(s); before=copy.deepcopy(s.books[b.book]); b.step(row(),1000)
            records=b.state['receipts']
            for key in ('reward','fees','exit_reserve','seq','clock','valid','origin'):
                audit=copy.deepcopy(before); rows=copy.deepcopy(records)
                index=next(i for i,r in enumerate(rows) if r['fill'])
                if key=='valid': rows[index][key]=False
                elif key=='clock': rows[index][key]=-1
                elif key=='origin': rows[index][key]['version']='forged'
                else: rows[index][key]+=1
                with self.assertRaises((AssertionError,ValueError),msg=key):
                    for r in rows: audit.accept(r)

    def test_10_short_funding_and_stress(self):
        with session() as s:
            books=[RuntimeLedger(s,m) for m in (1,2)]
            for b in books:
                b.step(row(),-1000); b.step(row(1,9900,9900,.001),-1000)
                self.assertLess(s.books[b.book].funding,0); b.settle()
            self.assertGreater(books[0].liquidation,books[1].liquidation)

    def test_11_direction_control(self): RESULTS['positive']=control('positive')
    def test_12_cost_control(self): RESULTS['cost']=control('cost')
    def test_13_holdout_reversal_control(self):
        RESULTS['reverse']=control('reverse')
        self.assertEqual(RESULTS['positive']['train_input_sha256'],RESULTS['reverse']['train_input_sha256'])
        self.assertEqual(RESULTS['positive']['result']['locked'],RESULTS['reverse']['result']['locked'])
    def test_14_exposure_scaling_control(self): RESULTS['scale']=control('scale')


def main():
    global DRIVER,TAPE
    p=argparse.ArgumentParser(); p.add_argument('--driver',required=True); p.add_argument('--output')
    args=p.parse_args(); DRIVER=str(Path(args.driver).resolve(strict=True))
    path=None
    if args.output:
        path=Path(args.output)
        attempt=0; original=path
        while path.exists() or path.with_suffix('.jsonl.gz').exists():
            attempt+=1
            suffix='-retry' if attempt==1 else '-retry'+str(attempt)
            path=original.with_name(original.stem+suffix+original.suffix)
        TAPE=gzip.open(path.with_suffix('.jsonl.gz'),'xt',encoding='utf-8')
    try:
        result=unittest.TextTestRunner(verbosity=2,failfast=True).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(RuntimeNetLearningTest))
    finally:
        if TAPE: TAPE.close()
    summary=dict(status='RUNTIME_NET_LEARNING_SYNTHETIC_PASS' if result.wasSuccessful() else 'FAIL',
        tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped),
        controls=RESULTS,audited_receipts=sum(s.audit_count for s in SESSIONS),
        driver_sha256=hashlib.sha256(Path(DRIVER).read_bytes()).hexdigest(),
        learning_source_sha256=hashlib.sha256(Path(__file__).with_name('execution_net_learning.py').read_bytes()).hexdigest(),
        g4=False,synthetic_only=True,online_objective_changed=False)
    if path:
        summary['tape_sha256']=hashlib.sha256(path.with_suffix('.jsonl.gz').read_bytes()).hexdigest()
        with path.open('x') as f: json.dump(summary,f,indent=2,sort_keys=True); f.write('\n')
    print(json.dumps(summary,sort_keys=True))
    return 0 if result.wasSuccessful() else 1
if __name__=='__main__': sys.exit(main())
