"""Local synthetic BotApplication execution receipts -> unchanged LearningWindow.

Not installed in trade_bot. No config, credentials, market loader or promotion.
COPY deterministically replays a book's actual event prefix into a new app/WAL.
Portfolio paired differences identify policy changes, not per-current-bucket PnL.
"""
from collections import defaultdict
import copy
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
from types import SimpleNamespace

from execution_net_learning import LearningWindow, target
from native_execution_reward import NativeLedger

TOL = D('0.00000005')
ZERO = D(0)

def near(actual, expected):
    value=D(str(actual))
    if not value.is_finite() or abs(value-expected)>=TOL:
        raise ValueError(('RECEIPT_ACCOUNTING',actual,str(expected)))


class DecimalRuntimeBook:
    """Cash-flow accounting independent of C++ account and exit estimator."""
    def __init__(self,multiplier):
        self.multiplier=multiplier
        self.cash=D(10000); self.fees=self.funding=self.realized=ZERO
        self.qty=defaultdict(lambda:ZERO); self.avg=defaultdict(lambda:ZERO)
        self.marks=defaultdict(lambda:ZERO); self.quotes=defaultdict(lambda:ZERO)
        self.previous=None; self.seq=0; self.clock=0; self.fills=set(); self.origins={}
        self.last=None

    def accept(self,r):
        if not r['valid'] or r['seq']!=self.seq or r['clock']<self.clock:
            raise ValueError('RECEIPT_CONTINUITY')
        if self.seq==0 and (r['event']!=0 or r['reward'] is not None):
            raise ValueError('BASELINE')
        self.clock=r['clock']
        if r['market']:
            m=r['market']; s=m['symbol']; self.quotes[s]=D(str(m['price']))
            self.marks[s]=D(str(m['mark'])) or self.quotes[s]
        if r['event']==6:
            o=r['origin']
            if not o or o['id'] in self.origins: raise ValueError('ORDER_ORIGIN')
            self.origins[o['id']]=copy.deepcopy(o)
        if r['fill']:
            f=r['fill']; s=f['symbol']; identity=f['id']
            if identity in self.fills or r['origin']!=self.origins.get(f['order']):
                raise ValueError('FILL_ORIGIN_OR_DUPLICATE')
            self.fills.add(identity)
            size,price,fee=(D(str(f[k])) for k in ('qty','price','fee'))
            if size<=0 or price<=0 or f['side'] not in (-1,1): raise ValueError('FILL_INPUT')
            near(fee,size*price*D('.00055')*self.multiplier)
            signed=size*f['side']; old=self.qty[s]; new=old+signed
            if abs(new)<D('1e-9'): new=ZERO
            self.cash-=signed*price+fee; self.fees+=fee
            if old*signed>=0: self.avg[s]=(abs(old)*self.avg[s]+size*price)/abs(new)
            else:
                self.realized+=min(size,abs(old))*(price-self.avg[s])*(1 if old>0 else -1)
                if new==0: self.avg[s]=ZERO
                elif new*old<0: self.avg[s]=price
            self.qty[s]=new; self.marks[s]=price
        if r['event']==3:
            s=r['funding_symbol']; rate=D(str(r['funding_rate']))
            paid=self.qty[s]*self.marks[s]*rate if abs(rate)>D('1e-9') else ZERO
            near(r['funding_paid'],paid); self.cash-=paid; self.funding+=paid
        equity=self.cash+sum(q*self.marks[s] for s,q in self.qty.items())
        margin=self.cash+sum(q*self.avg[s] for s,q in self.qty.items())
        liquidation=self.cash; positions={p['symbol']:p for p in r['positions']}
        if set(positions)!={s for s,q in self.qty.items() if abs(q)>D('1e-9')}:
            raise ValueError('POSITION_COVERAGE')
        for s,p in positions.items():
            q=self.qty[s]; side=-1 if q>0 else 1
            ticks=self.quotes[s]*(1+D(side)*D('.0001')*self.multiplier)/D('.1')
            price=(ticks-D(side)*D('1e-9')).to_integral_value(
                rounding=ROUND_CEILING if side>0 else ROUND_FLOOR)*D('.1')
            fee=abs(q)*price*D('.00055')*self.multiplier
            for k,v in dict(qty=q,average=self.avg[s],mark=self.marks[s],quote=self.quotes[s],
                            exit_price=price,exit_fee=fee).items(): near(p[k],v)
            liquidation+=q*price-fee
        for k,v in dict(cash=margin,equity=equity,liquidation=liquidation,
                        exit_reserve=equity-liquidation,fees=self.fees,
                        funding=self.funding,realized=self.realized).items(): near(r[k],v)
        if self.previous is not None: near(r['reward'],liquidation-self.previous)
        self.previous=liquidation; self.seq+=1; self.last=r


