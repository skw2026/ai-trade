#!/usr/bin/env python3
"""One authorized reference-model falsification; never a trading actuator."""
import argparse
import datetime as dt
import hashlib
import json
import math
import pathlib
import random
import ssl
import time
import urllib.parse
import urllib.request

HOUR = 3600000
WEEK = 168 * HOUR
ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/plans/2026-09-21-weekly-momentum-screen-v1.json"
HOST = "https://api.bybit.com"


def need(ok, message):
    if not ok:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def ms(value):
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def utc(value):
    return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).isoformat()


def write_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def load_contract():
    raw = CONTRACT.read_bytes()
    c = json.loads(raw)
    need(c["user_authorized_single_screen"] is True, "screen not authorized")
    need(all(v is False for v in c["authority"].values()), "actuation is forbidden")
    expected = {"symbol": "BTCUSDT", "category": "linear", "interval_minutes": 60,
                "lookback_days": 7, "decision_delay_hours": 1,
                "normalized_initial_capital": 1.0, "new_position_notional_per_initial_capital": 1.0,
                "base_reference_fee_bps": 5.5, "sensitivity_fee_bps": 11.0,
                "risk_drawdown_stop": .20, "maximum_public_gets": 100,
                "automatic_retries": 0, "maximum_wall_seconds": 1800}
    need(all(c.get(k) == v for k, v in expected.items()), "unsupported contract or changed acceptance")
    need(c["unknown_execution_cost_bps"] is None, "unknown execution costs must remain unknown")
    return c, digest(raw)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("redirect not authorized for fixed official source")


