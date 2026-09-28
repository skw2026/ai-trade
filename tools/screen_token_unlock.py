#!/usr/bin/env python3
"""Fixed, one-shot ARB vesting reference screen. Public read-only data only."""
import argparse
import datetime as dt
from decimal import Decimal
import json
import math
from pathlib import Path
import random
import time
import urllib.error
import urllib.parse
import urllib.request

from screen_regional_session import need, sha, strict, number, stamp, ms, utc, save_new, transport, quantile, OLD_GATES

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/contracts/token_unlock_screen_v1.json"
BATCH = ROOT / ".artifacts/candidate-intake-20260928"
RUN = BATCH / "e2"
GATE = BATCH / "validation-state.json"
HOUR = 3600000
STEP = HOUR // 2
DAY = 24 * HOUR
SOURCES = ["tools/screen_token_unlock.py", "tools/test_screen_token_unlock.py",
           "tools/screen_regional_session.py", "docs/contracts/token_unlock_screen_v1.json",
           "docs/plans/2026-09-28-candidate-intake-batch.md"]
GATES = OLD_GATES + ["regional-session-screen-20260928/validation-state.json"]
ENDPOINTS = {"trade": "/v5/market/kline", "mark": "/v5/market/mark-price-kline",
             "funding": "/v5/market/funding/history"}


def contract():
    c = strict(CONTRACT.read_bytes())
    need(c["symbol"] == "ARBUSDT" and c["category"] == "linear", "CONTRACT_MARKET")
    need(not any(c["authority"].values()), "ACTUATION_FORBIDDEN")
    need(c["calendar"] == dict(entry_day=9,exit_day=17,hour_utc=0,minute_utc=30)
         and c["interval_minutes"]==30 and c["initial_reference_capital"]==1, "FIXED_SEMANTICS")
    return c


def schedule(c):
    y, m = map(int, c["first_month"].split("-"))
    ey, em = map(int, c["last_month"].split("-"))
    result = []
    while (y, m) <= (ey, em):
        ny, nm = (y+1, 1) if m == 12 else (y, m+1)
        def at(year, month, day):
            return ms(f"{year:04d}-{month:02d}-{day:02d}T00:30:00Z")
        a, b, e = at(y,m,9), at(y,m,17), at(ny,nm,9)
        result.append(dict(entry=a, exit=b, end=e, rho=(b-a)/(e-a)))
        y, m = ny, nm
    need(result and ms(c["data_start"]) <= result[0]["entry"]
         and result[-1]["end"] < ms(c["data_end"]), "CALENDAR_COVERAGE")
    return result


def request_plan(c):
    start, end = ms(c["data_start"]), ms(c["data_end"])
    result = []
    for kind in ("funding", "trade", "mark"):
        funding = kind == "funding"
        span = c["funding"]["page_span_hours"]*HOUR if funding else STEP*1000
        for a in range(start, end, span):
            b = min(end,a+span)-1
            params = dict(category="linear", symbol="ARBUSDT", limit=200 if funding else 1000)
            params.update({"startTime" if funding else "start":a, "endTime" if funding else "end":b})
            if not funding:
                params["interval"] = "30"
            result.append(dict(kind=kind, start=a, end=b, limit=params["limit"],
                          url="https://api.bybit.com"+ENDPOINTS[kind]+"?"+urllib.parse.urlencode(params)))
    need(len(result) <= c["budget"]["batch_market_gets"], "GET_BUDGET")
    return result


def validate_page(raw, req):
    r = strict(raw)
    need(type(r.get("retCode")) is int and r["retCode"] == 0, "API_CODE")
    r = r["result"]
    need(r.get("category") == "linear", "CATEGORY")
    rows, times = r["list"], []
    need(isinstance(rows,list) and len(rows)<=req["limit"], "PAGE_SIZE")
    funding = req["kind"] == "funding"
    if not funding:
        need(r.get("symbol") == "ARBUSDT", "SYMBOL")
    for row in rows:
        if funding:
            need(row.get("symbol") == "ARBUSDT", "FUNDING_SYMBOL")
            t = stamp(row["fundingRateTimestamp"])
            number(row["fundingRate"])
            need(t%HOUR == 0, "FUNDING_ALIGNMENT")
        else:
            need(isinstance(row,list) and len(row)==(7 if req["kind"]=="trade" else 5), "ROW_SIZE")
            t = stamp(row[0])
            o,h,l,close = map(number,row[1:5])
            need(0<l<=min(o,close)<=max(o,close)<=h, "OHLC")
            if req["kind"] == "trade":
                need(number(row[5])>=0 and number(row[6])>=0, "VOLUME")
        need(req["start"]<=t<=req["end"], "ROW_WINDOW")
        times.append(t)
    need(times==sorted(set(times),reverse=True), "ROW_ORDER_DUPLICATE")
    if funding:
        # 168-hour windows have at most 168 hourly settlements, below the 200-row cap.
        need(len(rows)<req["limit"] and rows, "FUNDING_TRUNCATED_OR_EMPTY")
    else:
        need(times==list(range(req["start"],req["end"]+1,STEP))[::-1], "BAR_GRID")
    return rows


