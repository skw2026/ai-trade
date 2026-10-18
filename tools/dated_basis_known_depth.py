"""Known-level depth bounds across the initial snapshot boundary.

An absolute, sequence-accepted update can establish a previously uncaptured
level. Unknown better levels are omitted, so the sweep is a displayed-price
bound, not exact full-book VWAP, an actual fill or permission to trade.
"""
from decimal import Decimal as D
import json
import math

from analyze_dated_basis_quotes import Book


class KnownDepth:
    def __init__(self,kind):
        if kind not in ('spot','future'):
            raise ValueError('BOOK_KIND')
        self._book=Book(kind)
        self._origin={'bid':{},'ask':{}}
        self._buffer=[]
        self._tainted=False

    def snapshot(self,data):
        if self._tainted:
            raise ValueError('TAINTED_BOOK')
        try:
            self._book.snapshot(data)
            self._origin={'bid':{p:'SNAPSHOT' for p in self._book.bids},
                          'ask':{p:'SNAPSHOT' for p in self._book.asks}}
            pending=self._buffer; self._buffer=[]
            for row in pending: self.update(row)
        except Exception:
            self._tainted=True
            raise

    def update(self,row):
        if self._tainted:
            raise ValueError('TAINTED_BOOK')
        if self._book.last is None:
            self._buffer.append(row)
            return
        try:
            before=self._book.applied
            self._book.update(row)
            if self._book.applied==before:
                return
            event=json.loads(row['payload']['raw'])
            for side,key in (('bid','b'),('ask','a')):
                for raw_price,raw_quantity in event[key]:
                    p,q=D(raw_price),D(raw_quantity)
                    if q:
                        self._origin[side][p]='ABSOLUTE_SEQUENCE_UPDATE'
                    else:
                        self._origin[side].pop(p,None)
        except Exception:
            self._tainted=True
            raise

    def bound(self,side,amount,mono_ms):
        if side not in ('bid','ask'):
            raise ValueError('SIDE')
        amount=D(str(amount))
        if not amount.is_finite() or amount<=0:
            raise ValueError('SIZE')
        if type(mono_ms) not in (float,int) or not math.isfinite(mono_ms):
            raise ValueError('CLOCK')
        base=dict(exact_full_book_vwap=False,actual_execution_qualified=False,
                  orders_authorized=False,recovery_authorized=False,exchange_clock_alignment='UNKNOWN')
        if self._tainted:
            return dict(base,status='TAINTED_BOOK',displayed_depth_covers_request=None)
        b=self._book
        if not b.bridged or b.event is None:
            return dict(base,status='UNBRIDGED',displayed_depth_covers_request=None)
        age=mono_ms-b.event['received']
        if not math.isfinite(age) or age<0 or age>1000:
            return dict(base,status='INVALID_OR_STALE_LOCAL_CLOCK',displayed_depth_covers_request=None)
        ladder=b.bids if side=='bid' else b.asks
        if any(p not in self._origin[side] for p in ladder):
            return dict(base,status='UNBOUND_LEVEL_PROVENANCE',displayed_depth_covers_request=None)
        value=D(0); remaining=amount; used=[]
        for p in sorted(ladder,reverse=side=='bid'):
            q=min(remaining,ladder[p])
            if q<=0: continue
            value+=q*100/p if b.kind=='future' else q*p
            used.append(dict(price=str(p),quantity=str(q),origin=self._origin[side][p],
                             outside_initial_region=p<b.bid_floor or p>b.ask_ceiling))
            remaining-=q
            if not remaining:
                price=amount*100/value if b.kind=='future' else value/amount
                return dict(base,status='KNOWN_DISPLAYED_DEPTH_COVERS_REQUEST',displayed_depth_covers_request=True,
                            price_bound=str(price),bound_direction='LOWER_SELL_PRICE' if side=='bid' else 'UPPER_BUY_PRICE',
                            used_levels=used,event_ms=b.event['E'],local_receive_age_ms=age)
        return dict(base,status='KNOWN_LEVELS_INSUFFICIENT',displayed_depth_covers_request=False,
                    known_quantity=str(amount-remaining))
