#!/usr/bin/env python3
"""Bounded public GET archive for the separately approved frozen MVP screen.

No credentials, redirects, environment proxies, retries within an invocation,
or economic computation. Existing successful pages are verified before reuse.
Failed attempts remain charged and require the repository gate review to resume.
"""
import argparse
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from mvp_reference_inputs import (ROOT, CONTRACT_SHA256, candle, load_contract,
                                  number, require, strict_json, timestamp, utc_ms, window)
from mvp_reference_pipeline import digest, sources, write_json

AUTH = ROOT / "docs/plans/2026-09-21-mvp-reference-history-execution.json"
SELF = Path(__file__).resolve()


def requests(contract):
    start, _, end = window(contract)
    records = []
    # Funding first: reject a changed/missing grid before downloading all candles.
    grid = contract["input"]["funding_required_grid_ms"]
    chunks = [("funding", a, min(end, a+200*grid)-1, 200, grid)
              for a in range(start, end, 200*grid)]
    interval = contract["interval_ms"]
    for a in range(start, end, 1000*interval):
        chunks.extend((kind,a,min(end,a+1000*interval)-1,1000,interval) for kind in ("trade","mark"))
    for kind,a,b,limit,step in chunks:
        funding = kind == "funding"
        params = {"category":"linear","symbol":contract["symbol"],"limit":limit,
                  "startTime" if funding else "start":a,"endTime" if funding else "end":b}
        if not funding:params["interval"]="5"
        records.append({"kind":kind,"url":"https://"+contract["input"]["public_host"]+
            contract["input"][kind+"_endpoint"]+"?"+urllib.parse.urlencode(params),
            "start":a,"end":b,"step":step,"limit":limit})
    return records


def freeze(directory):
    auth = strict_json(AUTH.read_bytes())
    require(auth["historical_execution_approved"] is True and auth["contract_sha256"] == CONTRACT_SHA256,
            "NEW_APPROVAL_REQUIRED")
    require(utc_ms(auth["started_at_utc"]) <= time.time()*1000 < utc_ms(auth["deadline_utc"]), "STAGE_TIME_SCOPE")
    contract = load_contract()
    directory = Path(directory).resolve()
    directory.mkdir(parents=False, exist_ok=False)
    (directory/"archive").mkdir()
    plan = {"schema":"mvp_public_request_plan_v1","contract_sha256":CONTRACT_SHA256,
            "authorization_sha256":digest(AUTH),"collector_sha256":digest(SELF),
            "source_sha256":sources(),"binary_sha256":digest(ROOT/"build/trade_bot"),
            "reference_config_sha256":digest(ROOT/"config/bybit.replay.mvp-reference.yaml"),
            "deadline_utc":auth["deadline_utc"],"max_gets":auth["maximum_public_gets"],
            "requests":requests(contract),"redirects":False,"automatic_retry":False}
    require(len(plan["requests"]) <= plan["max_gets"] == 500,"REQUEST_BUDGET")
    write_json(directory/"request-plan.json",plan)
    return plan


def validate_page(raw, request):
    response = strict_json(raw)
    require(type(response.get("retCode")) is int and response["retCode"] == 0,"API_ERROR")
    result = response["result"]
    require(result.get("category") == "linear","RESPONSE_CATEGORY")
    funding = request["kind"] == "funding"
    if not funding:require(result.get("symbol") == "BTCUSDT","RESPONSE_SYMBOL")
    rows = result.get("list")
    require(isinstance(rows,list) and len(rows) <= request["limit"],"PAGE_LIMIT")
    times=[]
    for row in rows:
        if funding:
            require(row.get("symbol") == "BTCUSDT","FUNDING_SYMBOL")
            times.append(timestamp(row["fundingRateTimestamp"]));number(row["fundingRate"])
        else:
            ts,_ = candle(row,request["kind"]);times.append(ts)
    step=request["step"]
    first=((request["start"]+step-1)//step)*step
    require(times == list(range(first,request["end"]+1,step))[::-1],"PAGE_GRID_OR_COVERAGE")
    return len(times)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def opener():
    context=ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED,"TLS_VERIFICATION_REQUIRED")
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),NoRedirect())


