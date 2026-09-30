"""Offline bounded allocation learning from execution-ledger receipts.

No live adapter or promotion authority. Fixed signals and costs are not learned.
All candidates start with the same carried position, not a free empty portfolio.
"""
import copy
import math
import statistics
from run_bounded_learning import Wallet, need

GRID=(.4,.45,.5,.55,.6)
WINDOW=240
TRAIN=168
MINIMUM=120

def target(signal, weights):
    if signal is None:return 0.0
    w=weights[signal['bucket']]
    value=w*signal['trend']+(1-w)*signal['defensive']
    need(math.isfinite(value) and abs(value)<=2500+1e-8,'TARGET_CAP')
    return 0.0 if abs(value)<1e-8 else value

def t_stat(values):
    if len(values)<2:return 0.0
    sd=statistics.stdev(values)
    return statistics.mean(values)/(sd/math.sqrt(len(values))) if sd>1e-12 else 0.0

class ExecutionLedger:
    def __init__(self,multiplier=1):
        self.wallet=Wallet(multiplier)
        self.last_target=0.0
        self.last_liquidation=10000.0

    def copy(self):return copy.deepcopy(self)

    def liquidation(self, price, mark):
        closing=copy.deepcopy(self.wallet)
        closing.trade(0,price,mark)
        return closing.cash

    def step(self,row,desired,*,funded=False,halt=False):
        price,mark,close,mc,rate=(float(row[k]) for k in
            ('open','mark_open','price','mark_close','funding_rate_per_interval'))
        if not funded:self.wallet.fund(mark,rate)
        halt=halt or self.wallet.drawdown>=.08
        cap_reduce=abs(self.wallet.qty)*max(price,mark)>2500+1e-8
        if not halt and (cap_reduce or not math.isclose(desired,self.last_target,rel_tol=0,abs_tol=1e-8)):
            capped=math.copysign(min(abs(desired),2500*price/max(price,mark)),desired)
            self.wallet.trade(capped,price,mark)
            self.last_target=desired
        if not halt:self.wallet.observe(float(row['mark_high']),float(row['mark_low']))
        liquidation=self.liquidation(close,mc)
        receipt=liquidation-self.last_liquidation
        self.last_liquidation=liquidation
        return receipt

    def settle(self,row,at_open):
        price=float(row['open' if at_open else 'price'])
        mark=float(row['mark_open' if at_open else 'mark_close'])
        if at_open:self.wallet.fund(mark,float(row['funding_rate_per_interval']))
        self.wallet.trade(0,price,mark)
        self.last_target=0.0
        self.last_liquidation=self.wallet.cash
        return self.snapshot(mark)

    def snapshot(self,mark):
        return dict(**self.wallet.snapshot(mark),last_target=self.last_target,
                    net_liquidation_equity=self.last_liquidation)

