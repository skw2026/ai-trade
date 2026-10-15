"""Strict reconstructed OBM input checks; no trading or network access."""
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import io

SERIES=('obm_spent_value_ge155d_btc_daily','obm_spent_value_btc_daily')
FIELDS=['date','series_id','value','unit','frequency','release_version']

def require(value,reason):
    if not value:raise ValueError(reason)

def series(raw,expected):
    reader=csv.DictReader(io.StringIO(raw.decode('utf-8')))
    require(reader.fieldnames==FIELDS,'OBM_SCHEMA')
    values={};versions=set();previous=None
    for r in reader:
        require(None not in r and all(v is not None for v in r.values()),'OBM_ROW_WIDTH')
        d=dt.date.fromisoformat(r['date'])
        require(d.isoformat()==r['date'] and (previous is None or d>previous),'OBM_DATE_ORDER_OR_DUPLICATE')
        previous=d
        require(r['series_id']==expected and r['unit']=='BTC' and r['frequency']=='daily','OBM_IDENTITY')
        try:v=Decimal(r['value'])
        except InvalidOperation:raise ValueError('OBM_VALUE')
        require(v.is_finite() and v>=0,'OBM_VALUE')
        values[d]=v;versions.add(r['release_version'])
    require(values and len(versions)==1 and '' not in versions,'OBM_RELEASE')
    return values,next(iter(versions))

def required_dates():
    dates=[]
    for a,b in (('2020-12-01','2021-12-30'),('2024-12-01','2026-01-01')):
        start,end=dt.date.fromisoformat(a),dt.date.fromisoformat(b)
        dates.extend(start+dt.timedelta(days=i) for i in range((end-start).days))
    return dates

def admit(old_raw,total_raw,dates=None):
    old,ov=series(old_raw,SERIES[0]);total,tv=series(total_raw,SERIES[1])
    require(ov==tv,'OBM_VERSION_MISMATCH')
    dates=required_dates() if dates is None else dates
    ratios={}
    for d in dates:
        require(d in old and d in total,'OBM_REQUIRED_DATE_MISSING:'+d.isoformat())
        require(total[d]>0 and old[d]<=total[d],'OBM_ZERO_OR_SUBSET:'+d.isoformat())
        ratios[d.isoformat()]=str(old[d]/total[d])
    return dict(status='RECONSTRUCTED_DEVELOPMENT_INPUT_ADMITTED',release_version=ov,
        required_days=len(dates),raw_rows=[len(old),len(total)],
        zero_old_days=sum(old[d]==0 for d in dates),ratios=ratios,
        first_observed_at_certified=False,entity_adjusted=False)