def identities():
    return {p:sha((ROOT/p).read_bytes()) for p in SOURCES}


def gate():
    state = strict(GATE.read_bytes())
    need(state["status"] == "RUNNING", "FORMAL_GATE_REQUIRED")
    return state


def freeze(c):
    gate()
    RUN.mkdir(exist_ok=False)
    for name in ("raw","attempts","receipts"):
        (RUN/name).mkdir()
    plan = dict(source_sha256=identities(), requests=request_plan(c), cycles=schedule(c),
                frozen_at_utc=utc(time.time()*1000),
                old_gate_sha256={p:sha((ROOT/".artifacts"/p).read_bytes()) for p in GATES})
    save_new(RUN/"plan.json",plan)
    print(json.dumps(dict(requests=len(plan["requests"]),cycles=len(plan["cycles"]),sources=plan["source_sha256"])))


def load_plan(c):
    p = strict((RUN/"plan.json").read_bytes())
    need(p["source_sha256"]==identities() and p["requests"]==request_plan(c)
         and p["cycles"]==schedule(c), "FROZEN_INPUT_CHANGED")
    need(all(sha((ROOT/".artifacts"/k).read_bytes())==v for k,v in p["old_gate_sha256"].items()), "OLD_GATE_CHANGED")
    return p


def collect(c):
    state = gate()
    p = load_plan(c)
    b = c["budget"]
    attempts = list((RUN/"attempts").glob("*.json"))
    if attempts:
        need(state["active"].get("review_path"), "REVIEW_REQUIRED")
    else:
        save_new(RUN/"collection-start.json",dict(time=time.time()))
    start = strict((RUN/"collection-start.json").read_bytes())["time"]
    opener, prev, count = transport(), 0.0, len(attempts)
    for i, req in enumerate(p["requests"]):
        receipt_path = RUN/"receipts"/f"{i:04d}.json"
        if receipt_path.exists():
            cached = strict(receipt_path.read_bytes())
            need(cached["success"] is True, "FAILED_RECEIPT_NOT_REPLACED")
            raw = (RUN/"raw"/cached["raw_name"]).read_bytes()
            need(sha(raw)==cached["sha256"], "RAW_CHANGED")
            validate_page(raw,req)
            continue
        need(count<b["batch_market_gets"] and time.time()-start<b["collection_seconds"], "COLLECTION_BUDGET")
        time.sleep(max(0,b["request_gap_seconds"]-(time.monotonic()-prev)))
        count += 1
        raw_name = f"{count:04d}.bin"
        save_new(RUN/"attempts"/f"{count:04d}.json",dict(request=req,at=utc(time.time()*1000)))
        raw = b""
        receipt = dict(url=req["url"],success=False,raw_name=raw_name)
        try:
            with opener.open(urllib.request.Request(req["url"],headers={"User-Agent":"ai-trade-fixed-unlock/1"}),
                             timeout=b["request_timeout_seconds"]) as response:
                receipt["http_status"] = response.status
                raw = response.read(b["response_bytes"]+1)
            need(len(raw)<=b["response_bytes"], "RESPONSE_SIZE")
            need(sum(x.stat().st_size for x in (RUN/"raw").glob("*.bin"))+len(raw)<=b["total_bytes"], "TOTAL_BYTES")
            rows = validate_page(raw,req)
            receipt.update(success=True,rows=len(rows))
        except Exception as exc:
            if isinstance(exc,urllib.error.HTTPError):
                receipt["http_status"] = exc.code
                raw = exc.read(b["response_bytes"]+1)
            receipt.update(error_type=type(exc).__name__,error=str(exc)[:1000])
            raise
        finally:
            with (RUN/"raw"/raw_name).open("xb") as f:
                f.write(raw)
            receipt.update(sha256=sha(raw),bytes=len(raw),finished_at=utc(time.time()*1000))
            save_new(receipt_path,receipt)
            prev = time.monotonic()
        print("PUBLIC_GET",count,req["kind"],receipt["rows"],flush=True)
    save_new(RUN/"collection-complete.json",dict(attempts=count,elapsed_seconds=time.time()-start))


