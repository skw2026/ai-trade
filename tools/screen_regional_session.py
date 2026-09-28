#!/usr/bin/env python3
"""One frozen, reference-only regional-session screen. No account/trading API.

CLI paths and contract are fixed. Synthetic tests call pure functions directly.
Collection, one-shot computation and independent cash audit are separate steps.
"""
import argparse
import datetime as dt
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/contracts/regional_session_screen_v1.json"
CONTRACT_SHA = "43eb4286376a0f3aea685a94395e7c299bb1fc74ada85091c39238f99affff76"
BATCH = ROOT / ".artifacts/regional-session-screen-20260928"
RUN = BATCH / "run"
GATE = BATCH / "validation-state.json"
STEP = 300000
DAY = 86400000
HOUR = 3600000
OLD_GATES = ["validation-gate/state.json"] + [x + "/validation-state.json" for x in (
    "engineering-freeze-20260921", "offline-learning-loop-20260922",
    "strict-learning-20260923", "safety-withdrawal-20260923",
    "safety-promotion-20260924", "candidate-governance-20260928", "regional-session-20260928")]
ENDPOINTS = {"trade": "/v5/market/kline", "mark": "/v5/market/mark-price-kline",
             "funding": "/v5/market/funding/history"}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            need(k not in result, "DUPLICATE_JSON_KEY")
            result[k] = v
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))


def number(x):
    need(not isinstance(x, bool), "BOOLEAN_NOT_NUMBER")
    value = float(x)
    need(math.isfinite(value), "NONFINITE_NUMBER")
    return value


def stamp(x):
    need(type(x) is int or isinstance(x, str) and x.isdigit(), "INVALID_TIMESTAMP")
    return int(x)


def ms(value):
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def utc(value):
    return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).isoformat()


def save_new(path, value):
    # Evidence is append-only, including an interrupted computation marker.
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
        f.write("\n")
        f.flush()
        import os
        os.fsync(f.fileno())


def contract():
    raw = CONTRACT.read_bytes()
    need(sha(raw) == CONTRACT_SHA, "FROZEN_CONTRACT_CHANGED")
    c = strict(raw)
    need(all(v is False for v in c["authority"].values()), "ACTUATION_FORBIDDEN")
    return c


