"""Independent Decimal policy/event/WAL audit of stored mock execution evidence.

No strategy fitting, market access, native replay or qualification authority.
"""
import csv
from decimal import Decimal as D, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR
import math
from pathlib import Path
import statistics

from audit_bounded_learning import Book
from audit_offline_policy_correction import read_domain, audit_action, sign
from cross_asset_inputs import need, sha
from frozen_history_transfer import concentration
from run_bounded_learning import hac_lower

MS=300000
MONEY=D('0.0000005')

def close(a,b,name):
    tolerance=D('2e-12') if name in ('qty','dd') else MONEY
    need(abs(D(str(a))-D(b))<=tolerance,'NATIVE_MISMATCH:'+name+':'+str(a)+':'+str(b))

def quantity(x):
    return (x*1000).to_integral_value(rounding=ROUND_DOWN)/1000

def fill_price(price,side,cost):
    p=price*(1+side*D(cost)*D('.0001'))
    return (p*10).to_integral_value(rounding=ROUND_CEILING if side>0 else ROUND_FLOOR)/10

class AuditBook(Book):
    def __init__(self,stress,delay):
        super().__init__(stress)
        self.delay=delay; self.intent=D(0); self.due=D(0); self.goal=D(0)
        self.stopped=False; self.orders=0

    def latch(self):
        self.stopped=True; self.intent=self.due=self.goal=D(0)

    def risk(self):
        if self.dd>=D('.08'):self.latch()

    def bar(self,features,price):
        decision=D(0) if self.stopped else audit_action(features,(0,-4),sign(self.intent))
        due=self.intent if self.delay else decision
        if self.stopped:due=D(0)
        if due!=self.due:self.goal=quantity(due/price)
        self.intent=decision; self.due=due

    def funding_event(self,mark,rate):
        self.fund(mark,rate); self.risk()

    def execute(self,price,mark,exiting=False):
        for _ in range(8):
            self.observe(mark,mark); self.risk()
            cap=quantity(D(2500)/max(price,mark))
            if abs(self.goal)>cap:self.goal=cap*sign(self.goal)
            desired=D(0) if self.stopped else self.goal
            # Existing engine closes before flipping, with a per-order cap.
            target=D(0) if desired*self.q<0 else desired
            delta=target-self.q
            amount=quantity(min(abs(delta),D(2500)/price))
            if amount<D('.001'):return
            side=sign(delta); px=fill_price(price,side,self.m); self.orders+=1
            for q in (amount*D('.6'),amount*D('.4')):
                fee=q*px*self.m*D('.00055')
                self.cash-=side*q*px+fee; self.q+=side*q
                self.fees+=fee;self.slip+=q*side*(px-price);self.turnover+=q*px;self.fills+=1
                self.observe(mark,mark);self.risk()
            if self.stopped and not exiting:return
        raise ValueError('AUDIT_PER_OPEN_ORDER_BUDGET')

    def liquidation(self,price):
        if not self.q:return self.cash
        px=fill_price(price,-sign(self.q),self.m)
        return self.cash+self.q*px-abs(self.q)*px*self.m*D('.00055')

    def snapshot(self,price,mark):
        return dict(intent=self.intent,due=self.due,qty=self.q,cash=self.cash,equity=self.value(mark),
            fees=self.fees,funding=self.funding,peak=self.peak,dd=self.dd,slippage=self.slip,
            turnover=self.turnover,fills=self.fills,orders=self.orders,liquidation=self.liquidation(price),
            stopped=int(self.stopped))

