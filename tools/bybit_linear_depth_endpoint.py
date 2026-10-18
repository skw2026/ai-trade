"""Fixed-endpoint depth reconstruction and unchanged conditional cash arithmetic."""
from decimal import Decimal as D, localcontext
from itertools import product
import json
import math
import re

from bybit_linear_basis_panel import SYMBOLS, COSTS, quantities, sweep, cash_case, metadata


def require(ok,reason):
    if not ok:raise ValueError(reason)


def validate_catalog(futures,spot,reference):
    selected=metadata(futures,SYMBOLS)
    for s,r in selected.items():
        for k in ('contractType','baseCoin','quoteCoin','settleCoin','status','deliveryTime',
                  'fundingInterval','deliveryFeeRate','lotSizeFilter'):
            require(r[k]==reference['instruments'][s][k],'FUTURE_METADATA_CHANGED:'+s+':'+k)
    require(type(spot.get('retCode')) is int and spot['retCode']==0,'SPOT_METADATA_ERROR')
    rows=[r for r in spot['result']['list'] if r.get('symbol')=='BTCUSDT']
    require(len(rows)==1,'SPOT_METADATA_IDENTITY')
    for k in ('symbol','status','baseCoin','quoteCoin','lotSizeFilter'):
        require(rows[0][k]==reference['spot'][k],'SPOT_METADATA_CHANGED:'+k)
    return selected


def used_levels(rows,quantity):
    remaining=quantity;out=[]
    for price,available in rows:
        used=min(remaining,available)
        out.append([str(price),str(used)]);remaining-=used
        if remaining==0:return out
    raise ValueError('DEPTH_INSUFFICIENT')


def levels(rows,snapshot,side):
    require(isinstance(rows,list) and len(rows)<=(50 if snapshot else 1000),'LEVEL_SCHEMA')
    out={};previous=None
    for item in rows:
        require(isinstance(item,list) and len(item)==2,'LEVEL_SCHEMA')
        for value in item:
            require(isinstance(value,str) and re.fullmatch(r'\d+(?:\.\d+)?',value,flags=re.ASCII)
                    and math.isfinite(float(value)),'DECIMAL_FORMAT')
        price,qty=map(D,item)
        require(price>0 and qty>=0 and (not snapshot or qty>0),'LEVEL_NONPOSITIVE')
        require(price not in out,'DUPLICATE_PRICE')
        if snapshot and previous is not None:
            require(price<previous if side=='b' else price>previous,'SNAPSHOT_ORDER')
        out[price]=qty;previous=price
    return out


