"""Independent Decimal cash, funding, fill and mark-risk reconstruction.

Consumes immutable market rows and exported events; never calls model simulation.
"""
from collections import defaultdict
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR

STEP=300000
DAY=86400000

def need(ok,why):
    if not ok:raise ValueError('AUDIT_'+why)

def close(x,y,why):
    need(abs(D(str(x))-D(str(y)))<=D('0.0000001'),'NUMERIC_'+why)

def audit(result,rows):
    by_time={r['timestamp']:r for r in rows}
    own_risks=[];summaries=[]
    for index,book in enumerate(result['books']):
        cash=D(10000);gross_cash=D(10000);q=D(0);fees=D(0);slip=D(0);fund=D(0);turn=D(0)
        collateral=D(0);leg_cash=D(0);peak=D(10000);dd=D(0);close_peak=D(10000);close_dd=D(0)
        events=defaultdict(list)
        for e in book['events']:events[e['t']].append(e)
        for e in book['events']:
            if e['kind']=='fill':
                need(result['start']<=e['t']<result['end'] and e['t']%STEP==0,'FILL_DOMAIN')
        daily={x['t']:x['equity'] for x in book['days']};first_risk=None;seen_days=0;fills=0
        last=D(10000);position_bars=0;entry_t=None;before_entry=None
        completed=[];entry_full=False
        for n,t in enumerate(range(result['start'],result['end'],STEP)):
            if n>=result['bars']:break
            row=by_time[t];rate=D(str(row['funding_rate_per_interval']))
            funding_events=[e for e in events[t] if e['kind']=='funding']
            expected=q*D(str(row['mark_open']))*rate
            need(len(funding_events)==int(bool(q and rate)),'FUNDING_EVENT_COUNT')
            if funding_events:
                e=funding_events[0];close(expected,e['amount'],'FUNDING')
                close(q,e['q'],'FUND_Q');close(rate,e['rate'],'FUND_RATE')
                cash-=expected;leg_cash-=expected;fund+=expected
            fs=[e for e in events[t] if e['kind']=='fill'];need(len(fs)<=1,'ONE_FILL_PER_BAR')
            for e in fs:
                dq=D(str(e['dq']));requested=D(str(e['requested']))
                cap=(D(str(row['volume']))*D('.01')/D('.001')).to_integral_value(rounding=ROUND_FLOOR)*D('.001')
                need(abs(dq)==min(abs(requested),cap) and dq!=0,'QUANTITY_CAP')
                need(dq*requested>0,'FILL_DIRECTION')
                raw=D(str(row['high' if dq>0 else 'low'] if index else row['open']))
                factor=D(1)+(D('.0004') if index else D('.0002'))*(1 if dq>0 else -1)
                px=(raw*factor/D('.1')).to_integral_value(rounding=ROUND_CEILING if dq>0 else ROUND_FLOOR)*D('.1')
                fee=abs(dq)*px*(D('.0011') if index else D('.00055'))
                cost=dq*(px-D(str(row['open'])))
                close(px,e['price'],'PRICE');close(fee,e['fee'],'FEE');close(cost,e['slippage'],'SLIP')
                if e['phase']=='entry':
                    need(q==0,'ENTRY_WHILE_POSITION')
                    requests=[v for v in events[t-result['latency']*STEP] if v['kind']=='entry_request']
                    need(len(requests)==1,'ENTRY_DECISION_LATENCY')
                    close(requests[0]['quantity'],requested,'REQUESTED_Q')
                    f=requests[0]['features'];need(f['latest_price_closed_at']==t-result['latency']*STEP,'CAUSAL_CLOSE')
                    size=(D(2500)/D(str(f['price']))/D('.001')).to_integral_value(rounding=ROUND_FLOOR)*D('.001')
                    # Boundary rounding is checked to the declared quantity step.
                    close(abs(requested),size,'DECISION_SIZE')
                    collateral=abs(dq)*px;leg_cash=D(0);entry_t=t;before_entry=cash
                    entry_full=abs(dq)==abs(requested)
                else:
                    need(q and requested==-q and q*dq<0,'EXIT_QUANTITY')
                    requests=[v for v in book['events'] if v['kind']=='exit_request' and
                              entry_t<=v['t']<=t-result['latency']*STEP]
                    need(requests,'EXIT_WITHOUT_REQUEST')
                    request=requests[-1]
                    need(t<=request['deadline'],'EXIT_AFTER_DEADLINE')
                    if request['reason']=='horizon':need(request['t']==entry_t+3*DAY,'HOLDING_PERIOD')
                cash-=dq*px+fee;leg_cash-=dq*px+fee;q+=dq
                gross_cash-=dq*D(str(row['open']));fees+=fee;slip+=cost;turn+=abs(dq)*px;fills+=1
                close(cash,e['cash_after'],'FILL_CASH');close(q,e['quantity_after'],'FILL_POSITION')
                if not q:
                    completed.append((entry_t,t,cash-before_entry,entry_full))
                    collateral=D(0);leg_cash=D(0);entry_t=None;before_entry=None
            adverse=D(str(row['mark_low'] if q>=0 else row['mark_high']))
            mark=D(str(row['mark_close']))
            low_eq=cash+q*adverse-abs(q*adverse)*D('.0015')
            last=cash+q*mark-abs(q*mark)*D('.0015')
            dd=max(dd,(peak-low_eq)/peak);peak=max(peak,last)
            close_peak=max(close_peak,last);close_dd=max(close_dd,(close_peak-last)/close_peak)
            reason=None
            if dd>=D('.08'):reason='RESEARCH_DRAWDOWN_8_PERCENT'
            elif abs(q)*D(str(row['mark_high']))>D(5000)+D('0.00000001'):reason='GROSS_CAP'
            elif q:
                margin=collateral+leg_cash+q*adverse-abs(q*adverse)*D('.0015')
                if margin<=0:reason='REFERENCE_COLLATERAL_EXHAUSTED'
                elif margin<D('.08')*abs(q*adverse):reason='REFERENCE_MARGIN_BUFFER'
            if reason and first_risk is None:first_risk=dict(t=t+STEP,reason=reason)
            position_bars+=int(bool(q))
            if (t+STEP)%DAY==0:
                need(t+STEP in daily,'DAILY_MISSING');seen_days+=1
                close(last,daily[t+STEP],'DAILY_EQUITY')
        for actual,reported,name in ((cash,book['cash'],'CASH'),(q,book['quantity'],'POSITION'),
            (last-10000,book['net'],'NET'),(gross_cash-10000,book['gross'],'GROSS'),
            (fees,book['fees'],'FEES'),(slip,book['slippage'],'SLIPPAGE'),
            (fund,book['funding'],'FUNDING'),(turn,book['turnover'],'TURNOVER'),
            (dd,book['drawdown'],'DRAWDOWN'),(close_dd,book['close_drawdown'],'CLOSE_DRAWDOWN')):
            close(actual,reported,name)
        need(seen_days==len(daily) and position_bars==book['holding_bars'],'OBSERVATION_COUNT')
        need(len(completed)==len(book['cycles']),'CYCLE_COUNT')
        for (a,b,net,complete),reported in zip(completed,book['cycles']):
            need((a,b,complete)==(reported['start'],reported['end'],reported['complete']),'CYCLE_IDENTITY')
            close(net,reported['net'],'CYCLE_NET')
        if not q:close(cash-10000,gross_cash-10000-fees-slip-fund,'COST_IDENTITY')
        own_risks.append(first_risk)
        summaries.append(dict(net=str(last-10000),cash=str(cash),fills=fills,
            gross=str(gross_cash-10000),fees=str(fees),slippage=str(slip),funding=str(fund),drawdown=str(dd)))
    earliest=min((v for v in own_risks if v),key=lambda x:x['t'],default=None)
    for b in result['books']:
        if result['initially_stopped']:
            need(b['stopped']==dict(t=result['start'],reason='TRAINING_HARD_STOP_RETAINED'),'INITIAL_STOP')
        else:
            need((b['stopped'] is None)==(earliest is None),'STOP_PRESENCE')
            if earliest:need(b['stopped']['t']==earliest['t'],'STOP_TIME')
    return dict(status='DECIMAL_LEDGER_AND_RISK_VERIFIED',books=summaries,bars=result['bars'])