class RuntimeSession:
    def __init__(self,driver,tape=None):
        self.driver=str(Path(driver).resolve(strict=True)); self.tape=tape
        self.errors=tempfile.TemporaryFile(mode='w+t'); self.audit_count=0; self.serial=0
        self.books={}; self.responses={}; self.chain='0'*64; self.closed=False
        self.process=subprocess.Popen([self.driver,'--synthetic-only'],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=self.errors,text=True,bufsize=1)
        self.lines=queue.Queue()
        def read():
            try:
                for line in self.process.stdout: self.lines.put(line)
            finally: self.lines.put(None)
        self.reader=threading.Thread(target=read,daemon=True); self.reader.start()
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def close(self):
        if self.closed: return
        self.closed=True
        try:
            if self.process.poll() is None: self.process.stdin.close()
            try: self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(); raise
            self.reader.join(timeout=1); self.process.stdout.close()
            self.errors.seek(0); text=self.errors.read()
            if self.tape: self.tape.write(json.dumps(dict(stderr=text,returncode=self.process.returncode))+'\n')
            if self.process.returncode or '[ERROR]' in text:
                raise RuntimeError('RUNTIME_DRIVER_FAILED '+text[-4000:])
        finally: self.errors.close()
    def identity(self):
        self.serial+=1; return 'b'+str(self.serial)
    def command(self,line):
        self.process.stdin.write(line+'\n'); self.process.stdin.flush()
        while True:
            raw=self.lines.get(timeout=20)
            if raw is None:
                self.errors.seek(0); raise RuntimeError('RUNTIME_REPLY_MISSING '+self.errors.read()[-4000:])
            if '[ERROR]' in raw: raise RuntimeError(raw)
            if raw.startswith('RUNTIME_REPLY '): break
        payload=raw[len('RUNTIME_REPLY '):]
        self.chain=hashlib.sha256((self.chain+line+payload).encode()).hexdigest()
        if self.tape: self.tape.write(json.dumps(dict(command=line,response=json.loads(payload)))+'\n')
        return json.loads(payload)
    def accept(self,key,response):
        for r in response['receipts']:
            self.books[key].accept(r); self.audit_count+=1
        self.responses[key]=response
        return response
    def new(self,multiplier=1,legacy=False,manual=False):
        key=self.identity(); self.books[key]=DecimalRuntimeBook(multiplier)
        self.accept(key,self.command('NEW {} {} {} {}'.format(key,multiplier,int(legacy),int(manual))))
        return key
    def clone(self,key):
        dest=self.identity(); self.books[dest]=DecimalRuntimeBook(self.books[key].multiplier)
        self.accept(dest,self.command('COPY {} {}'.format(dest,key)))
        # Identical prefix must reconstruct everything observed, not just balances.
        if self.books[dest].__dict__!=self.books[key].__dict__:
            raise ValueError('PREFIX_REPLAY_DIVERGENCE')
        for field in ('qty','drawdown','hard_lock','manual_lock','pending_late','fills','orders','legacy_weight'):
            if self.responses[dest][field]!=self.responses[key][field]: raise ValueError('PREFIX_STATE')
        return dest
    def event(self,key,command): return self.accept(key,self.command('DO '+key+' '+command))
    def check(self,key): return self.accept(key,self.command('CHECK '+key))


class RuntimeLedger:
    def __init__(self,session,multiplier=1,legacy=False,manual=False):
        self.session=session; self.multiplier=multiplier; self.legacy=legacy
        self.book=session.new(multiplier,legacy,manual)
        self.last_liquidation=10000.; self.weight=.5; self.bucket=1; self.version=0
        self.signal=dict(trend=0.,defensive=0.); self.mode=1
    @property
    def state(self): return self.session.responses[self.book]
    @property
    def wallet(self): return SimpleNamespace(drawdown=self.state['drawdown'])
    @property
    def liquidation(self): return float(self.session.books[self.book].previous)
    def copy(self):
        child=object.__new__(RuntimeLedger); child.__dict__=dict(self.__dict__)
        child.book=self.session.clone(self.book); return child
    def step(self,row,desired):
        NativeLedger.validate_row(row)
        if not math.isfinite(desired) or abs(desired)>2500: raise ValueError('TARGET_CAP')
        args=[row[k] for k in ('timestamp','open','price','mark_open','mark_close','mark_high','mark_low',
                              'funding_rate_per_interval')]
        args += [desired,self.bucket,self.version,self.weight,self.signal['trend'],self.signal['defensive'],self.mode]
        self.session.event(self.book,'STEP '+' '.join(str(v) for v in args))
        if self.legacy:
            observations=[r for r in self.state['receipts'] if r['event']==7]
            if len(observations)!=1 or observations[0]['clock']!=row['timestamp']:
                raise ValueError('LEGACY_SIGNAL_PRICE_CLOCK_MISMATCH')
        # Use the audited stream's liquidation, not virtual score or current bucket attribution.
        reward=self.liquidation-self.last_liquidation; self.last_liquidation=self.liquidation
        return reward
    def settle(self):
        self.session.event(self.book,'SETTLE'); self.last_liquidation=self.liquidation
        return self.liquidation
    def decision(self,signal,weights):
        self.bucket=signal['bucket']; self.weight=weights[self.bucket]; self.signal=dict(signal)
        return target(signal,weights)


class RuntimeLearningWindow(LearningWindow):
    """Only routing changes; grid, selection, holdout and acceptance stay inherited."""
    def step(self,row,signal):
        NativeLedger.validate_row(row)
        if signal is None or type(signal.get('bucket')) is not int or signal['bucket'] not in (0,1,2):
            raise ValueError('DECISION_BUCKET')
        for vector in self.vectors.values(): target(signal,vector)
        for key,books in self.books.items():
            for book in books: book.decision(signal,self.vectors[key])
        return super().step(row,signal)