class Depth:
    def __init__(self,symbol):
        require(symbol in ('BTCUSDT',)+SYMBOLS,'SYMBOL')
        self.symbol=symbol;self.b={};self.a={};self.latest=None;self.quote=None
        self.initialized=False;self.issue=None;self.messages=0;self.snapshots=0
        self.deltas=0;self.duplicates=0;self.empty_deltas=0;self.last_fingerprint=None

    def update(self,message,row):
        if self.issue:return
        try:self._update(message,row)
        except (ValueError,KeyError,TypeError,ArithmeticError) as error:
            self.issue=str(error) if isinstance(error,ValueError) else type(error).__name__

    def _update(self,m,row):
        d=m['data'];kind=m['type']
        require(d['s']==self.symbol and m['topic']=='orderbook.50.'+self.symbol and
                row['source']==('spot' if self.symbol=='BTCUSDT' else 'linear'),'IDENTITY')
        require(kind in ('snapshot','delta'),'MESSAGE_TYPE')
        for x in (m['ts'],m['cts'],d['u'],d['seq']):
            require(type(x) is int and 0<x<=2**53-1,'CLOCK_SEQUENCE_TYPE')
        require(m['cts']<=m['ts']<=row['wall_ms'],'CLOCK_ORDER')
        current=dict(ts=m['ts'],cts=m['cts'],u=d['u'],seq=d['seq'],
                     mono=row['mono_ms'],wall=row['wall_ms'])
        bids=levels(d['b'],kind=='snapshot','b');asks=levels(d['a'],kind=='snapshot','a')
        fingerprint=(kind,current['u'],current['seq'],current['ts'],current['cts'],
                     tuple(sorted(bids.items())),tuple(sorted(asks.items())))
        self.messages+=1
        if self.latest:
            if fingerprint==self.last_fingerprint:
                self.duplicates+=1;return  # Transport duplicates do not renew quote age.
            require(kind!='snapshot' and d['u']!=1,'SNAPSHOT_OR_SERVICE_RESET')
            require(d['u']>=self.latest['u'] and d['seq']>=self.latest['seq'],'SEQUENCE_REGRESSION')
            require(m['ts']>=self.latest['ts'] and m['cts']>=self.latest['cts'],'CLOCK_REGRESSION')
            require(d['u']!=self.latest['u'],'SAME_UPDATE_CHANGED_DATA')
        if kind=='snapshot':
            require(not self.initialized,'SECOND_SNAPSHOT')
            self.b=bids;self.a=asks;self.initialized=True;self.snapshots+=1
        else:
            require(self.initialized,'DELTA_WITHOUT_SNAPSHOT')
            for target,update in ((self.b,bids),(self.a,asks)):
                for price,quantity in update.items():
                    if quantity==0:target.pop(price,None)
                    else:target[price]=quantity
            self.deltas+=1
        # Never revive levels that have left the subscribed top-50 domain.
        self.b=dict(sorted(self.b.items(),reverse=True)[:50])
        self.a=dict(sorted(self.a.items())[:50])
        require(not self.b or not self.a or max(self.b)<min(self.a),'CROSSED_BOOK')
        self.latest=current;self.last_fingerprint=fingerprint
        if kind=='snapshot' or bids or asks:self.quote=current
        else:self.empty_deltas+=1

    def state(self):
        return dict(symbol=self.symbol,initialized=self.initialized,issue=self.issue,
                    messages=self.messages,snapshots=self.snapshots,deltas=self.deltas,
                    duplicates=self.duplicates,empty_deltas=self.empty_deltas,
                    latest=self.latest,quote=self.quote,
                    bids=[[str(p),str(q)] for p,q in sorted(self.b.items(),reverse=True)],
                    asks=[[str(p),str(q)] for p,q in sorted(self.a.items())])


def pair_reasons(spot,future,end_wall,end_mono=120000):
    reasons=[]
    for label,book in (('spot',spot),('future',future)):
        if book.issue:reasons.append(label+':'+book.issue)
        if not book.initialized or book.quote is None:reasons.append(label+':UNINITIALIZED');continue
        if not book.b or not book.a:reasons.append(label+':EMPTY_BOOK')
        q=book.quote
        if not 0<=end_wall-q['ts']<=1000:reasons.append(label+':STALE_PUBLICATION')
        if not 0<=end_mono-q['mono']<=1000:reasons.append(label+':STALE_RECEIVE')
        if not 0<=q['wall']-q['ts']<=1000:reasons.append(label+':TRANSPORT_CLOCK')
    if spot.quote and future.quote:
        if abs(spot.quote['ts']-future.quote['ts'])>500:reasons.append('PUBLICATION_SKEW')
        if abs(spot.quote['mono']-future.quote['mono'])>1000:reasons.append('RECEIVE_SKEW')
    return reasons


