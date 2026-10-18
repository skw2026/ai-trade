"""Descriptive observation types, not a trading state machine or permission gate.

No fetch, realized return, forecast, policy, recovery or order interface. A quiet
trade candle cannot establish an empty book, and an active candle cannot prove
executable size. Observations must refer to the same time and instrument.
"""
from decimal import Decimal as D
import math

BAR=300000


def candle(bar,t,observed_through,*,mark=False):
    if type(t) is not int or t<0 or t % BAR or type(observed_through) is not int:
        raise ValueError('OBSERVATION_CLOCK')
    if type(mark) is not bool:
        raise ValueError('SOURCE_KIND')
    if observed_through<t+BAR:
        return dict(status='NOT_YET_CLOSED',trades='NOT_OBSERVED',price_range_observed=False)
    if bar is None:
        return dict(status='MISSING',trades='NOT_OBSERVED',price_range_observed=False)
    if bar.get('complete') is not True or bar.get('close_ms') != t+BAR-1:
        return dict(status='INCOMPLETE',trades='NOT_OBSERVED',price_range_observed=False)
    try:
        o,h,l,c=[D(str(bar[k])) for k in ('o','h','l','c')]
        good=all(v.is_finite() and v>0 for v in (o,h,l,c)) and l<=min(o,c)<=max(o,c)<=h
    except (ValueError,ArithmeticError,KeyError):
        good=False
    if not good:
        return dict(status='INVALID_PRICE',trades='NOT_OBSERVED',price_range_observed=False)
    if mark:
        return dict(status='MARK_RANGE_OBSERVED',trades='NOT_APPLICABLE',price_range_observed=True)
    try:
        v=D(str(bar['v'])); n=bar['n']
        counts=type(n) is int and n>=0 and v.is_finite() and v>=0 and ((n==0)==(v==0))
    except (ValueError,ArithmeticError,KeyError):
        counts=False
    if not counts:
        return dict(status='INVALID_TRADE_COUNTS',trades='NOT_OBSERVED',price_range_observed=False)
    if n==0:
        return dict(status='COMPLETE_QUIET_CANDLE',trades='ZERO_RECORDED',price_range_observed=False)
    return dict(status='TRADE_RANGE_OBSERVED',trades='POSITIVE_RECORDED',price_range_observed=True)


def quote(book,side,amount,mono_ms,*,max_receive_age_ms=1000):
    """Evaluate only an already reconstructed book, never infer it from a candle.

    amount: BTC for spot; number of 100 USD contracts for the prior COIN-M
    diagnostic. Limits are those of the existing capture, not trading tolerances.
    """
    if side not in ('bid','ask'):
        raise ValueError('SIDE')
    size=D(str(amount))
    if not size.is_finite() or size<=0:
        raise ValueError('SIZE')
    if not all(type(v) in (float,int) and math.isfinite(v) for v in (mono_ms,max_receive_age_ms)) or max_receive_age_ms<0:
        raise ValueError('QUOTE_CLOCK')
    base=dict(actual_execution_qualified=False,orders_authorized=False,recovery_authorized=False,
              exchange_clock_alignment='UNKNOWN')
    if book is None:
        return dict(base,status='NOT_CAPTURED',displayed_depth_covers_request=None)
    if book.kind not in ('spot','future'):
        raise ValueError('BOOK_KIND')
    if not book.bridged or book.event is None:
        return dict(base,status='UNBRIDGED',displayed_depth_covers_request=None)
    received=book.event['received']
    if type(received) not in (float,int) or not math.isfinite(received):
        raise ValueError('RECEIVE_CLOCK')
    age=mono_ms-received
    if age<0:
        return dict(base,status='FUTURE_RECEIPT',displayed_depth_covers_request=None)
    if age>max_receive_age_ms:
        return dict(base,status='STALE_LOCAL_RECEIPT',displayed_depth_covers_request=None)
    if not book.bids or not book.asks or max(book.bids)>=min(book.asks):
        return dict(base,status='EMPTY_OR_CROSSED_BOOK',displayed_depth_covers_request=None)
    try:
        value,last=book.sweep(side,size,inverse=book.kind=='future')
    except ValueError as exc:
        if str(exc)!='INSUFFICIENT_KNOWN_DEPTH': raise
        return dict(base,status='INSUFFICIENT_CAPTURED_DEPTH',displayed_depth_covers_request=False)
    return dict(base,status='DISPLAYED_DEPTH_COVERS_REQUEST',displayed_depth_covers_request=True,
                vwap=str(value),last_price=str(last),local_receive_age_ms=age)


def pair(spot,future,mono_ms,spot_btc,*,direction):
    if direction not in ('entry','exit'):
        raise ValueError('DIRECTION')
    if (spot is not None and spot.kind!='spot') or (future is not None and future.kind!='future'):
        raise ValueError('PAIR_KIND')
    spot_side,future_side=('ask','bid') if direction=='entry' else ('bid','ask')
    s=quote(spot,spot_side,spot_btc,mono_ms)
    f=quote(future,future_side,25,mono_ms)
    skew=None
    if spot is not None and future is not None and spot.event and future.event:
        skew=abs(spot.event['E']-future.event['E'])
    covers=s['displayed_depth_covers_request'] is True and f['displayed_depth_covers_request'] is True
    return dict(spot=s,future=f,event_skew_ms=skew,
                displayed_pair_observed=covers and skew is not None and skew<=500,
                same_time_atomic_execution=False,actual_execution_qualified=False,
                orders_authorized=False,recovery_authorized=False)


def historical_bar_evidence(quarter,t):
    """Raw candles only: explicitly do not accept a different-date book as proof."""
    rows=quarter['series']
    get=lambda kind:rows[kind].get(str(t),rows[kind].get(t))
    return dict(available_ms=t+BAR,
                future_trades=candle(get('future'),t,t+BAR),
                spot_trade_range=candle(get('spot'),t,t+BAR),
                future_mark_range=candle(get('mark'),t,t+BAR,mark=True),
                executable_quotes=dict(status='NOT_CAPTURED',displayed_depth_covers_request=None),
                actual_execution_qualified=False,orders_authorized=False,recovery_authorized=False)