def funding_coverage(c, rates):
    ts = sorted(rates)
    a,b = ms(c["data_start"]),ms(c["data_end"])
    need(ts and ts[0]-a<8*HOUR and b-ts[-1]<=8*HOUR, "FUNDING_EDGE_GAP")
    need(all((v-u)/HOUR in c["funding"]["possible_interval_hours"] for u,v in zip(ts,ts[1:])), "FUNDING_GAP")


def inputs(c):
    p = load_plan(c)
    need((RUN/"collection-complete.json").exists(), "COLLECTION_INCOMPLETE")
    data = {k:{} for k in ENDPOINTS}
    for i,req in enumerate(p["requests"]):
        rec = strict((RUN/"receipts"/f"{i:04d}.json").read_bytes())
        raw = (RUN/"raw"/rec["raw_name"]).read_bytes()
        need(rec["success"] is True and rec["url"]==req["url"] and rec["sha256"]==sha(raw), "SOURCE_IDENTITY")
        for r in validate_page(raw,req):
            fund = req["kind"]=="funding"
            t = stamp(r["fundingRateTimestamp"] if fund else r[0])
            need(t not in data[req["kind"]], "DUPLICATE_PAGE_ROW")
            data[req["kind"]][t] = number(r["fundingRate"]) if fund else tuple(map(number,r[1:5]))
    for k in ("trade","mark"):
        need(sorted(data[k])==list(range(ms(c["data_start"]),ms(c["data_end"]),STEP)), "FULL_GRID")
    funding_coverage(c,data["funding"])
    return data


class Ledger:
    def __init__(self):
        self.lo = self.hi = 1.0
        self.q = self.entry = self.gross = self.fees = self.slippage = self.turnover = 0.0
        self.fund_lo = self.fund_hi = 0.0
        self.events,self.settlements = [],[]

    def enter(self,t,q,mid,slip,fee):
        need(self.q==0 and q<0, "SHORT_ENTRY_STATE")
        price = mid*(1-slip)
        cost = abs(q)*price*fee
        self.q,self.entry = q,price
        self.lo -= cost; self.hi -= cost; self.fees += cost
        self.slippage += abs(q)*(mid-price); self.turnover += abs(q)*price
        self.events.append(dict(t=t,action="entry",q=q,price=price,fee=cost))

    def close(self,t,mid,slip,fee):
        need(self.q<0, "SHORT_EXIT_STATE")
        price = mid*(1+slip)
        gross,cost = self.q*(price-self.entry),abs(self.q)*price*fee
        self.lo += gross-cost; self.hi += gross-cost
        self.gross += gross; self.fees += cost
        self.slippage += abs(self.q)*(price-mid); self.turnover += abs(self.q)*price
        self.events.append(dict(t=t,action="exit",q=self.q,price=price,fee=cost))
        self.q = self.entry = 0.0

    def settle(self,t,rate,bar):
        if self.q:
            a,b = -self.q*rate*bar[1],-self.q*rate*bar[2]
            lo,hi = min(a,b),max(a,b)
            self.lo += lo; self.hi += hi; self.fund_lo += lo; self.fund_hi += hi
            self.settlements.append(dict(t=t,q=self.q,lo=lo,hi=hi))

    def nav(self,price):
        pnl = self.q*(price-self.entry)
        return self.lo+pnl,self.hi+pnl

    def export(self):
        need(abs(self.lo-(1+self.gross-self.fees+self.fund_lo))<1e-10, "CASH_IDENTITY")
        need(abs(self.hi-(1+self.gross-self.fees+self.fund_hi))<1e-10, "CASH_IDENTITY")
        return dict(cash_lo=self.lo,cash_hi=self.hi,q=self.q,entry_price=self.entry,
                    gross_after_slippage=self.gross,fees=self.fees,slippage=self.slippage,
                    funding_lo=self.fund_lo,funding_hi=self.fund_hi,turnover=self.turnover,
                    events=self.events,settlements=self.settlements)