def cycles(c):
    holidays = set(c["calendar"]["holidays_2026"])
    first, final = ms(c["first_entry_utc"]), ms(c["final_cycle_boundary_utc"])
    start_day = first // DAY * DAY
    entries = []
    for t in range(start_day, final + DAY, DAY):
        d = dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).date()
        if d.weekday() < 5 and d.isoformat() not in holidays:
            entry = t + 6 * HOUR + 35 * 60000
            if first <= entry <= final:
                entries.append(entry)
    need(len(entries) >= 2 and entries[0] == first and entries[-1] == final,
         "INCOMPLETE_CALENDAR_BOUNDARIES")
    return [{"entry": a, "exit": b // DAY * DAY + 5 * 60000,
             "end": b, "rho": (b // DAY * DAY + 5 * 60000 - a) / (b - a)}
            for a, b in zip(entries, entries[1:])]


def requests(c):
    start, end = ms(c["data_start_utc"]), ms(c["data_end_exclusive_utc"])
    need(start % STEP == end % STEP == 0 and start < end, "DATA_GRID")
    result = []
    for kind in ("funding", "trade", "mark"):
        funding = kind == "funding"
        step, limit = (c["cost"]["required_funding_grid_ms"], 200) if funding else (STEP, 1000)
        for a in range(start, end, step * limit):
            b = min(end, a + step * limit) - 1
            params = {"category": "linear", "symbol": "BTCUSDT", "limit": limit,
                      "startTime" if funding else "start": a,
                      "endTime" if funding else "end": b}
            if not funding:
                params["interval"] = "5"
            url = c["public_host"] + ENDPOINTS[kind] + "?" + urllib.parse.urlencode(params)
            result.append({"kind": kind, "start": a, "end": b, "step": step,
                           "limit": limit, "url": url, "key": sha(url.encode())})
    need(len(result) <= c["budget"]["maximum_public_gets"], "REQUEST_PLAN_OVER_BUDGET")
    return result


def validate_page(raw, request):
    data = strict(raw)
    need(type(data.get("retCode")) is int and data["retCode"] == 0, "API_RETCODE")
    result = data["result"]
    need(result.get("category") == "linear", "WRONG_CATEGORY")
    funding = request["kind"] == "funding"
    if not funding:
        need(result.get("symbol") == "BTCUSDT", "WRONG_SYMBOL")
    rows = result["list"]
    need(isinstance(rows, list) and len(rows) <= request["limit"], "PAGE_LENGTH")
    times = []
    for r in rows:
        if funding:
            need(r.get("symbol") == "BTCUSDT", "WRONG_FUNDING_SYMBOL")
            times.append(stamp(r["fundingRateTimestamp"]))
            number(r["fundingRate"])
        else:
            need(isinstance(r, list) and len(r) == (7 if request["kind"] == "trade" else 5), "ROW_LENGTH")
            times.append(stamp(r[0]))
            o, h, l, close = map(number, r[1:5])
            need(0 < l <= min(o, close) <= max(o, close) <= h, "OHLC_BOUNDS")
            if request["kind"] == "trade":
                need(number(r[5]) >= 0 and number(r[6]) >= 0, "NEGATIVE_VOLUME")
    step = request["step"]
    first = (request["start"] + step - 1) // step * step
    need(times == list(range(first, request["end"] + 1, step))[::-1], "PAGE_GRID_OR_COVERAGE")
    return rows


def source_hashes():
    paths = ["tools/screen_regional_session.py", "tools/test_screen_regional_session.py",
             "docs/contracts/regional_session_screen_v1.json"]
    return {p: sha((ROOT / p).read_bytes()) for p in paths}


def check_gate():
    state = strict(GATE.read_bytes())
    need(state["status"] == "RUNNING", "FORMAL_GATE_REQUIRED")
    return state


def freeze(c):
    check_gate()
    RUN.mkdir(exist_ok=False)
    for name in ("raw", "attempts", "receipts", "pages"):
        (RUN / name).mkdir()
    plan = {"contract_sha256": CONTRACT_SHA, "source_sha256": source_hashes(),
            "frozen_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requests": requests(c), "cycles": cycles(c),
            "old_gate_sha256": {p: sha((ROOT / ".artifacts" / p).read_bytes()) for p in OLD_GATES}}
    save_new(RUN / "plan.json", plan)
    print(json.dumps({"frozen": True, "requests": len(plan["requests"]), "cycles": len(plan["cycles"]),
                      "contract_sha256": CONTRACT_SHA}))


def load_plan(c):
    plan = strict((RUN / "plan.json").read_bytes())
    need(plan["contract_sha256"] == CONTRACT_SHA and plan["source_sha256"] == source_hashes(),
         "FROZEN_SOURCE_CHANGED")
    need(plan["requests"] == requests(c) and plan["cycles"] == cycles(c), "PLAN_CHANGED")
    need(all(sha((ROOT / ".artifacts" / p).read_bytes()) == v
             for p, v in plan["old_gate_sha256"].items()), "OLD_GATE_CHANGED")
    return plan


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("REDIRECT_FORBIDDEN")


def transport():
    ca = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").is_file() else None
    ctx = ssl.create_default_context(cafile=ca)
    need(ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED, "TLS_REQUIRED")
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                      urllib.request.HTTPSHandler(context=ctx))


def collect(c):
    gate = check_gate()
    plan = load_plan(c)
    attempts = sorted((RUN / "attempts").glob("*.json"))
    if attempts:
        need(gate["active"].get("review_path"), "REVIEWED_RETRY_REQUIRED")
        need(all((RUN / "receipts" / p.name).exists() for p in attempts), "INTERRUPTED_REQUEST")
    else:
        save_new(RUN / "collection-start.json", {"time": time.time()})
    started = strict((RUN / "collection-start.json").read_bytes())["time"]
    budget = c["budget"]
    opener = transport()
    count, previous = len(attempts), 0.0
    for request in plan["requests"]:
        page_path = RUN / "pages" / (request["key"] + ".json")
        if page_path.exists():
            page = strict(page_path.read_bytes())
            raw = (RUN / "raw" / page["raw_name"]).read_bytes()
            need(sha(raw) == page["sha256"] and page["url"] == request["url"], "CACHED_PAGE_CHANGED")
            validate_page(raw, request)
            continue
        need(count < budget["maximum_public_gets"], "GET_BUDGET")
        need(time.time() - started < budget["maximum_collection_seconds"], "COLLECTION_DEADLINE")
        delay = budget["minimum_request_interval_seconds"] - (time.monotonic() - previous)
        if delay > 0:
            time.sleep(delay)
        count += 1
        name = f"{count:06d}.json"
        save_new(RUN / "attempts" / name, {"url": request["url"], "started_at_utc": utc(time.time()*1000)})
        raw = b""
        receipt = {"url": request["url"], "success": False}
        try:
            req = urllib.request.Request(request["url"], headers={"User-Agent": "ai-trade-regional-fixed-screen/1"})
            with opener.open(req, timeout=budget["request_timeout_seconds"]) as response:
                receipt["http_status"] = response.status
                raw = response.read(budget["maximum_response_bytes"] + 1)
            need(len(raw) <= budget["maximum_response_bytes"], "RESPONSE_SIZE")
            need(sum(p.stat().st_size for p in (RUN / "raw").glob("*.bin")) + len(raw)
                 <= budget["maximum_total_bytes"], "TOTAL_SIZE")
            rows = validate_page(raw, request)
            receipt.update(success=True, rows=len(rows))
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                receipt["http_status"] = exc.code
                raw = exc.read(budget["maximum_response_bytes"] + 1)
            receipt.update(error_type=type(exc).__name__, error=str(exc)[:1000])
            raise
        finally:
            raw_name = name.replace(".json", ".bin")
            with (RUN / "raw" / raw_name).open("xb") as f:
                f.write(raw)
            receipt.update(raw_name=raw_name, sha256=sha(raw), bytes=len(raw),
                           finished_at_utc=utc(time.time()*1000))
            save_new(RUN / "receipts" / name, receipt)
            previous = time.monotonic()
        save_new(page_path, receipt)
        print("PUBLIC_GET", count, request["kind"], receipt["rows"], flush=True)
    save_new(RUN / "collection-complete.json", {"attempts": count, "pages": len(plan["requests"]),
             "elapsed_seconds": time.time()-started, "plan_sha256": sha((RUN / "plan.json").read_bytes())})


def inputs(c):
    plan = load_plan(c)
    need((RUN / "collection-complete.json").exists(), "COLLECTION_INCOMPLETE")
    values = {"trade": {}, "mark": {}, "funding": {}}
    for request in plan["requests"]:
        page = strict((RUN / "pages" / (request["key"] + ".json")).read_bytes())
        raw = (RUN / "raw" / page["raw_name"]).read_bytes()
        need(page["url"] == request["url"] and page["success"] is True and sha(raw) == page["sha256"],
             "SOURCE_IDENTITY")
        for row in validate_page(raw, request):
            funding = request["kind"] == "funding"
            t = stamp(row["fundingRateTimestamp"] if funding else row[0])
            dst = values[request["kind"]]
            need(t not in dst, "CROSS_PAGE_DUPLICATE")
            dst[t] = number(row["fundingRate"]) if funding else tuple(map(number, row[1:5]))
    start, end = ms(c["data_start_utc"]), ms(c["data_end_exclusive_utc"])
    for kind, grid in (("trade", STEP), ("mark", STEP), ("funding", c["cost"]["required_funding_grid_ms"])):
        need(sorted(values[kind]) == list(range((start+grid-1)//grid*grid, end, grid)), "FULL_INPUT_GRID")
    return values["trade"], values["mark"], values["funding"]


class Ledger:
    def __init__(self):
        self.lo = self.hi = 1.0
        self.q = self.entry = self.gross = self.fees = self.slippage = self.turnover = 0.0
        self.fund_lo = self.fund_hi = 0.0
        self.events, self.funding = [], []

    def enter(self, t, q, mid, slip, fee):
        need(self.q == 0 and q > 0, "ENTRY_STATE")
        p = mid * (1 + slip)
        cost = q * p * fee
        self.q, self.entry = q, p
        self.lo -= cost
        self.hi -= cost
        self.fees += cost
        self.slippage += q * (p-mid)
        self.turnover += q*p
        self.events.append({"t": t, "action": "entry", "q": q, "price": p, "fee": cost})

    def close(self, t, mid, slip, fee):
        need(self.q > 0, "EXIT_STATE")
        p = mid * (1 - slip)
        gross, cost = self.q * (p-self.entry), self.q*p*fee
        self.lo += gross-cost
        self.hi += gross-cost
        self.gross += gross
        self.fees += cost
        self.slippage += self.q * (mid-p)
        self.turnover += self.q*p
        self.events.append({"t": t, "action": "exit", "q": self.q, "price": p, "fee": cost})
        self.q = self.entry = 0.0

    def settle(self, t, rate, bar):
        if self.q:
            a, b = -self.q*rate*bar[2], -self.q*rate*bar[1]
            lo, hi = min(a, b), max(a, b)
            self.lo += lo
            self.hi += hi
            self.fund_lo += lo
            self.fund_hi += hi
            self.funding.append({"t": t, "q": self.q, "lo": lo, "hi": hi})

    def nav(self, p):
        pnl = self.q*(p-self.entry)
        return self.lo+pnl, self.hi+pnl

    def export(self):
        need(abs(self.lo-(1+self.gross-self.fees+self.fund_lo)) < 1e-10, "LOW_CASH_IDENTITY")
        need(abs(self.hi-(1+self.gross-self.fees+self.fund_hi)) < 1e-10, "HIGH_CASH_IDENTITY")
        return dict(cash_lo=self.lo, cash_hi=self.hi, q=self.q, entry_price=self.entry,
                    slipped_realized_gross=self.gross, fees=self.fees, reference_slippage=self.slippage,
                    turnover=self.turnover, funding_lo=self.fund_lo, funding_hi=self.fund_hi,
                    events=self.events, funding_events=self.funding)


class Risk:
    def __init__(self, c):
        self.c = c
        self.peak_lo = self.peak_hi = 1.0
        self.max_dd_lo = self.max_dd_hi = self.max_notional = 0.0

    def observe(self, ledger, low, high):
        low_lo, low_hi = ledger.nav(low)
        high_lo, high_hi = ledger.nav(high)
        # Current high/low order is unknown. Prior bars' maxima precede this bar.
        dd_lo = max(0.0, 1-low_hi/self.peak_lo)
        dd_hi = max(0.0, 1-low_lo/max(self.peak_hi, high_hi))
        self.max_dd_lo = max(self.max_dd_lo, dd_lo)
        self.max_dd_hi = max(self.max_dd_hi, dd_hi)
        self.max_notional = max(self.max_notional, ledger.q*high)
        limit = self.c["reference_max_drawdown"]
        if ledger.q*high > self.c["reference_max_notional_per_initial_capital"] or dd_lo >= limit:
            return "REJECT", "DEFINITE_REFERENCE_RISK_BREACH"
        if dd_hi >= limit:
            return "INSUFFICIENT_EVIDENCE", "INTRABAR_OR_FUNDING_RISK_AMBIGUITY"
        self.peak_lo = max(self.peak_lo, high_lo)
        self.peak_hi = max(self.peak_hi, high_hi)
        return None


def simulate(c, trade, mark, funding, schedule, deadline=math.inf):
    entries = {x["entry"]: x for x in schedule}
    exits = {x["exit"] for x in schedule}
    ends = {x["end"] for x in schedule}
    first, final = schedule[0]["entry"], schedule[-1]["end"]
    min_sep = c["cost"]["funding_execution_min_separation_ms"]
    need(all(abs(e-f) > min_sep for e in set(entries) | exits | ends for f in funding
             if abs(e-f) <= min_sep), "FUNDING_EXECUTION_AMBIGUITY")
    candidate, control, risk = Ledger(), Ledger(), Risk(c["risk"])
    daily, prev = [], (1.0, 1.0, 1.0, 1.0)
    completed = 0
    held_coin_ms = control_coin_ms = 0.0
    fee = c["cost"]["taker_fee_bps_per_side"] / 10000
    slip = c["cost"]["adverse_reference_slippage_bps_per_side"] / 10000

    def report(t, verdict=None):
        return {"full_window_evaluated": verdict is None, "last_processed_bar_ms": t,
                "decision": verdict[0] if verdict else None, "reason": verdict[1] if verdict else None,
                "completed_cycles": completed, "candidate": candidate.export(), "control": control.export(),
                "daily": daily, "drawdown_lower": risk.max_dd_lo, "drawdown_upper": risk.max_dd_hi,
                "max_reference_notional": risk.max_notional, "planned_candidate_coin_ms": held_coin_ms,
                "planned_control_coin_ms": control_coin_ms, "profitability_qualified": False,
                "promotion_authority": False, "stop_fill_simulated": False}

    for i, t in enumerate(range(first, final+STEP, STEP)):
        if i % 1000 == 0:
            need(time.monotonic() < deadline, "COMPUTE_DEADLINE")
        bar, mid = mark[t], trade[t][0]
        if t in funding:
            candidate.settle(t, funding[t], bar)
            control.settle(t, funding[t], bar)
        # Ex-post risk accounting, not a claim of an intrabar stop actuator.
        verdict = risk.observe(candidate, bar[0], bar[0])
        if verdict:
            return report(t, verdict)
        if t in exits:
            candidate.close(t, mid, slip, fee)
        if t in ends:
            control.close(t, mid, 0, 0)
            completed += 1
        if t in entries:
            s = entries[t]
            q = c["position"]["entry_notional_per_initial_capital"] / (mid*(1+slip))
            candidate.enter(t, q, mid, slip, fee)
            control.enter(t, q*s["rho"], mid, 0, 0)
            held_coin_ms += q*(s["exit"]-t)
            control_coin_ms += q*s["rho"]*(s["end"]-t)
        verdict = risk.observe(candidate, bar[2], bar[1])
        if verdict:
            return report(t, verdict)
        if (t+STEP) % DAY == 0 or t == final:
            nav = (*candidate.nav(bar[3]), *control.nav(bar[3]))
            pnl = tuple(a-b for a, b in zip(nav, prev))
            daily.append({"end_ms": t if t == final else t+STEP,
                          "net_lo": pnl[0], "net_hi": pnl[1],
                          "control_lo": pnl[2], "control_hi": pnl[3],
                          "timing_lo": pnl[0]-pnl[3], "timing_hi": pnl[1]-pnl[2]})
            prev = nav
    need(candidate.q == control.q == 0 and completed == len(schedule), "TERMINAL_NOT_FLAT")
    need(abs(sum(d["net_lo"] for d in daily)-(candidate.lo-1)) < 1e-10, "DAILY_CASH_IDENTITY")
    need(math.isclose(held_coin_ms, control_coin_ms, rel_tol=1e-12), "EXPOSURE_TIME_IDENTITY")
    return report(final)


def quantile(values, p):
    values = sorted(values)
    x = (len(values)-1)*p
    a = int(x)
    return values[a] + (values[min(a+1, len(values)-1)]-values[a])*(x-a)


def statistics(report, c, deadline=math.inf):
    if not report["full_window_evaluated"]:
        return report
    spec = c["statistics"]
    days = report["daily"]
    if report["completed_cycles"] < spec["minimum_completed_cycles"] or len(days) < spec["minimum_day_observations"]:
        return {**report, "decision": "INSUFFICIENT_EVIDENCE", "reason": "SAMPLE_FLOOR"}
    keys = ("net_lo", "net_hi", "timing_lo", "timing_hi")
    values = [[d[k] for d in days] for k in keys]
    n, results = len(days), []
    for block in spec["circular_moving_block_days"]:
        need(0 < block <= n, "BOOTSTRAP_BLOCK")
        prefixes = []
        for vector in values:
            p = [0.0]
            for v in vector+vector[:block]:
                p.append(p[-1]+v)
            prefixes.append(p)
        draws = [[] for _ in keys]
        rng = random.Random(spec["seed"]+block)
        for rep in range(spec["bootstrap_replicates"]):
            if rep % 256 == 0:
                need(time.monotonic() < deadline, "COMPUTE_DEADLINE")
            starts = [(rng.randrange(n), min(block, n-a)) for a in range(0, n, block)]
            for out, p in zip(draws, prefixes):
                out.append(sum(p[a+b]-p[a] for a, b in starts)/n)
        result = {"block_days": block}
        for key, draws_k in zip(keys, draws):
            tail = spec["lower_quantile"] if key.endswith("lo") else spec["upper_quantile"]
            result[key] = quantile(draws_k, tail)
        results.append(result)
    if max(x["net_hi"] for x in results) <= 0:
        decision, reason = "REJECT", "NONPOSITIVE_NET_UPPER_BOUND"
    elif all(x["net_lo"] > 0 and x["timing_lo"] > 0 for x in results):
        decision, reason = "WORTH_FURTHER_REVIEW", "REFERENCE_NET_AND_TIMING_LOWER_BOUNDS_POSITIVE"
    else:
        decision, reason = "INSUFFICIENT_EVIDENCE", "NET_OR_TIMING_LOWER_BOUND_NOT_POSITIVE"
    return {**report, "decision": decision, "reason": reason, "bootstrap": results,
            "mean_daily": dict(zip(keys, (sum(v)/n for v in values)))}


def audit_cash(report, c, trade, mark, funding, schedule):
    """Independent Decimal cash reconstruction from input prices, not simulate()."""
    D = lambda x: Decimal(str(x))
    entries = {x["entry"]: x for x in schedule}
    checked = {}
    final = report["last_processed_bar_ms"]
    for name in ("candidate", "control"):
        r = report[name]
        slip = D(c["cost"]["adverse_reference_slippage_bps_per_side"])/10000 if name == "candidate" else D(0)
        fee = D(c["cost"]["taker_fee_bps_per_side"])/10000 if name == "candidate" else D(0)
        lo = hi = D(1)
        q = entry = total_fees = gross = fund_lo = fund_hi = D(0)
        events = {e["t"]: [] for e in r["events"]}
        for e in r["events"]:
            events[e["t"]].append(e)
        expected_funding = []
        for t in sorted(set(events) | {f for f in funding if schedule[0]["entry"] <= f <= final}):
            if t in funding and q:
                a, b = -q*D(funding[t])*D(mark[t][2]), -q*D(funding[t])*D(mark[t][1])
                low, high = min(a,b), max(a,b)
                lo += low; hi += high; fund_lo += low; fund_hi += high
                expected_funding.append((t, low, high))
            for event in events.get(t, []):
                mid = D(trade[t][0])
                if event["action"] == "entry":
                    need(q == 0 and t in entries, "AUDIT_ENTRY_SCHEDULE")
                    s = entries[t]
                    candidate_q = D(c["position"]["entry_notional_per_initial_capital"]) / (
                        mid*(1+D(c["cost"]["adverse_reference_slippage_bps_per_side"])/10000))
                    q = candidate_q if name == "candidate" else candidate_q*D(s["exit"]-t)/D(s["end"]-t)
                    entry = price = mid*(1+slip)
                    need(abs(q-D(event["q"])) < D("1e-12"), "AUDIT_QUANTITY")
                else:
                    need(q > 0 and event["action"] == "exit", "AUDIT_EXIT_STATE")
                    need(abs(q-D(event["q"])) < D("1e-12"), "AUDIT_EXIT_QUANTITY")
                    need(t in {s["exit"] if name == "candidate" else s["end"] for s in schedule}, "AUDIT_EXIT_SCHEDULE")
                    price = mid*(1-slip)
                    pnl = q*(price-entry)
                    lo += pnl; hi += pnl; gross += pnl
                cost = q*price*fee
                lo -= cost; hi -= cost; total_fees += cost
                need(abs(price-D(event["price"])) < D("1e-8") and abs(cost-D(event["fee"])) < D("1e-12"), "AUDIT_EXECUTION")
                if event["action"] == "exit":
                    q = entry = D(0)
        need(len(expected_funding) == len(r["funding_events"]), "AUDIT_FUNDING_COUNT")
        for actual, expected in zip(r["funding_events"], expected_funding):
            need(actual["t"] == expected[0] and abs(D(actual["lo"])-expected[1]) < D("1e-12")
                 and abs(D(actual["hi"])-expected[2]) < D("1e-12"), "AUDIT_FUNDING_CASH")
        checks = {"cash_lo": lo, "cash_hi": hi, "fees": total_fees, "slipped_realized_gross": gross,
                  "funding_lo": fund_lo, "funding_hi": fund_hi, "q": q}
        for k, value in checks.items():
            need(abs(D(r[k])-value) < D("1e-10"), "AUDIT_"+name+"_"+k)
        if report["full_window_evaluated"]:
            need(len(r["events"]) == 2*len(schedule) and q == 0, "AUDIT_COMPLETE_EVENTS")
        checked[name] = {k: str(v) for k, v in checks.items()}
    return {"cash_reconstruction": "PASS", "method": "Decimal_input_price_calendar_and_settlement_reconstruction",
            "strategy_rerun": False, "market_qualification": False, "checks": checked}


def screen(c):
    check_gate()
    trade, mark, funding = inputs(c)
    save_new(RUN / "economic-start.json", {"contract_sha256": CONTRACT_SHA,
             "started_at_utc": utc(time.time()*1000), "maximum_experiments": 1})
    start = time.monotonic()
    deadline = start+c["budget"]["maximum_formal_compute_seconds"]
    report = statistics(simulate(c, trade, mark, funding, cycles(c), deadline), c, deadline)
    report.update(contract_sha256=CONTRACT_SHA, source_sha256=source_hashes(),
                  sample_classification=c["sample_classification"], compute_seconds=time.monotonic()-start)
    save_new(RUN / "result.json", report)
    print(json.dumps({k: report[k] for k in ("decision", "reason", "full_window_evaluated", "completed_cycles", "compute_seconds")}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "collect", "inspect", "screen", "audit"))
    args = parser.parse_args()
    c = contract()
    check_gate()
    if args.action == "freeze":
        freeze(c)
    elif args.action == "collect":
        collect(c)
    elif args.action == "screen":
        screen(c)
    elif args.action == "inspect":
        values = inputs(c)
        summary = {k: len(v) for k,v in zip(("trade_bars", "mark_bars", "funding_events"), values)}
        summary.update(contract_sha256=CONTRACT_SHA, market_return_calculations=0)
        save_new(RUN / "input-inspection.json", summary)
        print(json.dumps(summary))
    else:
        values = inputs(c)
        raw = (RUN / "result.json").read_bytes()
        report = strict(raw)
        need(report["contract_sha256"] == CONTRACT_SHA and report["source_sha256"] == source_hashes(), "RESULT_IDENTITY")
        result = audit_cash(report, c, *values, cycles(c))
        result["result_sha256"] = sha(raw)
        save_new(RUN / "cash-audit.json", result)
        print(json.dumps({k:v for k,v in result.items() if k != "checks"}))


if __name__ == "__main__":
    main()