def audit_wal(directory,expected,delay):
    """Rebuild money and actual-holder funding from actual ordered fill receipts."""
    directory=Path(directory);orders={};fills={};per_order={}
    for line in (directory/'orders.wal').read_text().splitlines():
        f=line.split('\t')
        if f[0]=='INTENT2':
            need(len(f)==16 and f[1] not in orders,'WAL_ORDER_SCHEMA')
            qty,price=D(f[8]),D(f[9]);side=int(f[7])
            need(qty>0 and qty*price<=2500+MONEY,'WAL_ORDER_CAP')
            close(qty,qty.quantize(D('.001')),'qty')
            orders[f[1]]=dict(qty=qty,price=price,side=side);per_order[f[1]]=D(0)
        else:
            need(f[0]=='FILL3' and len(f)==9 and f[1] not in fills and f[2] in orders,'WAL_FILL_SCHEMA')
            order=orders[f[2]]; q,p,fee=map(D,f[5:8]); side=int(f[4])
            need(q>0 and side==order['side'],'WAL_FILL_SIDE')
            close(p,fill_price(order['price'],side,expected.m),'actual_fill_price')
            close(fee,q*p*expected.m*D('.00055'),'actual_fill_fee')
            per_order[f[2]]+=q
            fills[f[1]]=(side,q,p,fee)
    for key,order in orders.items():close(per_order[key],order['qty'],'qty')
    cash=D(10000);qty=fee_sum=funding=D(0);used=set();times=set()
    lines=(directory/'intent.events').read_text().splitlines()
    need(lines[0]=='V 3 '+str(delay),'JOURNAL_CONFIGURATION')
    for line in lines[1:]:
        f=line.split()
        if f[0]=='E':
            need(len(f)==2 and f[1] in fills and f[1] not in used,'ORDERED_FILL_RECEIPT')
            used.add(f[1]);side,q,p,fee=fills[f[1]]
            qty+=side*q;cash-=side*q*p+fee;fee_sum+=fee
        elif f[0]=='U':
            need(len(f)==4 and f[1] not in times,'ORDERED_FUNDING_RECEIPT');times.add(f[1])
            paid=qty*D(f[2])*D(f[3]);funding+=paid;cash-=paid
        else:need(f[0] in ('B','M','H','C','R','O'),'UNKNOWN_JOURNAL_EVENT')
    need(used==set(fills),'WAL_WITHOUT_EVENT')
    close(qty,expected.q,'qty');close(cash,expected.cash,'journal_cash')
    close(fee_sum,expected.fees,'journal_fees');close(funding,expected.funding,'journal_funding')
    need(len(orders)==expected.orders and len(fills)==expected.fills,'WAL_COUNTS')
    return dict(orders=len(orders),fills=len(fills),funding_events=len(times),
                wal_sha256=sha(directory/'orders.wal'),events_sha256=sha(directory/'intent.events'))