class Risk:
    def __init__(self,c):
        self.c = c
        self.peak_lo = self.peak_hi = 1.0
        self.dd_lo = self.dd_hi = self.notional = 0.0

    def observe(self,ledger,low,high):
        # Short: high price is the NAV trough; low price is the NAV peak.
        trough_lo,trough_hi = ledger.nav(high)
        peak_lo,peak_hi = ledger.nav(low)
        lo = max(0.,1-trough_hi/self.peak_lo)
        hi = max(0.,1-trough_lo/max(self.peak_hi,peak_hi))
        self.dd_lo = max(self.dd_lo,lo); self.dd_hi = max(self.dd_hi,hi)
        self.notional = max(self.notional,abs(ledger.q)*high)
        if abs(ledger.q)*high>self.c["max_notional"] or lo>=self.c["reference_max_drawdown"]:
            return "REJECT","DEFINITE_REFERENCE_RISK_BREACH"
        if hi>=self.c["reference_max_drawdown"]:
            return "INSUFFICIENT_EVIDENCE","INTRABAR_OR_FUNDING_RISK_AMBIGUITY"
        self.peak_lo = max(self.peak_lo,peak_lo); self.peak_hi = max(self.peak_hi,peak_hi)


def simulate(c,data,cycles,deadline=math.inf):
    entries = {s["entry"]:s for s in cycles}
    exits = {s["exit"] for s in cycles}; ends = {s["end"] for s in cycles}
    need(not ((set(entries)|exits|ends)&set(data["funding"])), "FUNDING_EXECUTION_COLLISION")
    a,b,risk = Ledger(),Ledger(),Risk(c)
    fee,slip = c["taker_fee_bps_per_side"]/10000,c["adverse_slippage_bps_per_side"]/10000
    samples,previous = [],(1.,1.,1.,1.)
    qty_time,control_time = 0.,0.
    first,final = cycles[0]["entry"],cycles[-1]["end"]
    def report(t,verdict=None):
        return dict(full_window_evaluated=verdict is None,last_bar=t,
                    decision=verdict[0] if verdict else None,reason=verdict[1] if verdict else None,
                    candidate=a.export(),control=b.export(),cycles=samples,
                    dd_lo=risk.dd_lo,dd_hi=risk.dd_hi,max_notional=risk.notional,
                    planned_coin_ms=qty_time,planned_control_coin_ms=control_time,
                    stop_fill_simulated=False,profitability_qualified=False,promotion_authority=False)
    for i,t in enumerate(range(first,final+STEP,STEP)):
        if i%1000==0: need(time.monotonic()<deadline, "COMPUTE_BUDGET")
        bar = data["mark"][t]; mid = data["trade"][t][0]
        if t in data["funding"]:
            a.settle(t,data["funding"][t],bar); b.settle(t,data["funding"][t],bar)
        verdict = risk.observe(a,bar[0],bar[0])
        if verdict: return report(t,verdict)
        if t in exits: a.close(t,mid,slip,fee)
        if t in ends:
            b.close(t,mid,0,0)
            nav = (a.lo,a.hi,b.lo,b.hi)
            p = tuple(x-y for x,y in zip(nav,previous))
            samples.append(dict(end=t,net_lo=p[0],net_hi=p[1],control_lo=p[2],control_hi=p[3],
                                timing_lo=p[0]-p[3],timing_hi=p[1]-p[2]))
            previous = nav
        if t in entries:
            s = entries[t]; q = -c["entry_notional"]/(mid*(1-slip))
            a.enter(t,q,mid,slip,fee); b.enter(t,q*s["rho"],mid,0,0)
            qty_time += abs(q)*(s["exit"]-t)
            control_time += abs(q)*s["rho"]*(s["end"]-t)
        verdict = risk.observe(a,bar[2],bar[1])
        if verdict: return report(t,verdict)
    need(a.q==b.q==0 and len(samples)==len(cycles), "TERMINAL_FLAT")
    need(math.isclose(qty_time,control_time,rel_tol=1e-12), "COIN_TIME_MATCH")
    need(abs(sum(s["net_lo"] for s in samples)-(a.lo-1))<1e-10, "CYCLE_CASH")
    return report(final)


