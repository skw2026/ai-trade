"""Independent Decimal books, causal OLS/signals, clocks and risk for frozen traces.

This audits already consumed paths; it never searches or starts another experiment.
"""
from collections import defaultdict, deque
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math
from statistics import NormalDist

D = lambda v: Decimal(str(v))
ZERO = D(0)


def check(ok, reason):
    if not ok:
        raise ValueError(reason)


def near(a,b,tolerance='0.000001'):
    return abs(D(a)-D(b)) <= D(tolerance)


def independent_ols(hours):
    """Decimal covariance, using independently constructed causal log prices."""
    x=[D(math.log(v[1])) for v in hours]; y=[D(math.log(v[0])) for v in hours]
    n=D(len(x)); sx=sum(x); sy=sum(y)
    den=sum(v*v for v in x)-sx*sx/n
    if den<=D('1e-18'):
        return None
    beta=(sum(a*b for a,b in zip(x,y))-sx*sy/n)/den
    alpha=(sy-beta*sx)/n
    sigma=(sum((b-alpha-beta*a)**2 for a,b in zip(x,y))/n).sqrt()
    if not D('.25')<=beta<=4 or sigma<D('.000001'):
        return None
    return tuple(map(float,(alpha,beta,sigma)))


def zscore(model,prices):
    a,b,s=model
    return (math.log(prices[0])-a-b*math.log(prices[1]))/s


