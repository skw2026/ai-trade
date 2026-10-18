"""Independent raw sequence, case disposition, unwind and Decimal audit."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
from pathlib import Path


def verify(path,result):
    raw=[json.loads(line) for line in Path(path).open()]
    tick_indices=[i for i,r in enumerate(raw) if r['kind']=='tick']
    ticks=[raw[i]['mono_ms'] for i in tick_indices]
    expected_ids={(i,d,o,a) for i in range(len(ticks)) for d in (0,250,1000)
                  for o in ('SPOT_FIRST','FUTURE_FIRST_PREFUNDED_ONLY') for a in ('USDT','BTC')}
    ids=[(r['tick'],r['delay_ms'],r['order'],r['fee_asset']) for r in result['rows']]
    assert len(ids)==len(set(ids)) and set(ids)==expected_ids
    assert result['summary']['statuses']==dict(Counter(r['status'] for r in result['rows']))
    assert result['summary']['ticks']==len(ticks) and result['summary']['cases']==len(ids)
    assert not result['summary']['g4'] and not result['summary']['actual_execution_qualified']
    assert result['summary']['strategy_entries']==result['summary']['confirmation_attempts']==0
    assert result['metadata_semantics']['contracts']=='25'
    assert D(result['metadata_semantics']['spot_step'])==D('.00001')
    assert D(result['metadata_semantics']['face_per_contract'])==100
    assert not result['metadata_semantics']['dynamic_filters_verified']
    assert not result['metadata_semantics']['account_permission_verified']
    assert result['metadata_semantics']['metadata_capture_time_binding']=='NOT_ATTESTED'
    assert result['summary']['capture_failure'] is None
    assert result['summary']['status']=='DISPLAYED_EXECUTION_SENSITIVITY_MEASURED'
    assert all(raw[i]['mono_ms']>=raw[i-1]['mono_ms'] for i in range(1,len(raw)))
    boundaries={(raw[-1]['mono_ms'],len(raw))}
    for r in result['rows']:
        assert not r['actual_execution_qualified'] and not r['full_order_qualified']
        i=r['tick'];assert r['start_ms']==ticks[i] and r['start_frame_index']==tick_indices[i]
        assert r['evaluation_ms']==ticks[i]+r['delay_ms']
        boundaries.add((ticks[i],tick_indices[i]))
        if r['delay_ms'] and r['evaluation_ms']<=raw[-1]['mono_ms']:
            boundaries.add((r['evaluation_ms'],len(raw)))
    books={k:None for k in ('spot','future')};buffered={k:[] for k in books};contexts={}
    def levels(rows):
        out={}
        for p,q in rows:
            p,q=D(p),D(q)
            assert p.is_finite() and q.is_finite() and p>0 and q>=0 and p not in out
            out[p]=q
        return out
    def update(k,row):
        if books[k] is None:buffered[k].append(row);return
        b=books[k];e=json.loads(row['payload']['raw']);assert e['u']>=e['U']
        if e['u']<b['last'] or k=='spot' and e['u']==b['last']:return
        if not b['bridged']:assert e['U']<=b['last']+(k=='spot')<=e['u']
        elif k=='future':assert e['pu']==b['last']
        else:assert e['U']<=b['last']+1
        if b['bridged']:assert e['E']>=b['event']
        for side,key in (('bid','b'),('ask','a')):
            for p,q in levels(e[key]).items():
                if q:b[side][p]=q
                else:b[side].pop(p,None)
        assert b['bid'] and b['ask'] and max(b['bid'])<min(b['ask'])
        b.update(last=e['u'],bridged=True,event=e['E'],received=row['mono_ms'])
    index=0
    for t,cut in sorted(boundaries):
        while index<len(raw) and (raw[index]['mono_ms']<t or raw[index]['mono_ms']==t and index<=cut):
            row=raw[index];index+=1;k=row['source']
            if row['kind']=='snapshot':
                assert books[k] is None;data=json.loads(row['payload']['raw'])
                books[k]=dict(bid=levels(data['bids']),ask=levels(data['asks']),last=data['lastUpdateId'],bridged=False)
                for pending in buffered[k]:update(k,pending)
                buffered[k]=[]
            elif row['kind']=='update':update(k,row)
        contexts[t,cut]=deepcopy(books)
    def quote(context,k,side,qty,t):
        b=context[k]
        if b is None or not b['bridged']:return None,'UNBRIDGED'
        if not 0<=t-b['received']<=1000:return None,'INVALID_OR_STALE_LOCAL_CLOCK'
        remaining=qty;value=D(0)
        for p in sorted(b[side],reverse=side=='bid'):
            take=min(remaining,b[side][p]);remaining-=take
            value+=take*100/p if k=='future' else take*p
            if not remaining:break
        if remaining:return None,'KNOWN_LEVELS_INSUFFICIENT'
        return dict(price=qty*100/value if k=='future' else value/qty,event=b['event']),None
    def same(got,want):
        assert want is not None and got['event_ms']==want['event']
        assert abs(D(got['price_bound'])-want['price'])<D('1e-35')
    verified=arithmetic=unwinds=0
    with localcontext() as ctx:
        ctx.prec=50;step=D(result['metadata_semantics']['spot_step'])
        for r in result['rows']:
            t=r['start_ms'];late=r['evaluation_ms'];asset=r['fee_asset'];order=r['order']
            initial=contexts[t,r['start_frame_index']]
            f0,reason=quote(initial,'future','bid',D(25),t)
            initial_reason='FUTURE_'+reason if reason else None
            if not reason:
                target=D(2500)/f0['price']
                gross=((target/(D('.998') if asset=='BTC' else 1))/step).to_integral_value(rounding=ROUND_CEILING)*step
                net=gross*(D('.998') if asset=='BTC' else 1)
                s0,reason=quote(initial,'spot','ask',gross,t)
                initial_reason='SPOT_'+reason if reason else None
                if not reason and abs(s0['event']-f0['event'])>500:initial_reason='EVENT_SKEW'
            if late>raw[-1]['mono_ms']:
                assert r['status']=='RIGHT_CENSORED' and r['unwind']['status']=='UNRESOLVED';continue
            if initial_reason:
                assert r['status']=='INITIAL_UNAVAILABLE' and r['reason']==initial_reason;continue
            current=initial if not r['delay_ms'] else contexts[late,len(raw)]
            if order=='SPOT_FIRST':
                f,reason=quote(current,'future','bid',D(25),late);s=s0
            else:
                s,reason=quote(current,'spot','ask',gross,late);f=f0
            if not reason and abs(s['event']-f['event'])>500:reason='EVENT_SKEW'
            if reason:
                assert r['status']=='SECOND_LEG_UNAVAILABLE' and r['reason']==reason
                u=r['unwind']
                if order=='SPOT_FIRST':
                    qty=(net/step).to_integral_value(rounding=ROUND_FLOOR)*step
                    q,why=quote(current,'spot','bid',qty,late)
                else:q,why=quote(current,'future','ask',D(25),late)
                if why:assert u['status']=='UNRESOLVED' and u['reason']==why
                else:
                    assert u['status']=='DISPLAYED_UNWIND_ONLY' and not u['full_order_qualified'];same(u['quote'],q)
                    if order=='SPOT_FIRST':
                        loss=gross*s0['price']*(D('1.002') if asset=='USDT' else 1)-qty*q['price']*D('.998')
                        assert abs(D(u['cash_deficit_before_dust_usdt'])-loss)<D('1e-35')
                        assert D(u['dust_btc'])==net-qty
                    else:
                        loss=D(2500)*(D('1.001')/f0['price']-D('.999')/q['price'])*s0['price']
                        assert abs(D(u['loss_usdt_equivalent'])-loss)<D('1e-35')
                    unwinds+=1
                continue
            assert r['status']=='DISPLAYED_BOTH_LEGS'
            same(r['spot'],s);same(r['future'],f);verified+=2
            expected={'target_initial_btc':target,'gross_spot_btc':gross,'net_spot_btc':net,
                      'rounding_excess_btc':net-target,'future_contracts':D(25),'face_usd':D(2500),
                      'residual_before_future_fee_btc':net-D(2500)/f['price'],
                      'residual_after_future_fee_btc':net-D('2502.5')/f['price'],
                      'added_delay_cost_usdt_equivalent':D('2502.5')*s0['price']*(1/f['price']-1/f0['price']) if order=='SPOT_FIRST' else gross*(s['price']-s0['price'])*(D('1.002') if asset=='USDT' else 1)}
            for k,v in expected.items():assert abs(v-D(r[k]))<D('1e-35'),(r['tick'],k)
            assert not r['actual_execution_qualified'] and not r['full_order_qualified'];arithmetic+=1
    assert arithmetic>0
    group_ids=[(g['order'],g['fee_asset'],g['delay_ms']) for g in result['groups']]
    assert len(set(group_ids))==len(group_ids)==12
    for g in result['groups']:
        rows=[r for r in result['rows'] if (r['order'],r['fee_asset'],r['delay_ms'])==(g['order'],g['fee_asset'],g['delay_ms'])]
        assert g['registered']==len(rows)==len(ticks)
        assert g['statuses']==dict(Counter(r['status'] for r in rows))
        for name,key in (('added_delay_cost','added_delay_cost_usdt_equivalent'),('residual_before_fee','residual_before_future_fee_btc')):
            values=[D(r[key]) for r in rows if r['status']=='DISPLAYED_BOTH_LEGS']
            assert g[name]==(dict(min=str(min(values)),max=str(max(values))) if values else None)
    return dict(status='RAW_BOOK_DISPOSITIONS_AND_ARITHMETIC_MATCH',displayed_leg_bounds=verified,
                complete_cases=arithmetic,displayed_unwinds=unwinds,registered_cases=len(ids),
                strategy_trades=0,g4=False)