def statistics(r,c,deadline=math.inf):
    if not r["full_window_evaluated"]: return r
    spec,rows = c["statistics"],r["cycles"]
    if len(rows)<spec["minimum_cycles"]:
        return {**r,"decision":"INSUFFICIENT_EVIDENCE","reason":"CYCLE_COUNT"}
    n = len(rows); results = []
    keys = ("net_lo","net_hi","timing_lo","timing_hi")
    for block in spec["circular_block_cycles"]:
        need(0<block<=n, "BLOCK_LENGTH")
        rng = random.Random(spec["seed"]+block)
        draws = {k:[] for k in keys}
        for rep in range(spec["replicates"]):
            if rep%256==0: need(time.monotonic()<deadline, "COMPUTE_BUDGET")
            indices = []
            while len(indices)<n:
                start = rng.randrange(n)
                indices.extend((start+j)%n for j in range(min(block,n-len(indices))))
            for k in keys: draws[k].append(sum(rows[i][k] for i in indices)/n)
        results.append(dict(block_cycles=block,**{k:quantile(draws[k],spec["lower_quantile"] if k.endswith("lo")
                                                             else spec["upper_quantile"]) for k in keys}))
    if max(x["net_hi"] for x in results)<=0: decision,reason="REJECT","NONPOSITIVE_NET_UPPER_BOUND"
    elif all(x["net_lo"]>0 and x["timing_lo"]>0 for x in results):
        decision,reason="WORTH_FURTHER_REVIEW","NET_AND_TIMING_LOWER_POSITIVE"
    else: decision,reason="INSUFFICIENT_EVIDENCE","NET_OR_TIMING_LOWER_NOT_POSITIVE"
    return {**r,"bootstrap":results,"decision":decision,"reason":reason}


def audit_cash(r,c,data,cycles):
    """Rebuild expected events from calendar, not from the strategy's event list."""
    D = lambda x:Decimal(str(x))
    final = r["last_bar"]
    checked = {}
    for name in ("candidate","control"):
        slip = D(c["adverse_slippage_bps_per_side"])/10000 if name=="candidate" else D(0)
        fee = D(c["taker_fee_bps_per_side"])/10000 if name=="candidate" else D(0)
        lo=hi=D(1); total_fees=gross=fund_lo=fund_hi=q=entry=D(0)
        expected = []
        for s in cycles:
            if s["entry"]>final: continue
            expected.append((s["entry"],"entry",s))
            exit_t = s["exit"] if name=="candidate" else s["end"]
            if exit_t<=final: expected.append((exit_t,"exit",s))
        actual = r[name]["events"]
        # If risk stops at the open before a scheduled action, do not invent that action.
        if not r["full_window_evaluated"]:
            expected = [x for x in expected if x[0]<final] + [x for x in expected if x[0]==final
                         and any(e["t"]==x[0] and e["action"]==x[1] for e in actual)]
        need([(e["t"],e["action"]) for e in actual]==[(t,a) for t,a,s in expected], "AUDIT_CALENDAR_EVENTS")
        event_map = {}
        for i,(t,action,s) in enumerate(expected): event_map.setdefault(t,[]).append((action,s,actual[i]))
        expected_fund = []
        ts = sorted(set(event_map)|{t for t in data["funding"] if cycles[0]["entry"]<=t<=final})
        for t in ts:
            if t in data["funding"] and q:
                bar=data["mark"][t]; rate=D(data["funding"][t])
                x,y = -q*rate*D(bar[1]),-q*rate*D(bar[2]); l,h=min(x,y),max(x,y)
                lo+=l;hi+=h;fund_lo+=l;fund_hi+=h
                expected_fund.append((t,l,h))
            for action,s,event in event_map.get(t,[]):
                mid=D(data["trade"][t][0])
                if action=="entry":
                    need(q==0, "AUDIT_ENTRY_STATE")
                    q=-D(c["entry_notional"])/(mid*(1-D(c["adverse_slippage_bps_per_side"])/10000))
                    if name=="control": q*=D(s["exit"]-t)/D(s["end"]-t)
                    entry=price=mid*(1-slip)
                else:
                    need(q<0, "AUDIT_EXIT_STATE")
                    price=mid*(1+slip);pnl=q*(price-entry)
                    lo+=pnl;hi+=pnl;gross+=pnl
                cost=abs(q)*price*fee;lo-=cost;hi-=cost;total_fees+=cost
                need(abs(q-D(event["q"]))<D("1e-10") and abs(price-D(event["price"]))<D("1e-10")
                     and abs(cost-D(event["fee"]))<D("1e-10"), "AUDIT_EXECUTION")
                if action=="exit":q=entry=D(0)
        need(len(expected_fund)==len(r[name]["settlements"]), "AUDIT_FUND_COUNT")
        for (t,l,h),e in zip(expected_fund,r[name]["settlements"]):
            need(t==e["t"] and abs(l-D(e["lo"]))<D("1e-10") and abs(h-D(e["hi"]))<D("1e-10"), "AUDIT_FUND_CASH")
        values=dict(cash_lo=lo,cash_hi=hi,fees=total_fees,gross_after_slippage=gross,
                    funding_lo=fund_lo,funding_hi=fund_hi,q=q)
        for k,v in values.items():need(abs(D(r[name][k])-v)<D("1e-10"), "AUDIT_"+k)
        checked[name]={k:str(v) for k,v in values.items()}
    return dict(status="PASS",method="independent Decimal calendar and settlement reconstruction",checks=checked,
                strategy_rerun=False,profitability_qualified=False)


