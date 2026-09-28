#!/usr/bin/env python3
"""Read-only reconstruction of the same stopped path; no later prices or strategy."""
from decimal import Decimal
import json
import screen_token_unlock as m


def main():
    m.gate();c=m.contract();data=m.inputs(c)
    r=m.strict((m.RUN/"result.json").read_bytes())
    audit=m.strict((m.RUN/"cash-audit.json").read_bytes())
    m.need(audit["result_sha256"]==m.sha((m.RUN/"result.json").read_bytes()) and audit["status"]=="PASS","CASH_AUDIT_REQUIRED")
    m.need(not r["full_window_evaluated"] and r["reason"]=="DEFINITE_REFERENCE_RISK_BREACH","FIXED_STOP_CASE")
    D=lambda x:Decimal(str(x))
    cash_lo=cash_hi=peak_lo=peak_hi=D(1)
    q=entry=max_dd_lo=max_dd_hi=D(0)
    events={}
    for e in r["candidate"]["events"]:events.setdefault(e["t"],[]).append(e)
    def observe(t,phase,low,high):
        nonlocal peak_lo,peak_hi,max_dd_lo,max_dd_hi
        worst_lo,worst_hi=cash_lo+q*(high-entry),cash_hi+q*(high-entry)
        best_lo,best_hi=cash_lo+q*(low-entry),cash_hi+q*(low-entry)
        lo=max(D(0),1-worst_hi/peak_lo)
        hi=max(D(0),1-worst_lo/max(peak_hi,best_hi))
        max_dd_lo=max(max_dd_lo,lo);max_dd_hi=max(max_dd_hi,hi)
        definite=lo>=D(c["reference_max_drawdown"]) or abs(q)*high>D(c["max_notional"])
        possible=hi>=D(c["reference_max_drawdown"])
        if definite or possible:
            m.need(t==r["last_bar"] and definite,"FIRST_STOP_DISAGREES")
            m.need(abs(max_dd_lo-D(r["dd_lo"]))<D("1e-10") and abs(max_dd_hi-D(r["dd_hi"]))<D("1e-10"),"RISK_BOUND_MISMATCH")
            result=dict(status="PASS",first_stop_utc=m.utc(t),phase=phase,decision="REJECT",
                        drawdown_lower=str(max_dd_lo),drawdown_upper=str(max_dd_hi),
                        prior_peak_lower=str(peak_lo),prior_peak_upper=str(peak_hi),
                        observed_nav_lower=str(worst_lo),observed_nav_upper=str(worst_hi),
                        short_entry_price=str(entry),short_quantity=str(q),
                        observed_price_low=str(low),observed_price_high=str(high),
                        result_sha256=m.sha((m.RUN/"result.json").read_bytes()),
                        method="Decimal marked equity from independently verified events; prior peak lower versus current trough upper",
                        later_path_evaluated=False,strategy_rerun=False)
            m.save_new(m.BATCH/"risk-stop-audit.json",result)
            print(json.dumps(result,indent=2));return True
        peak_lo=max(peak_lo,best_lo);peak_hi=max(peak_hi,best_hi)
        return False
    for t in range(m.schedule(c)[0]["entry"],r["last_bar"]+m.STEP,m.STEP):
        bar=tuple(D(x) for x in data["mark"][t])
        if t in data["funding"] and q:
            rate=D(data["funding"][t]);a,b=-q*rate*bar[1],-q*rate*bar[2]
            cash_lo+=min(a,b);cash_hi+=max(a,b)
        if observe(t,"bar_open_before_action",bar[0],bar[0]):return
        for e in events.get(t,[]):
            if e["action"]=="entry":q,entry=D(e["q"]),D(e["price"])
            else:
                pnl=q*(D(e["price"])-entry);cash_lo+=pnl;cash_hi+=pnl
                q=entry=D(0)
            cost=D(e["fee"]);cash_lo-=cost;cash_hi-=cost
        if observe(t,"intrabar_after_action",bar[2],bar[1]):return
    raise ValueError("STOP_NOT_RECONSTRUCTED")


if __name__=="__main__":main()
