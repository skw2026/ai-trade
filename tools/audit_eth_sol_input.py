"""Independently bind the collection summary to every retained request/response."""
from collections import Counter
import hashlib
import json
from pathlib import Path

import eth_sol_market as market
import eth_sol_intake as intake
from mvp_reference_inputs import strict_json


def verify_records(root,freeze,collection):
    root=Path(root);pages={};attempts=Counter();raw_bytes=0;errors=0;wait=0.;identities=0
    files={}
    for folder in sorted((root/'identity-attempts').iterdir()):
        r=strict_json((folder/'record.json').read_bytes())
        intake.need(r['status']=='VALID' and intake.sha(folder/'response.bin')==r['wire_sha256'],'IDENTITY_WIRE_BINDING')
        raw_bytes+=(folder/'response.bin').stat().st_size;identities+=1
        for p in folder.iterdir():files[str(p)]=intake.sha(p)
    intake.need(identities==2,'IDENTITY_COUNT')
    folders=sorted((root/'market-attempts').iterdir())
    for i,folder in enumerate(folders):
        intake.need(folder.name==f'{i:04d}','ATTEMPT_SEQUENCE')
        r=strict_json((folder/'record.json').read_bytes());started=strict_json((folder/'started.json').read_bytes())
        index=r['index'];intake.need(type(index) is int and 0<=index<len(freeze['requests']),'PAGE_INDEX')
        intake.need(r['request']==started['request']==freeze['requests'][index] and started['index']==index,'REQUEST_BINDING')
        attempts[index]+=1;intake.need(r['page_attempt']==started['page_attempt']==attempts[index]<=2,'PAGE_ATTEMPTS')
        raw=folder/'response.bin';digest=intake.sha(raw)
        intake.need(digest==r['wire_sha256'] and raw.stat().st_size==r['bytes'],'WIRE_BINDING')
        intake.need(r['status'] in ('VALID','RECOVERABLE_NOT_VALID'),'INCOMPLETE_OR_FAILED_COLLECTION')
        if r['status']=='VALID':
            intake.need(index not in pages,'MULTIPLE_VALID_PAGE_READS')
            pages[index]=dict(folder=str(folder),wire_sha256=digest)
        else:
            intake.need(r['http']==200 and r['code'] in (10016,10006) and r['page_attempt']==1,'UNDECLARED_RECOVERY')
            errors+=1
        raw_bytes+=raw.stat().st_size;wait+=r['wait_seconds']
        for p in folder.iterdir():files[str(p)]=intake.sha(p)
    intake.need(set(pages)==set(range(len(freeze['requests']))),'VALID_PAGE_COVERAGE')
    intake.need({str(i):v for i,v in pages.items()}==collection['records'],'COLLECTION_RESPONSE_BINDING')
    intake.need(collection['attempts']==len(folders)+2 and collection['bytes']==raw_bytes and
        collection['known_errors']==errors and abs(collection['wait_seconds']-wait)<1e-9,'COLLECTION_TOTALS')
    intake.need(len(folders)+2<=1500 and raw_bytes<=1073741824 and errors<=30 and wait<=300,'TOTAL_BUDGET')
    return dict(status='RAW_COLLECTION_BINDING_PASS',attempts=len(folders)+2,wire_bytes=raw_bytes,
        known_errors=errors,valid_pages=len(pages),files=files)


def main():
    market.guard();intake.preserve()
    root=intake.ROOT
    result=verify_records(root,strict_json((root/'market-freeze.json').read_bytes()),
        strict_json((root/'market-collection.json').read_bytes()))
    result['source_sha256']=intake.sha(__file__)
    intake.save(root/'collection-audit.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='files'}),flush=True)


if __name__=='__main__':main()