def independent_models(archive,start,processed_minutes,cache):
    # Cache only hours already used by the trace being audited, never future fits.
    for minute in range(0,processed_minutes,1440):
        key=(start,minute)
        if key not in cache:
            if minute<1440*60:
                hours=list(archive.warmup[minute//60:])
                begin=0
            else:
                hours=[];begin=minute-1440*60
            for m in range(begin,minute,60):
                rows=archive.bars(start+(m+59)*60000,'trade')
                hours.append(tuple(b[3] for b in rows))
            check(len(hours)==1440,'INDEPENDENT_CAUSAL_WINDOW')
            cache[key]=independent_ols(hours)
    return {m:cache[start,m] for m in range(0,processed_minutes,1440)}


def audit_path(archive,snapshot,index,cache):
    data=snapshot['paths'][index]; stress=bool(index); delay=snapshot['extra_delay']
    events=data['events']; groups=defaultdict(list); last=-1
    for event in events:
        check(type(event['minute']) is int and last<=event['minute']<=snapshot['minute'],'EVENT_ORDER')
        last=event['minute'];groups[last].append(event)
    models=independent_models(archive,snapshot['start'],snapshot['minute'],cache)
    check(len(snapshot['models'])==len(models),'OLS_COUNT')
    for row in snapshot['models']:
        expected=models[row['minute']]; actual=row['model']
        check((expected is None)==(actual is None),'OLS_ELIGIBILITY')
        if expected is not None:
            check(all(near(a,b,'0.000000001') for a,b in zip(expected,actual)),'INDEPENDENT_OLS')
    cash=D(10000);qty=[ZERO,ZERO];fee_total=ZERO;funding_total=ZERO
    slip_total=ZERO;gross_cash=ZERO;turnover=ZERO
    leg_cash=[ZERO,ZERO];collateral=[ZERO,ZERO];peak=D(10000);dd=ZERO
    fee=D('.0011' if stress else '.00055');slip=D('.0004' if stress else '.0002')
    step=[D('.01'),D('.1')];tick=D('.01');pending=deque()
    exiting=False;deadline=None;held_model=None;entry_minute=None;entry_sign=0
    last_flat=None;stopped=False;insufficient=False;episode_full=False;completed=0
    cycle=None;cycles=[];failed_entries=0;fills=0;decision_count=0
    last_sample=D(10000);window=ZERO;window_count=0;negative=positive=0;alert=False
    first_legacy=None;checks=[];five=[];daily=[[0,10000.]]
    active_weeks=set();position_minutes=0;notional_minutes=ZERO
    stop_events=[];hard_detected=[];last_close=D(10000)
    maximum=(snapshot['end']-snapshot['start'])//60000

    def liquid(prices):
        return cash+sum(q*D(p) for q,p in zip(qty,prices))-sum(abs(q)*(D(p)*(fee+slip)+tick) for q,p in zip(qty,prices))

    def marked_risk(mark):
        nonlocal peak,dd
        good=[b[1] if q>=0 else b[2] for q,b in zip(qty,mark)]
        bad=[b[2] if q>=0 else b[1] for q,b in zip(qty,mark)]
        peak=max(peak,liquid(good));dd=max(dd,(peak-liquid(bad))/peak)
        reasons=[]
        if dd>=D('.08'):reasons.append('RESEARCH_DRAWDOWN_8_PERCENT')
        if sum(abs(q)*D(max(b[0],b[1])) for q,b in zip(qty,mark))>D('5000.00000001'):
            reasons.append('GROSS_EXPOSURE_CAP')
        for i,q in enumerate(qty):
            if q:
                cushion=collateral[i]+leg_cash[i]+q*D(bad[i])
                if cushion<=0:reasons.append('REFERENCE_ISOLATED_COLLATERAL_EXHAUSTED')
                if cushion/(abs(q)*D(bad[i]))<D('.08'):
                    reasons.append('REFERENCE_LIQUIDATION_DISTANCE_UNDER_8_PERCENT')
        return reasons

    for m in range(snapshot['minute']):
        t=snapshot['start']+m*60000; here=groups[m]
        rows=archive.bars(t,'trade');mark=archive.bars(t,'mark')
        previous=[b[3] for b in archive.bars(t-60000,'trade')]
        model=models[(m//1440)*1440]
        entries=[e for e in here if e['kind']=='entry_requested']
        regular_exits=[e for e in here if e['kind']=='exit_requested' and e['reason'] in ('TERMINAL_EXIT','RESIDUAL_OR_HORIZON_EXIT')]
        expected_entry=False;expected_exit=False
        if m%5==0 and not stopped:
            decision_count+=1
            if m>=maximum-5:
                expected_exit=(bool(any(qty)) or bool(pending)) and not exiting
            elif not exiting and not pending:
                if any(qty):
                    z=zscore(held_model,previous)
                    expected_exit=abs(z)<=.5 or z*entry_sign<=0 or m-entry_minute>=snapshot['parameters'][1]*60
                elif m<maximum-10 and model is not None and last_flat!=m:
                    expected_entry=abs(zscore(model,previous))>=snapshot['parameters'][0]
                    if expected_entry:
                        b=model[1]
                        qs=[(D(2500/(1+b)/previous[0])/step[0]).to_integral_value(rounding=ROUND_FLOOR)*step[0],
                            (D(2500*b/(1+b)/previous[1])/step[1]).to_integral_value(rounding=ROUND_FLOOR)*step[1]]
                        expected_entry=all(q>=s and q*D(px)>=5 for q,s,px in zip(qs,step,previous))
        check(bool(entries)==expected_entry and len(entries)<=1,'CAUSAL_ENTRY_OR_MISSING_SIGNAL')
        check(bool(regular_exits)==expected_exit and len(regular_exits)<=1,'CAUSAL_EXIT_OR_MISSING_SIGNAL')
        # Decisions precede funding and fills, while hard-risk reasons use end-of-minute OHLC.
        for event in entries:
            check(not any(qty) and not pending and cycle is None,'ENTRY_WHILE_OCCUPIED')
            check(all(near(a,b,'0.000000001') for a,b in zip(event['model'],model)),'ENTRY_MODEL')
            check(event['decision_prices']==previous and event['entry_delay']==delay,'ENTRY_INPUT_CLOCK')
            z=zscore(model,previous);check(near(z,event['z'],'0.00000001'),'SIGNAL_Z')
            direction=-1 if z>0 else 1
            expected_q=[direction*qs[0],-direction*qs[1]]
            check(all(near(q,v,'0.000000001') for q,v in zip(expected_q,event['quantities'])),'CAUSAL_QUANTITY')
            pending.extend([(m+1+delay,0,expected_q[0],'entry'),(m+2+delay,1,expected_q[1],'entry')])
            held_model=tuple(event['model']);entry_sign=1 if z>0 else -1;episode_full=False
            cycle=dict(start=m,cash=cash,fees=fee_total,funding=funding_total,slippage=slip_total)
        rates=archive.rates(t)
        fund_events=[e for e in here if e['kind']=='funding']
        expected_legs={i for i in rates if qty[i]}
        check({e['leg'] for e in fund_events}==expected_legs and len(fund_events)==len(expected_legs),'FUNDING_COMPLETENESS')
        for e in fund_events:
            i=e['leg'];check(near(e['quantity'],qty[i]) and e['rate']==rates[i] and e['mark']==mark[i][0],'FUNDING_SOURCE')
            amount=qty[i]*D(mark[i][0])*D(rates[i]);cash-=amount;leg_cash[i]-=amount;funding_total+=amount
            check(near(e['amount'],amount) and near(e['cash'],cash),'FUNDING_BOOK')
        previous_reasons=[] if stopped and not any(qty) and not pending else marked_risk(mark)
        expected_partial_exit=False
        for e in here:
            kind=e['kind']
            if kind in ('entry_requested','funding','performance_check'):continue
            if kind=='exit_requested':
                check(not exiting and e['deadline']==m+5,'EXIT_DEADLINE')
                if e['reason']=='PARTIAL_OR_FAILED_ENTRY':check(expected_partial_exit,'UNJUSTIFIED_PARTIAL_ABORT')
                exiting=True;deadline=m+5;pending.clear()
                legs=[i for i,q in enumerate(qty) if q]
                if legs:pending.append((m+1+delay,legs[0],None,'exit'))
            elif kind in ('fill','no_fill'):
                check(pending and pending[0][0]==m,'FILL_CLOCK_OR_MISSING_ORDER')
                due,leg,requested,phase=pending.popleft()
                check(e['leg']==leg and e['phase']==phase,'FILL_LEG_PHASE')
                if phase=='exit':requested=-qty[leg]
                check(near(requested,e['requested_delta'],'0.000000001'),'FILL_REQUESTED_QUANTITY')
                cap=(D(rows[leg][4])*D('.01')/step[leg]).to_integral_value(rounding=ROUND_FLOOR)*step[leg]
                quantity=min(abs(requested),cap)
                check((quantity==0)==(kind=='no_fill'),'FILL_CAPACITY')
                if quantity:
                    delta=quantity if requested>0 else -quantity
                    check(near(delta,e['delta'],'0.000000001'),'PARTIAL_FILL_QUANTITY')
                    raw=D(rows[leg][1 if delta>0 else 2]) if stress else D(rows[leg][0])
                    px=(raw*(1+slip if delta>0 else 1-slip)/tick).to_integral_value(rounding=ROUND_CEILING if delta>0 else ROUND_FLOOR)*tick
                    check(D(e['price'])==px and e['bar']==rows[leg],'EXECUTION_PRICE_SOURCE')
                    paid=abs(delta)*px*fee
                    check(near(e['fee'],paid),'FILL_FEE')
                    if not qty[leg]:collateral[leg]=abs(delta)*px
                    qty[leg]+=delta;cash-=delta*px+paid;leg_cash[leg]-=delta*px+paid
                    fee_total+=paid;turnover+=abs(delta)*px;fills+=1
                    gross_cash-=delta*D(rows[leg][0]);slip_total+=delta*(px-D(rows[leg][0]))
                    check(near(e['cash'],cash) and near(e['quantity_after'],qty[leg]),'DECIMAL_CASH_OR_POSITION')
                complete=quantity==abs(requested)
                if phase=='entry':
                    if entry_minute is None and qty[leg]:entry_minute=m
                    if not complete:
                        expected_partial_exit=True;failed_entries+=1
                    elif leg==1:episode_full=True
                elif any(qty):
                    remaining=leg if qty[leg] else next(i for i,q in enumerate(qty) if q)
                    pending.append((m+1,remaining,None,'exit'))
            elif kind=='flat':
                check(not any(qty) and near(e['cash'],cash),'FALSE_FLAT')
                check(e['completed_episode']==episode_full,'EPISODE_COMPLETION')
                if cycle is not None:
                    cycles.append(dict(start=cycle['start'],end=m,completed=episode_full,
                        net=float(cash-cycle['cash']),fees=float(fee_total-cycle['fees']),
                        funding=float(funding_total-cycle['funding']),slippage=float(slip_total-cycle['slippage'])))
                if episode_full:completed+=1
                cycle=None;episode_full=False;held_model=None;entry_minute=None;last_flat=m
                pending.clear();exiting=False;deadline=None;leg_cash=[ZERO,ZERO];collateral=[ZERO,ZERO]
            elif kind=='stop':
                check(not stopped,'STOP_REWRITTEN');stopped=True;stop_events.append(e)
            elif kind=='insufficient':
                check(e['reason']=='RESIDUAL_EXPOSURE_UNCLOSED' and exiting and m>deadline and any(qty),'FALSE_EXECUTION_INSUFFICIENCY')
                insufficient=True
            else:raise ValueError('UNKNOWN_TRACE_EVENT:'+kind)
        post_reasons=[] if stopped and not any(qty) and not pending else marked_risk(mark)
        reasons=previous_reasons+post_reasons
        if reasons:
            hard_detected.append((m,reasons))
            check(stopped,'MISSED_HARD_RISK')
        for e in stop_events[-1:]:
            if e['minute']==m and e['reason']!='PEER_PATH_REJECTED':
                check(e['reason'] in reasons,'UNJUSTIFIED_HARD_STOP')
        check(not pending or pending[0][0]>m or data['insufficient'],'MISSED_SCHEDULED_FILL')
        last_close=liquid([b[3] for b in mark]);peak=max(peak,last_close);dd=max(dd,(peak-last_close)/peak)
        if any(qty):
            active_weeks.add((t//60000+3*1440)//10080)
            position_minutes+=1;notional_minutes+=sum(abs(q)*D(b[3]) for q,b in zip(qty,mark))
        if (m+1)%5==0:
            if not stopped and not insufficient:
                window+=last_close-last_sample;window_count+=1
                if window_count==240:
                    negative=negative+1 if window<0 else 0
                    positive=positive+1 if window>0 else 0
                    if negative>=2:
                        alert=True
                        if first_legacy is None:first_legacy=m+1
                    reviewed=True if alert else None
                    if alert and reviewed and positive>=2:alert=False
                    checks.append(dict(minute=m+1,net=float(window),negative=negative,positive=positive,active=alert,review_passed=reviewed))
                    window=ZERO;window_count=0
            last_sample=last_close
            five.append([m+1,float(last_close)])
        if (m+1)%1440==0:daily.append([m+1,float(last_close)])
    check(near(cash,data['cash']) and all(near(q,v) for q,v in zip(qty,data['q'])),'FINAL_BOOK')
    for actual,expected in ((data['fees'],fee_total),(data['funding'],funding_total),
                            (data['traded'],turnover),(data['slippage'],slip_total),(data['gross_cash'],gross_cash)):
        check(near(actual,expected),'COST_DECOMPOSITION')
    check(near(dd,data['dd'],'0.000000001') and near(peak,data['peak']),'RISK_PEAK_AND_DRAWDOWN')
    check(completed==data['episodes'] and failed_entries==data['failed_entries'],'EPISODE_COUNTS')
    check(len(cycles)==len(data['cycles']),'CYCLE_COUNT')
    for a,b in zip(cycles,data['cycles']):
        check(all(a[k]==b[k] for k in ('start','end','completed')) and all(near(a[k],b[k]) for k in ('net','fees','funding','slippage')),'CYCLE_ATTRIBUTION')
    check(len(checks)==len(data['alert']['checks']) and first_legacy==data['alert']['first_legacy_withdrawal'],'ALERT_WINDOW_COUNT')
    for a,b in zip(checks,data['alert']['checks']):
        check(all(a[k]==b[k] for k in a if k!='net') and near(a['net'],b['net']),'INDEPENDENT_ALERT')
    check(active_weeks==set(snapshot['active_weeks'][index]) and position_minutes==snapshot['position_minutes'][index], 'ACTIVITY_COUNTS')
    check(near(notional_minutes,snapshot['notional_minutes'][index],'0.001'),'EXPOSURE_TIME_INTEGRAL')
    for computed,key in ((daily,'daily'),(five,'five')):
        check(len(computed)==len(snapshot[key]),'MARK_TRACE_LENGTH')
        for a,b in zip(computed,snapshot[key]):check(a[0]==b[0] and near(a[1],b[index+1]),'MARK_TRACE_VALUE')
    check(near(last_close,snapshot['last_liquid'][index]),'CHECKPOINT_LIQUIDATION')
    check(data['stopped']==(stop_events[0]['reason'] if stop_events else None),'STOP_STATE')
    check(data['exiting']==exiting and data['entry_minute']==entry_minute and
          data['last_flat_minute']==last_flat and data['complete']==episode_full,'CHECKPOINT_EXECUTION_STATE')
    check(len(data['queue'])==len(pending),'CHECKPOINT_PENDING_COUNT')
    for a,b in zip(data['queue'],pending):
        check(a[0:2]==list(b[0:2]) and a[3]==b[3] and
              ((a[2] is None and b[2] is None) or (a[2] is not None and b[2] is not None and near(a[2],b[2]))),'CHECKPOINT_PENDING_VALUE')
    check(near(data['audit_cash'],cash) and all(near(a,b) for a,b in zip(data['audit_q'],qty)),'REVIEW_BOOK_STATE')
    return dict(status='INDEPENDENT_DECIMAL_SIGNAL_EXECUTION_RISK_PASS',cash=str(cash),fees=str(fee_total),
        funding=str(funding_total),slippage=str(slip_total),gross_cash=str(gross_cash),positions=list(map(str,qty)),
        drawdown=str(dd),traded_notional=str(turnover),failed_entries=failed_entries,
        fills=fills,completed_episodes=completed,cycles=cycles,decision_checks=decision_count,
        causal_daily_models=len(models),alert_windows=len(checks),first_legacy_withdrawal=first_legacy,
        daily=daily,active_weeks=sorted(active_weeks),stop_events=stop_events,
        first_hard_risk=hard_detected[0] if hard_detected else None,terminal_liquidation=str(last_close))


def audit_snapshot(archive,snapshot,cache=None):
    if cache is None:cache={}
    audits=[audit_path(archive,snapshot,i,cache) for i in (0,1)]
    for i,a in enumerate(audits):
        if a['stop_events'] and a['stop_events'][0]['reason']=='PEER_PATH_REJECTED':
            peer=audits[1-i]
            check(peer['first_hard_risk'] is not None and peer['first_hard_risk'][0]<=a['stop_events'][0]['minute'],'UNBOUND_PEER_STOP')
    return audits


def bind_result(snapshot,audits,result):
    """A valid trace must not authorize a different summarized result."""
    for field in ('start','end','parameters','extra_delay','daily','active_weeks',
                  'position_minutes','notional_minutes','invalid_model_days','stop_minute'):
        check(result[field]==snapshot[field],'SUMMARY_BINDING:'+field)
    check(result['processed_minutes']==snapshot['minute'] and
          result['requested_minutes']==(snapshot['end']-snapshot['start'])//60000,'SUMMARY_COVERAGE')
    for i,name in enumerate(('base','stress')):
        p=result[name]; raw=snapshot['paths'][i]; audit=audits[i]
        for field in ('cash','fees','funding','slippage','gross_cash','traded_notional'):
            check(near(p[field],audit[field]),'SUMMARY_MONEY:'+field)
        check(near(p['maximum_drawdown'],audit['drawdown'],'0.000000001'),'SUMMARY_RISK')
        check(p['completed_episodes']==audit['completed_episodes'] and p['failed_entries']==audit['failed_entries'],'SUMMARY_COUNTS')
        check(p['positions']==raw['q'] and p['stop']==raw['stopped'] and p['insufficient']==raw['insufficient'],'SUMMARY_TERMINAL')
        check(p['cycles']==raw['cycles'] and p['performance_checks']==raw['alert']['checks'] and
              p['first_legacy_withdrawal']==audit['first_legacy_withdrawal'] and p['online_updates']==0,'SUMMARY_POLICY')
        check(near(result['last_liquidation'][i],audit['terminal_liquidation']),'SUMMARY_SCORE')


def independent_selection(results):
    eligible=[]
    for i,r in enumerate(results):
        if all(r[k]['stop'] is None and r[k]['insufficient'] is None for k in ('base','stress')):
            eligible.append((i,min(map(D,r['last_liquidation']))-10000,
                             sum(D(r[k]['traded_notional']) for k in ('base','stress'))))
    if not eligible:return None
    best=max(v[1] for v in eligible)
    tied=[v for v in eligible if best-v[1]<=D('.000001')]
    return sorted(tied,key=lambda v:(v[0]!=0,v[2],v[0]))[0][0]


def independent_verdict(candidate,baseline,latency,index):
    """Separate Decimal decision, not a call to the model's scoring function."""
    for r in (candidate,latency):
        if r['processed_minutes']!=r['requested_minutes'] or any(r[k]['insufficient'] or any(r[k]['positions']) for k in ('base','stress')):
            return 'TECHNICAL_OR_EXECUTION_INSUFFICIENT'
    if any(r[k]['stop'] for r in (candidate,latency) for k in ('base','stress')):return 'NO_GO_RISK'
    boundary=91*1440;end=366*1440; nets={};quarters={}; diversity=False
    for label,r in (('candidate',candidate),('baseline',baseline),('latency',latency)):
        d={v[0]:list(map(D,v[1:])) for v in r['daily']}
        required=[boundary+i*10080 for i in range(40)]+[182*1440,274*1440,end]
        if any(t not in d for t in required) or any(r[k]['insufficient'] for k in ('base','stress')):
            return 'TECHNICAL_OR_EXECUTION_INSUFFICIENT'
        nets[label]=[d[end][i]-d[boundary][i] for i in (0,1)]
        quarters[label]=[d[b][1]-d[a][1] for a,b in zip((boundary,182*1440,274*1440),(182*1440,274*1440,end))]
        if label=='candidate':
            for i,k in enumerate(('base','stress')):
                values=[d[boundary+(j+1)*10080][i]-d[boundary+j*10080][i] for j in range(39)]
                values.append(d[end][i]-d[boundary+39*10080][i])
                cycles=[D(c['net']) for c in r[k]['cycles'] if c['completed'] and c['start']>=boundary]
                first=(r['start']//60000+boundary+3*1440)//10080
                diversity|=len(cycles)<20 or sum(w>=first for w in r['active_weeks'][i])<8
                for series in (values,cycles):
                    positives=[v for v in series if v>0]
                    diversity|=not positives or max(positives)/sum(positives)>D('.35')
                if i==1:diversity|=nets[label][i]-max(values)<=0
    bad=(any(D(candidate[k]['cash'])<=10000 for k in ('base','stress')) or
         nets['candidate'][0]<=0 or nets['candidate'][1]<200 or
         (index!=0 and any(c-b<50 for c,b in zip(nets['candidate'],nets['baseline']))) or
         sum(v>0 for v in quarters['candidate'])<2 or min(quarters['candidate'])<-100 or
         any(D(latency[k]['cash'])<=10000 for k in ('base','stress')) or any(v<=0 for v in nets['latency']))
    return 'NO_GO_ECONOMIC' if bad else 'INSUFFICIENT_EVENT_DIVERSITY' if diversity else 'DEVELOPMENT_SUPPORTED'


def audit_report_math(decision,planning,results):
    """Verify reported magnitudes and conditional horizon, not just a status label."""
    for label in ('candidate','baseline','latency'):
        if label not in decision:continue
        r=results[label];d={v[0]:list(map(D,v[1:])) for v in r['daily']}
        for i,k in enumerate(('base','stress')):
            reported=decision[label][k];begin=91*1440;end=366*1440
            weekly=[d[begin+(j+1)*10080][i]-d[begin+j*10080][i] for j in range(39)]
            tail=d[end][i]-d[begin+39*10080][i]
            check(near(reported['net'],d[end][i]-d[begin][i]) and
                  near(reported['full_net'],D(r[k]['cash'])-10000),'REPORTED_ECONOMIC_NET')
            check(len(reported['weekly'])==39 and all(near(a,b) for a,b in zip(reported['weekly'],weekly)) and
                  near(reported['tail_net'],tail),'REPORTED_WEEK_AND_TAIL')
            qs=[d[b][i]-d[a][i] for a,b in zip((begin,182*1440,274*1440),(182*1440,274*1440,end))]
            check(all(near(a,b) for a,b in zip(qs,reported['quarter_net'])),'REPORTED_QUARTERS')
            cycles=[D(c['net']) for c in r[k]['cycles'] if c['completed'] and c['start']>=begin]
            first=(r['start']//60000+begin+3*1440)//10080
            check(reported['episodes']==len(cycles) and reported['active_weeks']==sum(w>=first for w in r['active_weeks'][i]),'REPORTED_CAPACITY')
            for series,field in ((cycles,'max_episode_positive_share'),(weekly+[tail],'max_week_positive_share')):
                positive=[v for v in series if v>0]
                expected=max(positive)/sum(positive) if positive else None
                check((expected is None and reported[field] is None) or
                      (expected is not None and reported[field] is not None and near(expected,reported[field],'0.000000001')),'REPORTED_CONCENTRATION')
            check(near(reported['stress_net_without_best_week'],d[end][i]-d[begin][i]-max(weekly+[tail])),'REPORTED_WEEK_REMOVAL')
    if decision['status']!='DEVELOPMENT_SUPPORTED':
        check(planning==dict(status='NOT_JUSTIFIED_BY_DEVELOPMENT',observation_started=False),'UNSUPPORTED_WAIT')
        return
    c=decision['candidate'];b=decision['baseline']
    series=[(k+'_absolute',c[k]['weekly'],D(200)*7/275) for k in ('base','stress')]
    if not decision['selected_is_baseline']:
        series += [(k+'_gain',[D(a)-D(v) for a,v in zip(c[k]['weekly'],b[k]['weekly'])],D(50)*7/275) for k in ('base','stress')]
    expected={};invalid=False
    for name,values,delta in series:
        data=list(map(D,values));n=len(data);mean=sum(data)/n;centered=[v-mean for v in data]
        for lag in (1,2,4):
            variance=sum(v*v for v in centered)/n
            for j in range(1,lag+1):
                variance+=2*(1-D(j)/(lag+1))*sum(centered[t]*centered[t-j] for t in range(j,n))/n
            if variance<=0:invalid=True;continue
            z=NormalDist().inv_cdf(.95)+NormalDist().inv_cdf(1-.2/len(series))
            for scale in (1,1.5,2):expected[name,lag,scale]=math.ceil((z*math.sqrt(float(variance))*scale/float(delta))**2)
    if invalid:
        check(planning['status']=='NO_AFFORDABLE_CONFIRMATION_EXIT' and planning['reason']=='INVALID_LONG_RUN_VARIANCE','INVALID_VARIANCE_PLANNING')
    else:
        actual={(c['series'],c['lag'],c['sd_multiplier']):c['weeks'] for c in planning['cases']}
        check(len(planning['cases'])==len(expected) and actual==expected,'CONDITIONAL_HORIZON_ARITHMETIC')
        weeks=max(expected.values());option=next((v for v in (8,13,26) if v>=weeks),None)
        check(planning['required_weeks']==weeks and planning['option_weeks']==option and
              planning['status']==('CONDITIONAL_PLANNING_ONLY' if option else 'NO_AFFORDABLE_CONFIRMATION_EXIT'),'CONDITIONAL_HORIZON_CHOICE')
    check(planning['observation_started'] is False,'UNAUTHORIZED_OBSERVATION')