class LearningWindow:
    def __init__(self,weights,base,stress,domain='development'):
        need(domain=='development','CONFIRMATION_LEARNING_FORBIDDEN')
        need(len(weights)==3 and all(any(abs(w-g)<1e-9 for g in GRID) for w in weights),'WEIGHT_GRID')
        self.weights=list(weights)
        self.books={'current':(base.copy(),stress.copy())}
        self.vectors={'current':list(weights)}
        for bucket in range(3):
            for i,weight in enumerate(GRID):
                vector=list(weights);vector[bucket]=weight
                key=f'{bucket}:{i}'
                self.vectors[key]=vector
                self.books[key]=(base.copy(),stress.copy())
        self.receipts={k:[[],[]] for k in self.books}
        self.counts=[[0,0] for _ in range(3)]
        self.steps=0;self.locked=None;self.train_locked=False

    def step(self,row,signal):
        need(self.steps<WINDOW,'WINDOW_CLOSED')
        phase=0 if self.steps<TRAIN else 1
        if signal is not None and abs(signal['trend'])+abs(signal['defensive'])>1e-8:
            self.counts[signal['bucket']][phase]+=1
        for key,books in self.books.items():
            desired=target(signal,self.vectors[key])
            for i,book in enumerate(books):
                self.receipts[key][i].append(book.step(row,desired))
        self.steps+=1
        if self.steps==TRAIN:
            self.lock_train(signal['bucket'] if signal else 1)

    def lock_train(self,preferred):
        need(self.steps==TRAIN and not self.train_locked,'TRAIN_LOCK_TIME')
        self.train_locked=True
        # Pure capacity bounds before reading a single holdout observation.
        possible=[k for k,(n,_) in enumerate(self.counts) if n>=10 and n+WINDOW-TRAIN>=MINIMUM]
        if not possible:return
        bucket=preferred if preferred in possible else max(possible,key=lambda k:self.counts[k][0])
        reachable=[f'{bucket}:{i}' for i,w in enumerate(GRID) if abs(w-self.weights[bucket])<=.05+1e-9]
        # Prefer no change on an exact tie. No holdout ranking or runner-up retry.
        self.locked=max(reachable,key=lambda k:(sum(self.receipts[k][0]),
                              -abs(self.vectors[k][bucket]-self.weights[bucket])))

    def finish(self):
        need(self.steps==WINDOW and self.train_locked,'WINDOW_INCOMPLETE')
        scores={key:dict(train=sum(v[0][:TRAIN]),holdout=sum(v[0][TRAIN:]),
                        stress_train=sum(v[1][:TRAIN]),stress_holdout=sum(v[1][TRAIN:]))
                for key,v in self.receipts.items()}
        result=dict(counts=copy.deepcopy(self.counts),locked=self.locked,scores=scores,
                    weights_before=list(self.weights),weights_after=list(self.weights),updated=False,
                    reason='CAPACITY_INSUFFICIENT')
        if self.locked is None:return result
        bucket=int(self.locked.split(':')[0]);train,hold=self.counts[bucket]
        if train+hold<MINIMUM or min(train,hold)<10:return result
        if any(b.wallet.drawdown>=.08 for b in self.books[self.locked]):
            result['reason']='CANDIDATE_REFERENCE_RISK';return result
        selected=self.receipts[self.locked]
        holdout=selected[0][TRAIN:]
        difference=[x-y for x,y in zip(holdout,self.receipts['current'][0][TRAIN:])]
        score=scores[self.locked]
        result.update(net_t=t_stat(holdout),paired_t=t_stat(difference))
        if min(score.values())<=0:
            result['reason']='NO_ABSOLUTE_NET_EDGE';return result
        if result['net_t']<1.5 or result['paired_t']<1.5 or sum(difference)<=0:
            result['reason']='NET_OR_PAIRED_EVIDENCE_INSUFFICIENT';return result
        if abs(self.vectors[self.locked][bucket]-self.weights[bucket])<1e-9:
            result['reason']='UNCHANGED';return result
        result.update(updated=True,reason='EXECUTION_NET_UPDATE',weights_after=self.vectors[self.locked])
        return result

class Safety:
    def __init__(self):
        self.loss_streak=[0,0,0,0];self.net=[0.,0.,0.,0.];self.count=0;self.withdrawn=False

    def observe(self,receipt,bucket):
        need(not self.withdrawn,'SAFETY_LATCHED')
        self.net[bucket]+=receipt;self.net[3]+=receipt;self.count+=1

    def assess(self):
        need(self.count==WINDOW,'SAFETY_WINDOW')
        net=list(self.net)
        for k,pnl in enumerate(net):
            self.loss_streak[k]=self.loss_streak[k]+1 if pnl<0 else 0
        self.withdrawn=any(n>=2 for n in self.loss_streak)
        self.net=[0.,0.,0.,0.];self.count=0
        return dict(net_by_decision_bucket_and_total=net,streak=list(self.loss_streak),withdrawn=self.withdrawn)