class PublicSource:
    def __init__(self, output, contract, contract_sha, deadline):
        self.output, self.c, self.deadline = output, contract, deadline
        output.mkdir(parents=True, exist_ok=True)
        self.path = output / "source-manifest.json"
        self.manifest = json.loads(self.path.read_text()) if self.path.exists() else {
            "contract_sha256": contract_sha, "attempted_gets": 0, "total_bytes": 0, "responses": {}}
        need(self.manifest["contract_sha256"] == contract_sha, "cached contract identity changed")
        fallback = pathlib.Path("/etc/ssl/cert.pem")
        ca = str(fallback) if ssl.get_default_verify_paths().cafile is None and fallback.is_file() else None
        self.context = ssl.create_default_context(cafile=ca)
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=self.context))

    def get(self, endpoint, params):
        need(endpoint in ("/v5/market/kline", "/v5/market/mark-price-kline", "/v5/market/funding/history"),
             "endpoint outside public source scope")
        url = HOST + endpoint + "?" + urllib.parse.urlencode(dict(category="linear", symbol="BTCUSDT", **params))
        key = digest(url.encode())
        path = self.output / (key + ".raw.json")
        if key in self.manifest["responses"]:
            record = self.manifest["responses"][key]
            raw = path.read_bytes()
            need(digest(raw) == record["sha256"] and record["url"] == url, "cached response identity failed")
        else:
            need(time.monotonic() < self.deadline, "wall budget exhausted")
            need(self.manifest["attempted_gets"] < self.c["maximum_public_gets"], "request budget exhausted")
            self.manifest["attempted_gets"] += 1
            write_json(self.path, self.manifest)  # Count failures as attempts too.
            request = urllib.request.Request(url, headers={"User-Agent": "ai-trade-weekly-falsification/1"})
            with self.opener.open(request, timeout=min(20, self.deadline-time.monotonic())) as response:
                raw = response.read(self.c["maximum_response_bytes"] + 1)
            need(len(raw) <= self.c["maximum_response_bytes"], "response size budget exceeded")
            need(self.manifest["total_bytes"] + len(raw) <= self.c["maximum_total_response_bytes"], "total size budget exceeded")
            path.write_bytes(raw)
            self.manifest["total_bytes"] += len(raw)
            self.manifest["responses"][key] = {"url": url, "sha256": digest(raw), "bytes": len(raw),
                "path": path.name, "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            write_json(self.path, self.manifest)
            print("SOURCE", endpoint, "get", self.manifest["attempted_gets"], flush=True)
        data = json.loads(raw)
        need(data.get("retCode") == 0, "official API retCode not zero")
        need(data.get("result", {}).get("category") == "linear", "unexpected market category")
        return data["result"]["list"]


def fetch_pages(source, name, start, end):
    funding = name == "funding"
    endpoint = "/v5/market/funding/history" if funding else (
        "/v5/market/kline" if name == "trade" else "/v5/market/mark-price-kline")
    cursor, collected = end - 1, []
    while cursor >= start:
        params = dict(startTime=start, endTime=cursor, limit=200) if funding else dict(
            interval="60", start=start, end=cursor, limit=1000)
        rows = source.get(endpoint, params)
        need(isinstance(rows, list) and rows, "missing required historical page")
        timestamps = [int(r["fundingRateTimestamp"] if funding else r[0]) for r in rows]
        need(all(start <= t <= cursor for t in timestamps), "page outside frozen range")
        need(len(set(timestamps)) == len(timestamps), "duplicate timestamp in page")
        collected.extend(rows)
        cursor = min(timestamps) - 1
    return collected


def bars(rows, start, end):
    values = {}
    for row in rows:
        need(len(row) >= 5, "short OHLC row")
        t = int(row[0])
        o, h, l, close = map(float, row[1:5])
        need(t not in values, "duplicate candle")
        need(all(math.isfinite(v) and v > 0 for v in (o, h, l, close)), "nonfinite or nonpositive OHLC")
        need(l <= min(o, close) <= max(o, close) <= h, "invalid OHLC bounds")
        values[t] = (o, h, l, close)
    need(sorted(values) == list(range(start, end, HOUR)), "incomplete hourly coverage")
    return values


def rates(rows, start, end):
    values = {}
    for row in rows:
        t, rate = int(row["fundingRateTimestamp"]), float(row["fundingRate"])
        need(row["symbol"] == "BTCUSDT" and math.isfinite(rate), "invalid funding row")
        need(t not in values, "duplicate funding event")
        values[t] = rate
    need(sorted(values) == list(range(start, end, 8*HOUR)),
         "funding not consistent with scoped 8h calendar; do not fill or change scope")
    return values


def funding_bounds(q, rate, low, high):
    a, b = -q * rate * low, -q * rate * high
    return min(a, b), max(a, b)


def target_quantity(q, signal, reference_price, initial_capital=1.0):
    if signal == 0:
        return 0.0
    if q * signal > 0:
        return q
    return signal * initial_capital / reference_price


def simulate(trade, mark, funding, first, final, fee_bps=5.5, drawdown_stop=.20):
    """Funding interval NAV at fixed reference prices; NOT executable account PnL."""
    executions = list(range(first, final + 1, WEEK))
    need(executions[-1] == final, "incomplete week schedule")
    need(not any(abs(t-e) <= 5000 for t in funding for e in executions), "ambiguous funding/execution boundary")
    balance_lo = balance_hi = peak_lo = 1.0
    q = entry = turnover = fees = gross_realized = fund_lo = fund_hi = 0.0
    max_reference_exposure = 0.0
    past_week_lo = past_week_hi = 1.0
    past_turnover = 0.0
    weeks, events = [], []
    base = {"initial_capital": 1.0, "full_window_evaluated": False,
            "profitability_qualified": False, "unknown_execution_cost_bps": None}
    for t in range(first, final + 1, HOUR):
        if t in funding and q:
            low, high = funding_bounds(q, funding[t], mark[t][2], mark[t][1])
            balance_lo += low
            balance_hi += high
            fund_lo += low
            fund_hi += high
        if t in executions:
            if t == final:
                signal = 0
            else:
                decision = t - HOUR
                ret = trade[decision-HOUR][3] / trade[decision-WEEK][0] - 1
                signal = (ret > 0) - (ret < 0)
            price = trade[t][0]
            new_q = target_quantity(q, signal, price)
            if new_q != q:
                realized = q * (price-entry)
                cost_notional = abs(new_q-q) * price
                fee = cost_notional * fee_bps / 10000
                balance_lo += realized-fee
                balance_hi += realized-fee
                gross_realized += realized
                fees += fee
                turnover += cost_notional
                events.append({"time": utc(t), "old_q": q, "new_q": new_q,
                    "reference_price": price, "turnover": cost_notional, "fee": fee})
                q, entry = new_q, price
            if t == final:
                # Replace the last pre-close weekly checkpoint with the final flat balance.
                if weeks:
                    weeks[-1]["lo"] += balance_lo-past_week_lo
                    weeks[-1]["hi"] += balance_hi-past_week_hi
                    weeks[-1]["turnover"] += turnover-past_turnover
                nav_lo, nav_hi, observed_at = balance_lo, balance_hi, t
            else:
                nav_lo = balance_lo + q * (mark[t][3]-entry)
                nav_hi = balance_hi + q * (mark[t][3]-entry)
                observed_at = t+HOUR
        else:
            nav_lo = balance_lo + q * (mark[t][3]-entry)
            nav_hi = balance_hi + q * (mark[t][3]-entry)
            observed_at = t+HOUR
        max_reference_exposure = max(max_reference_exposure, abs(q)*mark[t][3])
        peak_lo = max(peak_lo, nav_lo)
        dd_bound = max(0.0, 1-nav_hi/peak_lo)
        if nav_hi <= 0 or dd_bound >= drawdown_stop:
            return {**base, "decision": "REJECT", "reason": "REFERENCE_RISK_LIMIT",
                "stopped_at": utc(observed_at), "nav_lower": nav_lo, "nav_upper": nav_hi,
                "past_peak_lower": peak_lo, "drawdown_lower_bound_within_model": dd_bound,
                "complete_week_count": len(weeks), "funding_cash_lower": fund_lo,
                "funding_cash_upper": fund_hi, "reference_fees": fees,
                "realized_reference_gross": gross_realized, "turnover": turnover,
                "quantity_at_stop": q, "position_not_simulated_closed": True,
                "maximum_notional_per_initial_capital": max_reference_exposure,
                "events": events, "weekly": weeks}
        if t < final and t+HOUR in executions[1:]:
            weeks.append({"end": utc(t+HOUR), "lo": nav_lo-past_week_lo,
                          "hi": nav_hi-past_week_hi, "turnover": turnover-past_turnover})
            past_week_lo, past_week_hi, past_turnover = nav_lo, nav_hi, turnover
    need(q == 0 and len(weeks) == (final-first)//WEEK, "nonflat or incomplete evaluation")
    need(abs(sum(w["lo"] for w in weeks) - (balance_lo-1)) < 1e-9, "weekly cash identity failed")
    need(abs(sum(w["turnover"] for w in weeks)-turnover) < 1e-9, "weekly turnover identity failed")
    return {**base, "full_window_evaluated": True, "nav_lower": balance_lo, "nav_upper": balance_hi,
        "complete_week_count": len(weeks), "funding_cash_lower": fund_lo, "funding_cash_upper": fund_hi,
        "reference_fees": fees, "realized_reference_gross": gross_realized, "turnover": turnover,
        "maximum_notional_per_initial_capital": max_reference_exposure, "events": events, "weekly": weeks,
        "additional_one_way_cost_budget_bps_lower": (balance_lo-1)/turnover*10000 if turnover else None,
        "additional_one_way_cost_budget_bps_upper": (balance_hi-1)/turnover*10000 if turnover else None}


def classify(report, c):
    if report.get("decision") == "REJECT":
        return report  # Never analyze the remaining path or bootstrap after a risk stop.
    if not report["turnover"]:
        return {**report, "decision": "INSUFFICIENT_EVIDENCE", "reason": "NO_ACTIVITY"}
    if report["nav_upper"] <= 1:
        return {**report, "decision": "REJECT", "reason": "NONPOSITIVE_OPTIMISTIC_REFERENCE_RESULT"}
    values = [w["lo"]-(c["sensitivity_fee_bps"]-c["base_reference_fee_bps"])/10000*w["turnover"]
              for w in report["weekly"]]
    lower = {}
    for block in c["bootstrap_block_weeks"]:
        rng = random.Random(c["bootstrap_seed"]+block)
        means = []
        for _ in range(c["bootstrap_trials"]):
            draw = []
            while len(draw) < len(values):
                start = rng.randrange(len(values))
                draw.extend(values[(start+i) % len(values)] for i in range(block))
            means.append(sum(draw[:len(values)])/len(values))
        means.sort()
        lower[str(block)] = means[int(c["lower_percentile"]*(len(means)-1))]
    worthy = sum(values)/len(values) > 0 and all(v > 0 for v in lower.values())
    return {**report, "decision": "WORTH_FURTHER_REVIEW" if worthy else "INSUFFICIENT_EVIDENCE",
        "reason": "REFERENCE_SCREEN_ONLY_NOT_EXECUTION_QUALIFICATION" if worthy else "REFERENCE_UNCERTAINTY",
        "sensitivity_mean_weekly_cash_increment": sum(values)/len(values), "bootstrap_lower": lower}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    c, contract_sha = load_contract()
    output = args.output.resolve()
    need(output.is_relative_to(ROOT / ".artifacts"), "output must be an isolated repository artifact path")
    need(not (output / "result.json").exists(), "completed screen cannot be blindly repeated")
    start_time = time.monotonic()
    source = PublicSource(output, c, contract_sha, start_time+c["maximum_wall_seconds"])
    start, end = ms(c["source_start"]), ms(c["source_end_exclusive"])
    trade = bars(fetch_pages(source, "trade", start, end), start, end)
    mark = bars(fetch_pages(source, "mark", start, end), start, end)
    funding = rates(fetch_pages(source, "funding", start, end), start, end)
    need(time.monotonic()-start_time < c["maximum_wall_seconds"], "budget expired before calculation")
    print("DATA_VALIDATED", len(trade), len(mark), len(funding), flush=True)
    result = classify(simulate(trade, mark, funding, ms(c["first_execution"]), ms(c["final_exit"]),
                               c["base_reference_fee_bps"], c["risk_drawdown_stop"]), c)
    result.update({"schema_version": "weekly_momentum_reference_screen_v1", "contract_sha256": contract_sha,
        "source_manifest_sha256": digest(source.path.read_bytes()),
        "source_rows": {"trade_hourly": len(trade), "mark_hourly": len(mark), "funding": len(funding)},
        "attempted_public_gets": source.manifest["attempted_gets"], "source_bytes": source.manifest["total_bytes"],
        "elapsed_seconds": time.monotonic()-start_time,
        "authority": c["authority"], "historical_account_evidence": False,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    need(result["elapsed_seconds"] < c["maximum_wall_seconds"], "calculation budget exceeded")
    write_json(output / "result.json", result)
    print(json.dumps({k:v for k,v in result.items() if k not in ("events", "weekly")}, indent=2))


if __name__ == "__main__":
    main()