def audit_pair(root,name,delay,domain):
    books=[AuditBook(False,delay),AuditBook(True,delay)]
    handles=[(Path(root)/(name+'_'+suffix+'.csv')).open() for suffix in ('base','stress')]
    readers=[csv.DictReader(f) for f in handles]
    rows=0;restarts=0;pending=flat=False;active=0;stop_at=exit_at=None
    weekly=[];week_start=[D(10000)]*2;exposure=[0,0,0];events=[]
    def expect(k,kind,row,index,price,mark):
        nonlocal rows
        rec=next(readers[k],None)
        need(rec is not None and rec['event']==kind and int(rec['ts'])==int(row['timestamp']) and
             int(rec['index'])==index,'EVENT_CLOCK:'+name+':'+kind+':'+str(index))
        for field,value in books[k].snapshot(price,mark).items():
            if field in ('fills','orders','stopped','intent','due'):
                need(D(rec[field])==value,'EVENT_DISCRETE:'+name+':'+kind+':'+field+':'+str(index))
            else:close(rec[field],value,name+':'+kind+':'+field if field not in ('qty','dd') else field)
        rows+=1
    def latch():
        for b in books:b.latch()
    try:
        for index,sample in enumerate(domain,1):
            row=sample['row'];ts=int(row['timestamp'])
            price,mark,mc,closing,rate=(D(row[k]) for k in
                ('open','mark_open','mark_close','price','funding_rate_per_interval'))
            if flat:
                for k in (0,1):expect(k,'FLAT',row,index,closing,mc)
            elif pending:
                for k,b in enumerate(books):
                    b.funding_event(mark,rate);expect(k,'EXIT_FUND',row,index,price,mark)
                    b.execute(price,mark,True);need(b.q==0,'AUDIT_EXIT_RESIDUAL')
                    expect(k,'EXIT',row,index,price,mark)
                pending=False;flat=True;exit_at=ts
            else:
                active+=1
                for k,b in enumerate(books):
                    b.bar(sample['features'],price);b.funding_event(mark,rate)
                    expect(k,'OPEN',row,index,price,mark)
                opening=any(b.stopped for b in books)
                if opening:latch();pending=True
                for k,b in enumerate(books):
                    if not opening:b.execute(price,mark)
                    expect(k,'EXEC',row,index,price,mark)
                if any(b.stopped for b in books):latch();pending=True
                for k,b in enumerate(books):
                    if not opening:b.observe(D(row['mark_high']),D(row['mark_low']));b.risk()
                    expect(k,'RANGE',row,index,price,mark)
                if any(b.stopped for b in books):latch();pending=True
                for k,b in enumerate(books):
                    if not opening:b.observe(mc,mc);b.risk()
                    expect(k,'CLOSE',row,index,closing,mc)
                    if index%8640==0:
                        expect(k,'RECOVER',row,index,closing,mc);restarts+=1
                if pending and stop_at is None:stop_at=ts+MS
            exposure[sign(books[0].q)+1]+=1
            wallets=[dict(qty=float(b.q),cash=float(b.cash),fills=b.fills) for b in books]
            events.append(dict(kind='BAR',timestamp=ts,wallets=wallets))
            if index%2016==0:
                values=[b.liquidation(closing) for b in books]
                weekly.append([float(v-w) for v,w in zip(values,week_start)]);week_start=values
        for k,b in enumerate(books):
            b.latch()
            if not flat:b.execute(closing,mc,True)
            expect(k,'TERMINAL',row,len(domain),closing,mc)
            need(b.q==0 and next(readers[k],None) is None,'TERMINAL_OR_EXTRA_EVENTS')
        if not flat:exit_at=int(row['timestamp'])+MS
        if len(domain)%2016==0:
            for k,b in enumerate(books):weekly[-1][k]+=float(b.cash-week_start[k])
        wallets=[dict(qty=float(b.q),cash=float(b.cash),fills=b.fills) for b in books]
        summary=dict(wallets=wallets,weekly=weekly,exit_at_ms=exit_at,exposure_short_flat_long=exposure)
        events.append(dict(kind='TERMINAL',result=summary));groups=concentration(events,summary)
        wal=[audit_wal(Path(root)/(name+'_'+suffix),b,delay) for suffix,b in zip(('base','stress'),books)]
        economics=[]
        for k,b in enumerate(books):
            capital=10000.;returns=[]
            for w in weekly:returns.append(w[k]/capital);capital+=w[k]
            sharpe=(statistics.mean(returns)/statistics.stdev(returns)*math.sqrt(52)
                    if len(returns)>1 and statistics.stdev(returns)>0 else None)
            economics.append(dict(net=float(b.cash-10000),return_pct=float((b.cash/10000-1)*100),
                max_drawdown_pct=float(b.dd*100),fees=float(b.fees),funding=float(b.funding),
                slippage=float(b.slip),turnover=float(b.turnover),orders=b.orders,fills=b.fills,
                annualized_weekly_sharpe_descriptive=sharpe))
        risk=any(b.dd>=D('.08') for b in books)
        return dict(reason='REFERENCE_RISK_STOP' if risk else 'COMPLETE',economic_status=
            'REJECT_NATIVE_RISK' if risk else 'REJECT_NATIVE_NET' if min(b.cash for b in books)<=10000
            else 'NATIVE_CLOSED_LOOP_DEVELOPMENT_SUPPORTED',economics=economics,groups=groups,
            weekly=weekly,pressure_hac_lower_descriptive=hac_lower([w[1] for w in weekly]),
            active_bars=active,domain_bars=len(domain),event_rows=rows,bridge_restarts=restarts,
            terminal_flat=True,stop_at_ms=stop_at,exit_at_ms=exit_at,wal=wal)
    finally:
        for f in handles:f.close()

def audit(root,csv_path,start,end):
    domain=read_domain(csv_path,start,end)
    return {name:audit_pair(root,name,delay,domain) for name,delay in (('normal',0),('delay',1))}


if __name__=='__main__':
    import argparse
    import json
    parser=argparse.ArgumentParser(description='Read-only audit of an existing native four-book run')
    parser.add_argument('run_directory');parser.add_argument('input_csv')
    parser.add_argument('start_ms',type=int);parser.add_argument('end_ms',type=int)
    args=parser.parse_args()
    print(json.dumps(audit(args.run_directory,args.input_csv,args.start_ms,args.end_ms),allow_nan=False))
