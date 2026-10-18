"""L1 publication/engine-clock diagnostic. Never calculates returns or orders."""
import json
from collections import Counter
from decimal import Decimal as D, ROUND_CEILING, ROUND_FLOOR
import math
import re

EXPIRIES = tuple('BTCUSDT-'+s for s in ('23OCT26','30OCT26','06NOV26','27NOV26',
                                       '25DEC26','26MAR27','25JUN27','24SEP27'))
SYMBOLS = ('BTCUSDT',)+EXPIRIES


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def positive(value):
    require(isinstance(value,str) and re.fullmatch(r'\d+(?:\.\d+)?',value,flags=re.ASCII),
            'DECIMAL_STRING_REQUIRED')
    d=D(value)
    require(d.is_finite() and d>0 and math.isfinite(float(value)) and float(value)>0,
            'POSITIVE_FINITE_REQUIRED')
    return d


def parse(row):
    message=json.loads(row['payload']['raw'])
    require(isinstance(message,dict),'MESSAGE_OBJECT_REQUIRED')
    if message.get('op') in ('ping','pong','subscribe'):
        return message, None
    data=message['data']; symbol=data['s']
    expected='spot' if symbol=='BTCUSDT' else 'linear'
    require(symbol in SYMBOLS and row['source']==expected and
            message['topic']=='orderbook.1.'+symbol and message['type']=='snapshot', 'IDENTITY')
    for value in (message['ts'],message['cts'],data['u'],data['seq']):
        require(type(value) is int and 0<value<=2**53-1,'CLOCK_SEQUENCE')
    sides=[]
    for side in (data['b'],data['a']):
        require(isinstance(side,list) and len(side)<=1,'L1_SHAPE')
        if side:
            require(isinstance(side[0],list) and len(side[0])==2,'LEVEL_SHAPE')
            sides.append(tuple(map(positive,side[0])))
        else: sides.append(None)
    require(not all(sides) or float(sides[0][0])<float(sides[1][0]),'CROSSED')
    return message, dict(symbol=symbol,ts=message['ts'],cts=message['cts'],u=data['u'],seq=data['seq'],
                         bid=sides[0],ask=sides[1],mono=row['mono_ms'],wall=row['wall_ms'])


def transition(old,new):
    issues=[]
    if old is None: return issues
    if new['u']==1 and old['u']!=1: issues.append('SERVICE_RESET')
    elif new['u']<old['u']: issues.append('UPDATE_REGRESSION')
    if new['seq']<old['seq']: issues.append('SEQUENCE_REGRESSION')
    if new['ts']<old['ts'] or new['cts']<old['cts']: issues.append('CLOCK_REGRESSION')
    if new['u']==old['u'] and (new['bid'],new['ask'])!=(old['bid'],old['ask']):
        issues.append('SAME_UPDATE_CHANGED_CONTENT')
    return issues


def sizes(spot,future):
    if not spot['ask'] or not future['bid']: return False
    ask,available=spot['ask']
    quantity=(D(2500)/ask*1000).to_integral_value(rounding=ROUND_FLOOR)/1000
    if quantity<=0: return False
    gross=(quantity/D('.998')*1000000).to_integral_value(rounding=ROUND_CEILING)/1000000
    return available>=gross and future['bid'][1]>=quantity


