"""Fixed same-source gap diagnosis. No returns, orders or admission override."""
import csv
import datetime as dt
from decimal import Decimal
import io
import zipfile
from dated_basis_input_probe import require,digest

SYMBOL='BTCUSD_230929'
DAYS=('2023-08-30','2023-08-27','2023-08-28','2023-08-29','2023-08-31')
BAR=300000


def requests():
    result=[]
    for day in DAYS:
        for kind,folder in (('future','klines'),('mark','markPriceKlines')):
            name=SYMBOL+'-5m-'+day+'.zip'
            result.append(dict(kind=kind,day=day,filename=name,
                url='https://data.binance.vision/data/futures/cm/daily/'+folder+'/'+SYMBOL+'/5m/'+name))
    return result


def parse(raw,checksum,request):
    require(request in requests(),'FIXED_DIAGNOSTIC_SCOPE')
    words=checksum.decode('ascii').strip().split()
    require(len(words)==2 and words[0]==digest(raw) and words[1].lstrip('*')==request['filename'],'CHECKSUM')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        members=z.infolist()
        require(len(members)==1 and members[0].filename==request['filename'][:-4]+'.csv','ZIP_IDENTITY')
        require(members[0].file_size<=1048576 and not members[0].flag_bits&1,'ZIP_LIMIT')
        rows=list(csv.reader(io.StringIO(z.read(members[0]).decode('utf-8-sig'))))
    if rows and rows[0][0] in ('open_time','open time'):
        require(len(rows[0])==12,'HEADER_WIDTH');rows=rows[1:]
    start=int(dt.datetime.fromisoformat(request['day']).replace(tzinfo=dt.timezone.utc).timestamp()*1000)
    require(len(rows)==288,'FULL_DAY_REQUIRED')
    bars={}
    for i,row in enumerate(rows):
        require(len(row)==12 and row[0].isascii() and row[0].isdigit(),'ROW_SHAPE')
        t=int(row[0]);end=int(row[6])
        require(t==start+i*BAR and end==t+BAR-1,'EXACT_DAY_GRID')
        o,h,l,c=(Decimal(row[k]) for k in (1,2,3,4))
        require(all(x.is_finite() for x in (o,h,l,c)) and 0<l<=min(o,c)<=max(o,c)<=h,'OHLC')
        volumes=[Decimal(row[k]) for k in (5,7,9,10)]
        require(all(v.is_finite() and v>=0 for v in volumes),'VOLUME')
        require(row[8].isascii() and row[8].isdigit(),'TRADES')
        bars[t]=dict(o=str(o),h=str(h),l=str(l),c=str(c),v=str(volumes[0]),n=int(row[8]),close_ms=end,complete=True)
    return bars


def reconcile(month,days):
    """Returns new series; refuses conflicts and cannot replace existing records."""
    result=dict(month);overlap=added=0
    for bars in days:
        for t,bar in bars.items():
            if t in result:
                require(result[t]==bar,'MONTH_DAY_CONFLICT');overlap+=1
            else:result[t]=bar;added+=1
    return result,dict(overlap=overlap,added=added)
