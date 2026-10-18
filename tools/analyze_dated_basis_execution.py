"""One causal displayed-book latency diagnostic, never fills or alpha returns."""
from collections import Counter
from decimal import Decimal as D, ROUND_FLOOR, localcontext
import json
from pathlib import Path

from dated_basis_known_depth import KnownDepth
from dated_basis_execution_bridge import Instrument, spot_requirement

DELAYS = (0, 250, 1000)
ORDERS = ('SPOT_FIRST', 'FUTURE_FIRST_PREFUNDED_ONLY')
ASSETS = ('USDT', 'BTC')
FACE = D(2500)


def bound(book, side, qty, t):
    r = book.bound(side, qty, t)
    if r['status'] != 'KNOWN_DISPLAYED_DEPTH_COVERS_REQUEST':
        return None, r['status']
    return r, None


def pair(books, spot, contracts, t, fee_asset):
    f, reason = bound(books['future'], 'bid', contracts, t)
    if reason: return None, 'FUTURE_'+reason
    sizing = spot_requirement(spot, FACE, f['price_bound'], fee_asset)
    s, reason = bound(books['spot'], 'ask', sizing['gross_btc'], t)
    if reason: return None, 'SPOT_'+reason
    if abs(s['event_ms']-f['event_ms']) > 500: return None, 'EVENT_SKEW'
    return dict(spot=s, future=f, sizing=sizing), None


def unwind(books, order, original, contracts, t, spot):
    if order == 'SPOT_FIRST':
        net = original['sizing']['net_btc']
        q = (net/spot.step()).to_integral_value(rounding=ROUND_FLOOR)*spot.step()
        try: spot.validate(q)
        except ValueError: return dict(status='UNRESOLVED',reason='UNWIND_LOT_SIZE')
        r, reason = bound(books['spot'], 'bid', q, t)
        if reason: return dict(status='UNRESOLVED', reason=reason)
        debit = original['sizing']['gross_btc']*D(original['spot']['price_bound'])
        if original['fee_asset'] == 'USDT': debit *= D('1.002')
        loss = debit-q*D(r['price_bound'])*D('.998')
    else:
        r, reason = bound(books['future'], 'ask', contracts, t)
        if reason: return dict(status='UNRESOLVED', reason=reason)
        entry = D(original['future']['price_bound']); exit_price = D(r['price_bound'])
        loss_btc = FACE*(1/entry-1/exit_price)+FACE*(1/entry+1/exit_price)*D('.001')
        loss = loss_btc*D(original['spot']['price_bound'])
    detail = dict(cash_deficit_before_dust_usdt=str(loss),dust_btc=str(net-q)) if order=='SPOT_FIRST' else dict(loss_usdt_equivalent=str(loss))
    return dict(status='DISPLAYED_UNWIND_ONLY',**detail,quote=r,full_order_qualified=False)


def evaluate(order, asset, original, late, contracts):
    q = original['sizing']; f0 = D(original['future']['price_bound']); s0 = D(original['spot']['price_bound'])
    if order == 'SPOT_FIRST':
        f = D(late['future']['price_bound']); s = s0
        added = FACE*D('1.001')*(1/f-1/f0)*s0
        chosen_spot, chosen_future = original['spot'], late['future']
    else:
        f = f0; s = D(late['spot']['price_bound'])
        added = q['gross_btc']*(s-s0)*(D('1.002') if asset == 'USDT' else 1)
        chosen_spot, chosen_future = late['spot'], original['future']
    residual = q['net_btc']-FACE/f
    return dict(status='DISPLAYED_BOTH_LEGS', spot=chosen_spot, future=chosen_future,
                gross_spot_btc=str(q['gross_btc']), net_spot_btc=str(q['net_btc']),
                target_initial_btc=str(q['target_btc']), rounding_excess_btc=str(q['rounding_excess_btc']),
                future_contracts=str(contracts), face_usd=str(FACE),
                added_delay_cost_usdt_equivalent=str(added),
                residual_before_future_fee_btc=str(residual),
                residual_after_future_fee_btc=str(residual-FACE/f*D('.001')),
                full_order_qualified=False, actual_execution_qualified=False,
                collateral_ready='UNVERIFIED', actual_fee_schedule='UNKNOWN')