def analyze(rows,receipt):
    require(receipt['fixed_duration_ms']==120000 and receipt['reconnects']==0,'CAPTURE_CONTRACT')
    start=receipt['start_wall_ms']; stop=receipt['observation_stop_ms']
    require(0<=stop<=120000,'STOP_TIME')
    last_mono=-1
    for row in rows:
        require(isinstance(row['mono_ms'],(int,float)) and not isinstance(row['mono_ms'],bool)
                and math.isfinite(row['mono_ms']) and last_mono<=row['mono_ms']<=120000,'RECEIVE_ORDER')
        last_mono=row['mono_ms']
    states={s:None for s in SYMBOLS}; counters={s:Counter() for s in SYMBOLS}
    latches={s:set() for s in SYMBOLS}; last_receive={s:0 for s in SYMBOLS}
    max_gap={s:0 for s in SYMBOLS}; clocks={s:[] for s in SYMBOLS}
    acknowledgements=set(); controls=Counter(); pairs=[]; anomalies=[]; index=0
    protocol_failure=None
    for second in range(1,121):
        tick=second*1000; wall=start+tick
        while protocol_failure is None and index<len(rows) and rows[index]['mono_ms']<=min(tick,stop):
            row=rows[index];index+=1
            if row['kind']!='message': continue
            try:
                message,new=parse(row)
                if new is None:
                    require(row['source'] in ('spot','linear'),'CONTROL_SOURCE')
                    if message.get('op')=='subscribe':
                        require(message.get('success') is True and
                                message.get('req_id') in (None,'','obs-'+row['source']),'ACK_FAILED')
                    else:
                        require(message.get('op')=='pong' or (message.get('op')=='ping' and
                                message.get('success') is True and message.get('ret_msg')=='pong'),'PONG_INVALID')
            except (ValueError,KeyError,TypeError,ArithmeticError) as error:
                protocol_failure=dict(mono_ms=row['mono_ms'],source=row['source'],
                                      reason=type(error).__name__)
                stop=min(stop,row['mono_ms'])
                anomalies.append(dict(symbol=None,mono_ms=row['mono_ms'],issues=['PROTOCOL_FAILURE']))
                break
            if new is None:
                if message.get('op')=='subscribe':
                    acknowledgements.add(row['source']);controls['acks']+=1
                else: controls['pongs']+=1
                continue
            symbol=new['symbol']; old=states[symbol]; c=counters[symbol];c['snapshots']+=1
            gap=new['mono']-last_receive[symbol]
            max_gap[symbol]=max(max_gap[symbol],gap);last_receive[symbol]=new['mono']
            clocks[symbol].append(new['wall']-new['ts'])
            issues=transition(old,new)
            if new['cts']>new['ts'] or new['wall']<new['ts']: issues.append('CLOCK_ORDER')
            if issues:
                latches[symbol].update(issues)
                anomalies.append(dict(symbol=symbol,mono_ms=row['mono_ms'],issues=issues))
            if old is not None:
                unchanged=(new['bid'],new['ask'])==(old['bid'],old['ask'])
                if not unchanged: c['content_changes']+=1
                if new['u']==old['u'] and unchanged:
                    c['same_update_same_content']+=1
                    if new['ts']>old['ts']:
                        c['republished_ts_advanced']+=1
                        if new['cts']==old['cts']:
                            c['republished_same_cts']+=1
                            if 0<=new['wall']-new['ts']<=1000 and new['wall']-new['cts']>1000:
                                c['fresh_publication_old_engine_clock']+=1
                        else: c['republished_changed_cts']+=1
            states[symbol]=new
        for symbol in EXPIRIES:
            s,f=states['BTCUSDT'],states[symbol]
            reasons=[]
            if tick>stop: reasons.append('CAPTURE_ENDED')
            if protocol_failure is not None: reasons.append('PROTOCOL_FAILURE')
            if not {'spot','linear'}<=acknowledgements: reasons.append('NOT_ACKNOWLEDGED')
            if s is None or f is None: reasons.append('MISSING_SNAPSHOT')
            if latches['BTCUSDT'] or latches[symbol]: reasons.append('CONTINUITY_ANOMALY')
            item=dict(second=second,symbol=symbol,reasons=reasons,legacy_condition=False,
                      recent_l1_report=False,target_l1_size_covered=False,qualified=False)
            if s is not None and f is not None:
                item['engine_skew_ms']=abs(s['cts']-f['cts'])
                item['receive_skew_ms']=abs(s['mono']-f['mono'])
                item['spot_cts_age_ms']=wall-s['cts'];item['future_cts_age_ms']=wall-f['cts']
                item['spot_publication_age_ms']=wall-s['ts'];item['future_publication_age_ms']=wall-f['ts']
                item['spot_receive_age_ms']=tick-s['mono'];item['future_receive_age_ms']=tick-f['mono']
                if not reasons:
                    legs=(s,f)
                    populated=all(b['bid'] and b['ask'] for b in legs)
                    item['legacy_condition']=bool(populated and all(0<=wall-b['cts']<=1000 and
                        0<=b['ts']-b['cts']<=1000 and 0<=b['wall']-b['cts']<=1000 for b in legs)
                        and item['engine_skew_ms']<=500 and item['receive_skew_ms']<=1000)
                    item['recent_l1_report']=bool(populated and all(0<=wall-b['ts']<=4000 and
                        0<=tick-b['mono']<=4000 for b in legs))
                    item['target_l1_size_covered']=bool(item['recent_l1_report'] and sizes(s,f))
            pairs.append(item)
    for symbol in SYMBOLS:
        # Entire fixed horizon, including startup and unobserved tail, stays visible.
        max_gap[symbol]=max(max_gap[symbol],120000-last_receive[symbol])
    summaries=[]
    for symbol in SYMBOLS:
        c=counters[symbol]
        summaries.append(dict(symbol=symbol,counts=dict(c),max_receive_gap_ms=max_gap[symbol],
             continuity_issues=sorted(latches[symbol]),
             publication_receive_lag_range_ms=[min(clocks[symbol]),max(clocks[symbol])] if clocks[symbol] else None))
    term_summary=[]
    for symbol in EXPIRIES:
        group=[r for r in pairs if r['symbol']==symbol]
        term_summary.append(dict(symbol=symbol,denominator=120,
              legacy_condition=sum(x['legacy_condition'] for x in group),
              recent_l1_report=sum(x['recent_l1_report'] for x in group),
              target_l1_size_covered=sum(x['target_l1_size_covered'] for x in group),
              missing_or_anomalous=sum(bool(x['reasons']) for x in group)))
    evidence=sum(counters[s]['fresh_publication_old_engine_clock'] for s in SYMBOLS)
    return dict(schema='linear_ws_observability_v1',pairs=pairs,symbol_summary=summaries,
        term_summary=term_summary,controls=dict(controls),anomalies=anomalies,denominator=960,
        old_clock_republication_evidence=evidence,
        hypothesis='OBSERVED_OLD_CTS_REPUBLICATION' if evidence else 'OLD_CTS_REPUBLICATION_NOT_OBSERVED',
        capture_status=receipt['status'],protocol_failure=protocol_failure,
        legacy_qualified_pairs=sum(x['legacy_condition'] for x in pairs),
        recent_l1_pairs=sum(x['recent_l1_report'] for x in pairs),
        l1_size_covered_pairs=sum(x['target_l1_size_covered'] for x in pairs),
        economic_computations=0,old_l2_revalidated=False,independent_events=0,qualified=False)
