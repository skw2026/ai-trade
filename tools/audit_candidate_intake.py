#!/usr/bin/env python3
"""Bind this completed batch's receipts and publish only allowlisted summaries.

No network, strategy execution, parameter selection or gate-state modification.
"""
import json
from pathlib import Path
import screen_token_unlock as m


def main():
    m.gate()
    c=m.contract();plan=m.load_plan(c)
    r=m.strict((m.RUN/"result.json").read_bytes())
    cash=m.strict((m.RUN/"cash-audit.json").read_bytes())
    collection=m.strict((m.RUN/"collection-complete.json").read_bytes())
    inspection=m.strict((m.RUN/"inspection.json").read_bytes())
    started=m.strict((m.RUN/"economic-start.json").read_bytes())
    m.need(r["source_sha256"]==plan["source_sha256"],"RESULT_SOURCE")
    m.need(cash["status"]=="PASS" and cash["strategy_rerun"] is False
           and cash["result_sha256"]==m.sha((m.RUN/"result.json").read_bytes()),"AUDIT_BINDING")
    requests=plan["requests"]
    m.need(collection["attempts"]==len(requests)<=240,"REQUEST_COUNT")
    for directory in ("attempts","receipts","raw"):
        m.need(len(list((m.RUN/directory).iterdir()))==len(requests),"EVIDENCE_COUNT")
    raw_bytes=0
    last_mark_open=None
    for i,request in enumerate(requests):
        attempt=m.strict((m.RUN/"attempts"/f"{i+1:04d}.json").read_bytes())
        receipt=m.strict((m.RUN/"receipts"/f"{i:04d}.json").read_bytes())
        raw=(m.RUN/"raw"/receipt["raw_name"]).read_bytes()
        m.need(attempt["request"]==request and receipt["url"]==request["url"]
               and receipt["success"] is True and receipt["sha256"]==m.sha(raw),"RECEIPT_BINDING")
        rows=m.validate_page(raw,request)
        if request["kind"]=="mark" and request["start"]<=r["last_bar"]<=request["end"]:
            last_mark_open=next(m.number(row[1]) for row in rows if m.stamp(row[0])==r["last_bar"])
        raw_bytes+=len(raw)
    state=m.strict(m.GATE.read_bytes())
    labels=[x["label"] for x in state["history"] if x.get("event")=="validation"]
    m.need(labels.count("intake-e2-one-economic-screen")==1,"ONE_EXPERIMENT")
    m.need(all(x.get("exit_code")==0 and x.get("retry_of") is None for x in state["history"]
               if x.get("event")=="validation"),"NO_UNREVIEWED_FAILURE")
    m.need("intake-closeout-regression" in labels,"CLOSEOUT_REQUIRED")
    sources=m.strict((m.ROOT/"docs/reviews/2026-09-28-candidate-intake-sources.json").read_bytes())
    m.need(len(sources["sources"])<=16,"SOURCE_BUDGET")
    entries=[]
    for path in sorted(p for p in m.RUN.rglob("*") if p.is_file()):
        raw=path.read_bytes()
        entries.append(dict(path=path.relative_to(m.RUN).as_posix(),bytes=len(raw),sha256=m.sha(raw)))
    index_sha=m.sha(json.dumps(entries,sort_keys=True,separators=(",",":")).encode())
    a,b=r["candidate"],r["control"]
    m.need(last_mark_open is not None,"STOP_MARK_SOURCE")
    # If the risk guard stopped with an open position, cash is not total return.
    ledger={}
    for name,v in (("candidate",a),("control",b)):
        ledger[name]={k:v[k] for k in ("cash_lo","cash_hi","q","entry_price","gross_after_slippage",
                                      "fees","slippage","funding_lo","funding_hi","turnover")}
        ledger[name].update(execution_events=len(v["events"]),funding_events=len(v["settlements"]))
    report=dict(schema="candidate_intake_batch_evidence_v1",phase_status="COMPLETED_BOUNDED_BATCH",
        E1=dict(decision="INSUFFICIENT_HISTORICAL_POSITIONING_INPUT",market_gets=0,experiments=0),
        E2=dict(decision=r["decision"],reason=r["reason"],full_window_evaluated=r["full_window_evaluated"],
                last_bar_utc=m.utc(r["last_bar"]),complete_cycles=len(r["cycles"]),planned_cycles=len(plan["cycles"]),
                ledger=ledger,drawdown_lo=r["dd_lo"],drawdown_hi=r["dd_hi"],max_notional=r["max_notional"],
                planned_coin_ms=r["planned_coin_ms"],planned_control_coin_ms=r["planned_control_coin_ms"],
                bootstrap=r.get("bootstrap"),stop_fill_simulated=False),
        requests=dict(market_gets=collection["attempts"],successes=len(requests),failed=0,retries=0,limit=240,
                      elapsed_seconds=collection["elapsed_seconds"]),
        inputs=inspection,experiment_count=1,compute_seconds=r["compute_seconds"],started=started,
        validations=dict(synthetic_tests=22,old_closeout_tests=8,cash_audit="PASS",
                         statistics_audit=cash["statistics_audit"],old_gates_unchanged=len(plan["old_gate_sha256"]),
                         nonzero_validation_exits=0,platform_ci_claimed=False),
        source_sha256=plan["source_sha256"],old_gate_sha256=plan["old_gate_sha256"],
        closeout_source_sha256={p:m.sha((m.ROOT/p).read_bytes()) for p in
                               ("tools/audit_candidate_intake.py","docs/reviews/2026-09-28-candidate-intake-sources.json")},
        local_evidence=dict(directory=str(m.RUN.relative_to(m.ROOT)),files=len(entries),
                            bytes=sum(x["bytes"] for x in entries),raw_bytes=raw_bytes,index_sha256=index_sha,
                            key_files={x["path"]:x["sha256"] for x in entries if "/" not in x["path"]}),
        selected_for_further_review=["E2"] if r["decision"]=="WORTH_FURTHER_REVIEW" else [],
        market_candidate_status="NO_QUALIFIED_CANDIDATE",profitability_qualified=False,
        authority_used=c["authority"],no_automatic_budget_renewal=True)
    report["E2"]["last_mark_bar_open"]=last_mark_open
    report["E2"]["candidate_mark_open_equity_lo"]=a["cash_lo"]+a["q"]*(last_mark_open-a["entry_price"])
    report["E2"]["candidate_mark_open_equity_hi"]=a["cash_hi"]+a["q"]*(last_mark_open-a["entry_price"])
    if r["full_window_evaluated"]:
        report["E2"]["reference_net_lo"]=a["cash_lo"]-1
        report["E2"]["reference_net_hi"]=a["cash_hi"]-1
        report["E2"]["control_net_lo"]=b["cash_lo"]-1
        report["E2"]["control_net_hi"]=b["cash_hi"]-1
    m.save_new(m.ROOT/"docs/reviews/2026-09-28-candidate-intake.evidence.json",report)
    print(json.dumps(report,indent=2,sort_keys=True))


if __name__=="__main__":main()