def collect(plan_path):
    plan_path=Path(plan_path).resolve();directory=plan_path.parent;archive=directory/"archive"
    plan=strict_json(plan_path.read_bytes());auth=strict_json(AUTH.read_bytes())
    require(plan["schema"]=="mvp_public_request_plan_v1" and digest(AUTH)==plan["authorization_sha256"] and
            auth["historical_execution_approved"] is True,"AUTHORIZATION_CHANGED")
    require(plan["contract_sha256"]==CONTRACT_SHA256 and digest(SELF)==plan["collector_sha256"] and
            sources()==plan["source_sha256"] and digest(ROOT/"build/trade_bot")==plan["binary_sha256"] and
            digest(ROOT/"config/bybit.replay.mvp-reference.yaml")==plan["reference_config_sha256"],"SCREEN_IDENTITY_CHANGED")
    require(plan["requests"]==requests(load_contract()) and plan["max_gets"]==500 and
            plan["deadline_utc"]==auth["deadline_utc"],"REQUEST_PLAN_CHANGED")
    deadline=utc_ms(plan["deadline_utc"])/1000
    attempted=len(list(archive.glob("attempt-*.json")))
    if attempted:
        # An interrupted attempt without a receipt is not implicitly retryable.
        for attempt in archive.glob("attempt-*.json"):
            require(attempt.with_name(attempt.name.replace("attempt-","receipt-")).is_file(),"UNFINISHED_ATTEMPT")
        gate=strict_json((ROOT/".artifacts/validation-gate/state.json").read_bytes())
        require(gate["status"]=="RUNNING" and gate["active"].get("review_path"),"REVIEWED_GATE_RETRY_REQUIRED")
    transport=opener();pages=[];total_bytes=sum(p.stat().st_size for p in archive.glob("response-*.json"))
    for index,request in enumerate(plan["requests"]):
        require(time.time()<deadline,"STAGE_DEADLINE")
        cached=sorted(archive.glob(f"page-{index:03d}-*.json"))
        require(len(cached)<=1,"DUPLICATE_SUCCESS_PAGE")
        if cached:
            record=strict_json(cached[0].read_bytes());raw_path=archive/record["file"]
            require(record["url"]==request["url"] and digest(raw_path)==record["sha256"],"CACHED_PAGE_CHANGED")
            validate_page(raw_path.read_bytes(),request);pages.append(record);continue
        require(attempted<plan["max_gets"],"PUBLIC_GET_BUDGET")
        attempted+=1;stem=f"{attempted:04d}"
        write_json(archive/("attempt-"+stem+".json"),{"url":request["url"],"request_index":index,
                   "attempt":attempted,"started_at_ms":int(time.time()*1000)})
        raw=b"";status=None;failure=None
        try:
            req=urllib.request.Request(request["url"],headers={"User-Agent":"ai-trade-mvp-reference-readonly/1"},method="GET")
            with transport.open(req,timeout=min(20,deadline-time.time())) as response:
                status=response.status;raw=response.read(2*1024*1024+1)
        except urllib.error.HTTPError as error:
            status=error.code;raw=error.read(2*1024*1024+1);failure="HTTP_ERROR_"+str(status)
        except (urllib.error.URLError,OSError,TimeoutError) as error:
            failure=type(error).__name__+":"+str(error)
        received=int(time.time()*1000)
        raw_path=archive/("response-"+stem+".json")
        with raw_path.open("xb") as f:f.write(raw)
        total_bytes+=len(raw)
        record={"kind":request["kind"],"url":request["url"],"file":raw_path.name,
                "sha256":digest(raw_path),"received_at_ms":received}
        if failure is None:
            try:
                require(status==200 and len(raw)<=2*1024*1024 and total_bytes<=256*1024*1024,"HTTP_OR_BYTE_BUDGET")
                require(received<=deadline*1000,"STAGE_DEADLINE")
                count=validate_page(raw,request)
            except (ValueError,KeyError,TypeError) as error:failure=str(error)
        write_json(archive/("receipt-"+stem+".json"),{**record,"http_status":status,"error":failure})
        require(failure is None,"PUBLIC_COLLECTION_STOP:"+str(failure))
        write_json(archive/f"page-{index:03d}-{stem}.json",record);pages.append(record)
        if (index+1)%20==0 or index+1==len(plan["requests"]):
            print(json.dumps({"completed_pages":index+1,"planned_pages":len(plan["requests"]),
                              "attempted_gets":attempted,"last_page_rows":count}),flush=True)
        time.sleep(0.10)
    manifest={"schema":"mvp_public_archive_v1","contract_sha256":CONTRACT_SHA256,"pages":pages,
              "request_plan_sha256":digest(plan_path),"attempted_gets":attempted,"total_response_bytes":total_bytes}
    write_json(archive/"manifest.json",manifest)
    return {"completed_pages":len(pages),"attempted_gets":attempted,"total_response_bytes":total_bytes,
            "manifest_sha256":digest(archive/"manifest.json"),"historical_screen_started":False}


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest="action",required=True)
    sub.add_parser("freeze").add_argument("--output",type=Path,required=True)
    sub.add_parser("collect").add_argument("--plan",type=Path,required=True)
    args=p.parse_args()
    if args.action=="freeze":
        plan=freeze(args.output);print(json.dumps({"planned_gets":len(plan["requests"]),"network_started":False}))
    else:print(json.dumps(collect(args.plan),sort_keys=True))


if __name__=="__main__":main()