def audit_statistics(r,c):
    if not r["full_window_evaluated"]:
        need("bootstrap" not in r, "NO_POST_STOP_STATISTICS")
        return dict(status="NOT_APPLICABLE_RISK_STOP")
    rows=r["cycles"];spec=c["statistics"];n=len(rows)
    for key,ledger_key in (("net_lo","cash_lo"),("net_hi","cash_hi")):
        need(abs(sum(x[key] for x in rows)-(r["candidate"][ledger_key]-1))<1e-10,"AUDIT_CYCLE_CASH")
    if n<spec["minimum_cycles"]:
        need(r["reason"]=="CYCLE_COUNT", "AUDIT_SAMPLE_FLOOR")
        return dict(status="PASS_SAMPLE_FLOOR")
    for result in r["bootstrap"]:
        block=result["block_cycles"];rng=random.Random(spec["seed"]+block)
        keys=("net_lo","net_hi","timing_lo","timing_hi")
        prefixes={}
        for k in keys:
            acc=[0.]
            for row in rows+rows:acc.append(acc[-1]+row[k])
            prefixes[k]=acc
        draws={k:[] for k in keys}
        for _ in range(spec["replicates"]):
            spans=[(rng.randrange(n),min(block,n-offset)) for offset in range(0,n,block)]
            for k in keys:
                p=prefixes[k];draws[k].append(sum(p[a+b]-p[a] for a,b in spans)/n)
        for k,values in draws.items():
            values.sort();p=spec["lower_quantile"] if k.endswith("lo") else spec["upper_quantile"]
            index=(len(values)-1)*p;left=math.floor(index);right=math.ceil(index)
            value=values[left]*(1-(index-left))+values[right]*(index-left)
            need(abs(value-result[k])<1e-10,"AUDIT_BOOTSTRAP")
    return dict(status="PASS",method="independent circular-prefix sums and quantile interpolation",
                economic_rule_rerun=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("freeze","collect","inspect","screen","audit"))
    args=parser.parse_args();c=contract();gate()
    if args.action=="freeze":freeze(c);return
    if args.action=="collect":collect(c);return
    data=inputs(c)
    if args.action=="inspect":
        report={k:len(v) for k,v in data.items()}
        report["funding_intervals_hours"]=sorted({(b-a)/HOUR for a,b in zip(sorted(data["funding"]),sorted(data["funding"])[1:])})
        save_new(RUN/"inspection.json",report)
    elif args.action=="screen":
        save_new(RUN/"economic-start.json",dict(started_at=utc(time.time()*1000),maximum_experiments=1))
        start=time.monotonic();deadline=start+c["budget"]["compute_seconds"]
        report=statistics(simulate(c,data,schedule(c),deadline),c,deadline)
        report.update(compute_seconds=time.monotonic()-start,source_sha256=identities())
        save_new(RUN/"result.json",report)
        report={k:report[k] for k in ("decision","reason","full_window_evaluated","last_bar","compute_seconds")}
    else:
        raw=(RUN/"result.json").read_bytes();r=strict(raw)
        need(r["source_sha256"]==identities(), "RESULT_SOURCE")
        report=audit_cash(r,c,data,schedule(c));report["result_sha256"]=sha(raw)
        report["statistics_audit"]=audit_statistics(r,c)
        save_new(RUN/"cash-audit.json",report)
    print(json.dumps(report,sort_keys=True))


if __name__=="__main__":main()