def replay(rows, spot, future):
    if not rows: raise ValueError('EMPTY_CAPTURE')
    contracts = future.contracts(FACE)
    if future.face_usd != 100: raise ValueError('KNOWN_DEPTH_MULTIPLIER_MISMATCH')
    if any(rows[i]['mono_ms'] < rows[i-1]['mono_ms'] for i in range(1,len(rows))):
        raise ValueError('REVERSED_CAPTURE_CLOCK')
    tick_indices = [i for i,r in enumerate(rows) if r['kind'] == 'tick']
    ticks = [rows[i]['mono_ms'] for i in tick_indices]
    if len(set(ticks)) != len(ticks): raise ValueError('DUPLICATE_TICK')
    last = rows[-1]['mono_ms']
    events = [(r['mono_ms'],0,i,'frame',r) for i,r in enumerate(rows)]
    for i,t in enumerate(ticks):
        events.append((t,0,tick_indices[i]+.25,'start',i))
        for j,d in enumerate(DELAYS):
            events.append((t+d,0 if d==0 else 1,tick_indices[i]+.5 if d==0 else i*len(DELAYS)+j,'late',(i,d)))
    events.sort(key=lambda e:e[:3])
    books = {s:KnownDepth(s) for s in ('spot','future')}
    initial = {}; output = []; failure = None
    with localcontext() as ctx:
        ctx.prec=50
        for t,_,_,kind,data in events:
            if kind == 'frame':
                try:
                    if data['kind'] == 'snapshot': books[data['source']].snapshot(json.loads(data['payload']['raw']))
                    elif data['kind'] == 'update': books[data['source']].update(data)
                except (ValueError, KeyError, TypeError) as exc:
                    if failure is None: failure = str(exc)
                continue
            if kind == 'start':
                for asset in ASSETS:
                    p,reason = (None,'CAPTURE_TAINTED') if failure else pair(books,spot,contracts,t,asset)
                    if p: p['fee_asset']=asset
                    initial[data,asset]=(p,reason)
                continue
            i,d = data
            for order in ORDERS:
                for asset in ASSETS:
                    p,reason=initial[i,asset]
                    base=dict(tick=i,start_frame_index=tick_indices[i],start_ms=ticks[i],evaluation_ms=t,delay_ms=d,order=order,fee_asset=asset,
                              actual_execution_qualified=False, full_order_qualified=False)
                    if t > last:
                        outcome=dict(status='RIGHT_CENSORED', unwind=dict(status='UNRESOLVED'))
                    elif reason or failure:
                        outcome=dict(status='INITIAL_UNAVAILABLE' if reason else 'CAPTURE_TAINTED',reason=reason or failure)
                    else:
                        # Keep the original spot amount; no hindsight resizing.
                        f,s=p['future'],p['spot']
                        if order=='SPOT_FIRST': f,reject=bound(books['future'],'bid',contracts,t)
                        else: s,reject=bound(books['spot'],'ask',p['sizing']['gross_btc'],t)
                        if not reject and abs(s['event_ms']-f['event_ms'])>500: reject='EVENT_SKEW'
                        if reject:
                            outcome=dict(status='SECOND_LEG_UNAVAILABLE',reason=reject,
                                         unwind=unwind(books,order,p,contracts,t,spot))
                        else: outcome=evaluate(order,asset,p,dict(spot=s,future=f),contracts)
                    output.append({**base,**outcome})
    groups=[]
    for order in ORDERS:
        for asset in ASSETS:
            for d in DELAYS:
                selected=[r for r in output if r['order']==order and r['fee_asset']==asset and r['delay_ms']==d]
                complete=[r for r in selected if r['status']=='DISPLAYED_BOTH_LEGS']
                def limits(key):
                    values=[D(r[key]) for r in complete]
                    return dict(min=str(min(values)),max=str(max(values))) if values else None
                groups.append(dict(order=order,fee_asset=asset,delay_ms=d,registered=len(selected),
                                   statuses=dict(Counter(r['status'] for r in selected)),
                                   added_delay_cost=limits('added_delay_cost_usdt_equivalent'),
                                   residual_before_fee=limits('residual_before_future_fee_btc')))
    return dict(summary=dict(status='DISPLAYED_EXECUTION_SENSITIVITY_MEASURED' if failure is None and any(r['status']=='DISPLAYED_BOTH_LEGS' for r in output) else 'EXECUTION_EVIDENCE_INSUFFICIENT',
                             capture_failure=failure,ticks=len(ticks),cases=len(output),
                             statuses=dict(Counter(r['status'] for r in output)),actual_execution_qualified=False,
                             strategy_entries=0,confirmation_attempts=0,g4=False),groups=groups,rows=output,
                metadata_semantics=dict(spot_step=str(spot.step()),future_step=str(future.step()),
                                        face_per_contract=str(future.face_usd),contracts=str(contracts),
                                        dynamic_filters_verified=False,account_permission_verified=False,
                                        metadata_capture_time_binding='NOT_ATTESTED'))


def analyze(path, spot_meta, future_meta):
    rows=[json.loads(line) for line in Path(path).open()]
    return replay(rows, Instrument.metadata(spot_meta,'spot'), Instrument.metadata(future_meta,'future'))