def economic(spot,future,instrument,end_wall):
    require(spot.symbol=='BTCUSDT' and future.symbol==instrument['symbol'],'PAIR_IDENTITY')
    r=dict(symbol=future.symbol,status='INPUT_INSUFFICIENT',reasons=pair_reasons(spot,future,end_wall),
           cases=[],depth_used={},price_interpretation='KNOWN_DISPLAYED_DEPTH_BOUNDARY',
           actual_fills=False,path_validated=False,qualified=False,legacy_cts_condition=False)
    if spot.quote and future.quote:
        r['legacy_cts_condition']=all(0<=end_wall-b.quote['cts']<=1000 and
            0<=b.quote['ts']-b.quote['cts']<=1000 and 0<=b.quote['wall']-b.quote['cts']<=1000
            for b in (spot,future)) and abs(spot.quote['cts']-future.quote['cts'])<=500 and \
            abs(spot.quote['mono']-future.quote['mono'])<=1000
    if r['reasons']:return r
    with localcontext() as ctx:
        ctx.prec=40
        ask=min(spot.a);mid=(ask+max(spot.b))/2
        days=D(int(instrument['deliveryTime'])-end_wall)/86400000
        require(days>0,'EXPIRED')
        for name,fees in COSTS.items():
            s,f,z=map(D,fees);q,gross,net=quantities(ask,s)
            lot=instrument['lotSizeFilter']
            require(gross<=120 and q<=min(D(lot['maxMktOrderQty']),D(lot['maxOrderQty'])),'MAX_QUANTITY')
            try:
                spend=sweep(sorted(spot.a.items()),gross)*(1+z)
                price=sweep(sorted(future.b.items(),reverse=True),q)/q*(1-z)
                r['depth_used'][name]=dict(spot_asks=used_levels(sorted(spot.a.items()),gross),
                    future_bids=used_levels(sorted(future.b.items(),reverse=True),q))
            except ValueError as error:
                if str(error)!='DEPTH_INSUFFICIENT':raise
                r['reasons'].append(name+':DEPTH_INSUFFICIENT');continue
            require(spend>=5 and q*price>=D(lot['minNotionalValue']),'MIN_NOTIONAL')
            for mult,dev,d in product(map(D,('.5','1','2')),map(D,('-.0025','0','.0025')),map(D,('0','.0005'))):
                c=cash_case(q,gross,net,spend,price,f,s,z,mid,days,mult,dev,d);c['cost']=name;r['cases'].append(c)
        if r['reasons']:return r
        require(len(r['cases'])==36,'SCENARIO_COUNT')
        pressure=[c for c in r['cases'] if c['cost']=='stress']
        r['stress_cash_range']=[str(min(D(c['profit']) for c in pressure)),str(max(D(c['profit']) for c in pressure))]
        r['base_cash_range']=[str(min(D(c['profit']) for c in r['cases'] if c['cost']=='base')),
                              str(max(D(c['profit']) for c in r['cases'] if c['cost']=='base'))]
        r['capital_budget_ok']=all(D(c['remaining_capital'])>=0 for c in r['cases'])
        r['terminal_margin_failure_cases']=sum(D(c['terminal_margin_headroom'])<0 for c in r['cases'])
        low,high=map(D,r['stress_cash_range'])
        r['status']=('NO_PRESSURE_CASH_MARGIN' if high<=0 else
                     'CONDITIONAL_CASH_MARGIN' if low>0 and r['capital_budget_ok'] else 'CONDITIONAL_ONLY')
        return r


def endpoint(rows,receipt,instruments):
    require(receipt['status']=='CAPTURE_COMPLETE' and receipt['observation_stop_ms']==120000 and
            receipt['reconnects']==0 and receipt['fixed_duration_ms']==120000,'CAPTURE_INCOMPLETE')
    books={s:Depth(s) for s in ('BTCUSDT',)+SYMBOLS};acked=set();previous=-1
    for row in rows:
        mono=row['mono_ms']
        require(type(mono) in (int,float) and math.isfinite(mono) and mono>=max(previous,0),'RECEIVE_ORDER')
        previous=mono
        if mono>120000:continue  # Never leak post-endpoint data into valuation.
        if row['kind']!='message':continue
        m=json.loads(row['payload']['raw'])
        require(isinstance(m,dict),'MESSAGE_OBJECT')
        if m.get('op')=='subscribe':
            require(row['source'] in ('spot','linear') and m.get('success') is True and
                    m.get('req_id') in (None,'','obs-'+row['source']),'ACK_ERROR')
            acked.add(row['source']);continue
        if m.get('op')=='pong' or (m.get('op')=='ping' and m.get('ret_msg')=='pong' and m.get('success') is True):continue
        require(m.get('data',{}).get('s') in books,'UNKNOWN_SYMBOL')
        books[m['data']['s']].update(m,row)
    require(acked=={'spot','linear'},'MISSING_ACK')
    end_wall=receipt['start_wall_ms']+120000
    results=[economic(books['BTCUSDT'],books[s],instruments[s],end_wall) for s in SYMBOLS]
    return dict(schema='linear_depth_endpoint_v1',endpoint_mono_ms=120000,endpoint_wall_ms=end_wall,
                books={s:b.state() for s,b in books.items()},results=results,
                economic_endpoints=1,independent_events=0,qualified=False)
