#!/usr/bin/env python3
"""Read-only diagnosis of an existing screen's observed fill path.

Does not generate signals/orders, execute a strategy, alter artifacts, download
data, or inspect real accounts. Independently reconciles cash from exact logged
fills and the bound CSV, stopping at the FIRST existing reference boundary.
"""
import argparse
import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

import mvp_reference_pipeline as pipeline
from mvp_reference_inputs import require, strict_json
from mvp_reference_verdict import parse_events


def audit(plan_path):
    plan,directory=pipeline.verify(plan_path)
    result=strict_json((directory/"result.json").read_bytes())
    require(result["decision"]=="INSUFFICIENT" and len(result["receipts"])==1,"EXPECTED_ONE_STOPPED_BASE")
    require(pipeline.digest(directory/"base.log")==result["receipts"][0]["log_sha256"],"LOG_IDENTITY")
    text=(directory/"base.log").read_text();events=parse_events(text)
    require(events["stops"]==[{"reason":"INSUFFICIENT_ACCOUNTING_CONTROL_PATH"}],"EXPECTED_ACCOUNTING_STOP")
    sides={}
    for line in text.splitlines():
        if "FILL_APPLIED:" in line:
            match=re.search(r"fill_id=([^,]+).*?direction=(-?\d+),",line)
            require(match is not None,"DIRECTION_LOG_PARSE")
            require(match[1] not in sides,"DUPLICATE_DIRECTION")
            sides[match[1]]=int(match[2])
    require(set(sides)=={f["fill"] for f in events["fills"]},"DIRECTION_LOG_COVERAGE")
    by_time=defaultdict(list)
    for fill in events["fills"]:by_time[fill["ts"]].append(fill)
    bars={b["ts"]:b for b in events["bars"]}
    cash=10000.;qty=entry=collateral=fees=funding=realized=slippage=uncertainty=0.
    point_peak=upper_peak=10000.;max_dd=0.;matched=used_fills=0;max_equity_error=max_bound_error=0.
    stop=None

    def point(mark,ts,phase):
        nonlocal point_peak,upper_peak,max_dd,stop
        equity=cash+qty*(mark-entry)
        point_peak=max(point_peak,equity);upper_peak=max(upper_peak,equity+uncertainty)
        point_dd=(point_peak-equity)/point_peak
        upper_dd=(upper_peak-equity+uncertainty)/upper_peak
        max_dd=max(max_dd,upper_dd)
        boundary=((abs(qty)*entry-collateral)/(abs(qty)*(1-.0113)) if qty>0 else
                  (abs(qty)*entry+collateral)/(abs(qty)*(1+.0113))) if qty else 0.
        distance=((mark-boundary)/mark if qty>0 else (boundary-mark)/mark) if qty else 1.
        if upper_dd>=.08 or distance<.08:
            stop={"ts":ts,"utc":datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),"phase":phase,
                  "mark":mark,"cash":cash,"qty":qty,"entry":entry,"collateral":collateral,
                  "equity":equity,"point_peak":point_peak,"upper_peak":upper_peak,
                  "point_drawdown":point_dd,"drawdown_upper":upper_dd,"risk_distance":distance,
                  "funding_uncertainty":uncertainty,"fees":fees,"funding_paid":funding,
                  "realized_pnl_after_slippage_before_fees":realized,"unrealized_pnl":qty*(mark-entry),
                  "slippage_and_adverse_tick_cost_on_observed_fills":slippage,
                  "price_pnl_before_execution_costs_on_same_fill_path":realized+qty*(mark-entry)+slippage}
            return True
        return False

    with (directory/"replay.csv").open() as stream:
        for row in csv.DictReader(stream):
            ts=int(row["timestamp"])
            require(ts<=max(bars)+300000,"STOP_NOT_REPRODUCED_WITHIN_OBSERVED_PREFIX")
            mark=float(row["mark_open"]);opening=float(row["open"])
            if point(mark,ts,"open_before_funding"):break
            rate=float(row["funding_rate_per_interval"]);paid=qty*mark*rate
            pending_uncertainty=abs(qty*rate)*(float(row["mark_high"])-float(row["mark_low"]))
            cash-=paid;collateral-=paid;funding+=paid
            if point(mark,ts,"open_after_funding"):break
            for f in by_time[ts]:
                side=sides[f["fill"]];n=f["qty"];price=f["price"];fee=f["fee"]
                require(abs(fee-n*price*.00055)<1e-9,"FEE_FORMULA")
                delta=side*n;new_qty=qty+delta
                if qty*delta>=0:
                    collateral+=n*price/2-fee
                    entry=(abs(qty)*entry+n*price)/abs(new_qty)
                else:
                    require(n<=abs(qty)+1e-9,"OBSERVED_CROSS_ZERO")
                    pnl=n*(price-entry)*(1 if qty>0 else -1)
                    realized+=pnl;cash+=pnl
                    collateral*=max(0.,abs(new_qty)/abs(qty))
                    if abs(new_qty)<1e-8:entry=collateral=0.
                qty=0. if abs(new_qty)<1e-8 else new_qty
                cash-=fee;fees+=fee;slippage+=side*n*(price-opening);used_fills+=1
                if point(mark,ts,"after_fill"):break
            if stop:break
            mark=float(row["mark_close"])
            if point(mark,ts+300000,"close_before_alpha"):break
            uncertainty+=pending_uncertainty
            adverse=float(row["mark_low"] if qty>0 else row["mark_high"])
            favorable=float(row["mark_high"] if qty>0 else row["mark_low"])
            upper_peak=max(upper_peak,cash+qty*(favorable-entry)+uncertainty)
            max_dd=max(max_dd,(upper_peak-cash-qty*(adverse-entry)+uncertainty)/upper_peak)
            require(max_dd<.08,"DIFFERENT_EARLIER_INTRABAR_STOP")
            observed=bars[ts+300000];equity=cash+qty*(mark-entry)
            max_equity_error=max(max_equity_error,abs(equity-observed["equity"]))
            max_bound_error=max(max_bound_error,abs(max_dd-observed["drawdown_upper"]))
            require(abs(equity-observed["equity"])<1e-7 and
                    abs(max_dd-observed["drawdown_upper"])<1e-10 and
                    abs(uncertainty-observed["funding_uncertainty"])<1e-8,"OBSERVED_LEDGER_MISMATCH")
            matched+=1
    require(stop is not None and matched==len(bars) and used_fills==len(events["fills"]),"PREFIX_RECONCILIATION_INCOMPLETE")
    return {"diagnostic_only":True,"new_economic_experiment":False,"strategy_rerun":False,
            "plan_sha256":pipeline.digest(plan_path),"log_sha256":pipeline.digest(directory/"base.log"),
            "matched_closed_bars":matched,"matched_fill_parts":used_fills,
            "distinct_filled_orders":len({f["order"] for f in events["fills"]}),
            "max_equity_reconciliation_error":max_equity_error,"max_drawdown_bound_reconciliation_error":max_bound_error,
            "stop":stop,"annual_result_available":False,"stress_executed":False}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--plan",required=True)
    print(json.dumps(audit(p.parse_args().plan),indent=2,sort_keys=True))
