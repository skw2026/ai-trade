#!/usr/bin/env python3
"""Six read-only access controls. Not research admission, a gate retry, or a screen."""
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from option_flow_probe import RUN, GATE, ROOT, option, old_hashes
from screen_regional_session import need, sha, strict, utc, save_new, transport

OUT = RUN / 'instrument-diagnosis'
DOC = ROOT / 'docs/reviews/2026-09-28-deribit-instrument-diagnosis.md'
HISTORY = 'https://history.deribit.com/api/v2/public/'
MAIN = 'https://www.deribit.com/api/v2/public/'
EXAMPLE = 'BTC-24APR26-72000-C'
ARCHIVED_CONTROL = 'BTC-26MAR27-76000-P'


def identities():
    files = [GATE, RUN/'probe-freeze.json', ROOT/'docs/plans/2026-09-28-option-flow-proxy.md']
    for sub in ('attempts','receipts','raw','pages'):
        files += sorted((RUN/sub).iterdir())
    return dict(old_gates=old_hashes(), files={str(p.relative_to(ROOT)):sha(p.read_bytes()) for p in files})


def summarize(data):
    if 'error' in data:
        return {'api_error':data['error']}
    r = data.get('result')
    if isinstance(r,list):
        names=sorted(x['instrument_name'] for x in r if isinstance(x,dict) and isinstance(x.get('instrument_name'),str))
        return dict(instrument_count=len(r),first_names=names[:3],field_names=sorted(r[0]) if r else [])
    if isinstance(r,dict) and 'trades' in r:
        return dict(trade_count=len(r['trades']),has_more=r.get('has_more'),
            identities=[{k:t.get(k) for k in ('instrument_name','trade_id','trade_seq','timestamp')} for t in r['trades']])
    if isinstance(r,dict):
        return dict(metadata={k:r.get(k) for k in ('instrument_name','kind','option_type','expiry',
            'expiration_timestamp','creation_timestamp','base_currency','settlement_currency','contract_size')},field_names=sorted(r))
    return {'unexpected_result_type':type(r).__name__}


def get(index,host,method,params):
    need(host in (MAIN,HISTORY) and method in ('get_instruments','get_instrument','get_last_trades_by_instrument'), 'PUBLIC_READ_ONLY')
    count=len(list(OUT.glob('attempt-*.json')))
    need(count < 6 and len(list((RUN/'attempts').glob('*.json')))+count < 20,'DIAGNOSTIC_GET_BUDGET')
    url=host+method+'?'+urllib.parse.urlencode(params)
    save_new(OUT/f'attempt-{index}.json',dict(url=url,at=utc(time.time()*1000)))
    raw,code,error=b'',None,None
    try:
        request=urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'ai-trade-readonly-diagnosis/1.0'})
        with transport().open(request,timeout=25) as response:
            code=response.status
            raw=response.read(8_000_001)
    except urllib.error.HTTPError as exc:
        code,raw,error=exc.code,exc.read(8_000_001),str(exc)
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)
    with (OUT/f'raw-{index}.bin').open('xb') as f:
        f.write(raw)
        f.flush()
        import os
        os.fsync(f.fileno())
    truncated=len(raw)>8_000_000
    data={}
    if not truncated:
        try:
            data=strict(raw)
        except (ValueError,TypeError):
            error=error or 'NON_JSON_RESPONSE'
    result=dict(index=index,url=url,http_status=code,error=error,truncated=truncated,
        raw_sha256=sha(raw),bytes=len(raw),scope='DIAGNOSIS_NOT_ACCEPTANCE',**summarize(data))
    save_new(OUT/f'receipt-{index}.json',result)
    print(json.dumps(result),flush=True)
    time.sleep(1)
    return data


def main():
    state=strict(GATE.read_bytes())
    need(state['status']=='HALTED' and state['active']['failure_id']=='0ea648c079f54d959163721d1c0ba0ec','BOUND_STOPPED_DIAGNOSIS_ONLY')
    need(len(list((RUN/'attempts').glob('*.json')))==12,'ORIGINAL_GET_COUNT_CHANGED')
    OUT.mkdir(exist_ok=False)
    before=identities()
    save_new(OUT/'before.json',dict(identities=before,diagnosis_plan_sha256=sha(DOC.read_bytes()),
        source_sha256=sha(Path(__file__).read_bytes()),maximum_new_gets=6))
    directory=get(1,HISTORY,'get_instruments',dict(currency='BTC',kind='option',expired='false',include_old='true'))
    names=[]
    if isinstance(directory.get('result'),list):
        for row in directory['result']:
            name=row.get('instrument_name','')
            try:
                if option(name)[1]=='C': names.append(name)
            except (ValueError,KeyError,TypeError):
                pass
    name=min(names) if names else ARCHIVED_CONTROL
    get(2,MAIN,'get_instrument',dict(instrument_name=EXAMPLE))
    get(3,HISTORY,'get_last_trades_by_instrument',dict(instrument_name=EXAMPLE,count=1,include_old='true'))
    get(4,HISTORY,'get_last_trades_by_instrument',dict(instrument_name=name,count=1,include_old='true'))
    get(5,HISTORY,'get_last_trades_by_instrument',dict(instrument_name=name,start_seq=1,end_seq=1,count=1,include_old='true'))
    get(6,HISTORY,'get_instrument',dict(instrument_name=name,include_old='true'))
    after=identities()
    save_new(OUT/'after.json',dict(identities=after,unchanged=after==before,scope='DIAGNOSTIC_METADATA_NOT_ACCEPTANCE'))
    need(after==before,'ORIGINAL_EVIDENCE_CHANGED')


if __name__=='__main__':
    main()
