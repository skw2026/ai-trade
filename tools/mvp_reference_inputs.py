#!/usr/bin/env python3
"""Strict local public-response compiler. No network, credentials, or backtest.

Only compile already authorized archives. Synthetic tests use isolated archives;
the CLI binds the frozen annual contract and refuses output-directory reuse.
"""
import argparse
import csv
import hashlib
import io
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs/plans/2026-09-21-mvp-reference-v1.json"
CONTRACT_SHA256 = "f50dfdc5e7db2d15a16cffdf0ed832b4bcd15901d7645247ed1447fdb2c20d62"
FIELDS = "timestamp symbol open high low price volume interval_ms funding_rate_per_interval mark_open mark_close mark_high mark_low execution_enabled".split()


def require(ok, why):
    if not ok:
        raise ValueError(why)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def pairs(items):
        obj = {}
        for key, value in items:
            require(key not in obj, "DUPLICATE_JSON_KEY")
            obj[key] = value
        return obj
    def invalid(value):
        raise ValueError("NONFINITE_JSON:" + value)
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def timestamp(value):
    require(type(value) in (int, str) and str(value).isdigit(), "INVALID_TIMESTAMP")
    return int(value)


def number(value, positive=False):
    require(type(value) in (int, float, str), "INVALID_NUMBER_TYPE")
    try:
        v = Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError("INVALID_DECIMAL") from e
    require(v.is_finite() and math.isfinite(float(v)), "NONFINITE_NUMBER")
    require(v == 0 or float(v) != 0, "UNREPRESENTABLE_NUMBER")
    require(not positive or v > 0, "NONPOSITIVE_NUMBER")
    return v


def utc_ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def load_contract():
    raw = CONTRACT.read_bytes()
    require(sha(raw) == CONTRACT_SHA256, "FROZEN_CONTRACT_CHANGED")
    return strict_json(raw)


def window(contract):
    return tuple(utc_ms(contract[key]) for key in
                 ("source_start_utc", "evaluation_start_utc", "end_exclusive_utc"))


def candle(row, kind):
    require(isinstance(row, list) and len(row) == (7 if kind == "trade" else 5), "CANDLE_SHAPE")
    ts = timestamp(row[0])
    prices = [number(v, True) for v in row[1:5]]
    o, h, l, c = prices
    require(h >= max(o, c) and l <= min(o, c), "CANDLE_OHLC")
    volume = number(row[5]) if kind == "trade" else Decimal(0)
    require(volume >= 0, "NEGATIVE_VOLUME")
    if kind == "trade":
        require(number(row[6]) >= 0, "NEGATIVE_TURNOVER")
    return ts, [str(v) for v in prices] + [str(volume)]


def covered(ranges, start, end):
    cursor = start
    for a, b in sorted(ranges):
        require(a <= cursor, "REQUEST_COVERAGE_GAP")
        cursor = max(cursor, b + 1)
    require(cursor >= end, "REQUEST_COVERAGE_INCOMPLETE")


def compile_archive(manifest_path, contract=None):
    """contract override is for synthetic unit fixtures, never a CLI option."""
    contract = load_contract() if contract is None else contract
    synthetic = contract != load_contract()
    identity = sha(json.dumps(contract, sort_keys=True).encode()) if synthetic else CONTRACT_SHA256
    start, evaluation, end = window(contract)
    interval = contract["interval_ms"]
    grid = contract["input"]["funding_required_grid_ms"]
    require(start < evaluation < end and start % interval == evaluation % interval == end % interval == 0,
            "WINDOW_ALIGNMENT")
    require(end % grid != 0, "TERMINAL_FUNDING_BOUNDARY_UNSUPPORTED")
    path = Path(manifest_path).resolve()
    manifest_raw = path.read_bytes()
    manifest = strict_json(manifest_raw)
    require(manifest.get("schema") == "mvp_public_archive_v1" and
            manifest.get("contract_sha256") == identity, "MANIFEST_CONTRACT")
    pages = manifest.get("pages")
    require(isinstance(pages, list) and 3 <= len(pages) <= 500, "PAGE_BUDGET")
    data = {k: {} for k in ("trade", "mark", "funding")}
    ranges = {k: [] for k in data}
    seen_paths, seen_urls = set(), set()
    total_bytes = 0
    for page in pages:
        kind = page.get("kind")
        require(kind in data, "UNKNOWN_PUBLIC_DATA_KIND")
        url = page["url"]
        u = urlsplit(url)
        require(u.scheme == "https" and u.netloc == contract["input"]["public_host"] and
                not u.fragment and u.path == contract["input"][kind + "_endpoint"], "PUBLIC_URL_SCOPE")
        require(url not in seen_urls, "DUPLICATE_REQUEST")
        seen_urls.add(url)
        query = parse_qs(u.query, strict_parsing=True)
        require(all(len(v) == 1 for v in query.values()), "DUPLICATE_QUERY_PARAMETER")
        q = {k: v[0] for k, v in query.items()}
        funding = kind == "funding"
        low, high = ("startTime", "endTime") if funding else ("start", "end")
        keys = {"category", "symbol", low, high, "limit"} | (set() if funding else {"interval"})
        require(set(q) == keys and q["category"] == "linear" and q["symbol"] == contract["symbol"] and
                (funding or q["interval"] == "5"), "REQUEST_IDENTITY")
        a, b, limit = timestamp(q[low]), timestamp(q[high]), timestamp(q["limit"])
        require(start <= a <= b < end and 1 <= limit <= (200 if funding else 1000), "REQUEST_RANGE_LIMIT")
        ranges[kind].append((a, b))
        received = timestamp(page["received_at_ms"])
        require(received >= end, "UNFINISHED_CANDLE_ARCHIVE")
        filename = Path(page["file"])
        require(not filename.is_absolute(), "RAW_PATH_ABSOLUTE")
        raw_path = (path.parent / filename).resolve()
        require(raw_path.is_relative_to(path.parent) and raw_path != path and raw_path not in seen_paths,
                "RAW_PATH_ESCAPE_OR_REUSE")
        seen_paths.add(raw_path)
        require(raw_path.stat().st_size <= 2 * 1024 * 1024, "RAW_PAGE_TOO_LARGE")
        raw = raw_path.read_bytes()
        total_bytes += len(raw)
        require(total_bytes <= 256 * 1024 * 1024 and sha(raw) == page["sha256"], "RAW_HASH_OR_BUDGET")
        response = strict_json(raw)
        require(type(response.get("retCode")) is int and response["retCode"] == 0, "API_ERROR")
        result = response["result"]
        require(result.get("category") == "linear", "RESPONSE_CATEGORY")
        if not funding:
            require(result.get("symbol") == contract["symbol"], "RESPONSE_SYMBOL")
        rows = result.get("list")
        require(isinstance(rows, list) and len(rows) <= limit, "RESPONSE_PAGE_LIMIT")
        previous = None
        for row in rows:
            if funding:
                require(row.get("symbol") == contract["symbol"], "FUNDING_SYMBOL")
                ts, value = timestamp(row["fundingRateTimestamp"]), str(number(row["fundingRate"]))
            else:
                ts, value = candle(row, kind)
                require(ts % interval == 0 and ts + interval <= received, "CANDLE_ALIGNMENT")
            require(a <= ts <= b and (previous is None or ts < previous), "RESPONSE_ORDER_OR_RANGE")
            require(ts not in data[kind], "DUPLICATE_DATA_TIMESTAMP")
            previous = ts
            data[kind][ts] = value
    expected_bars = set(range(start, end, interval))
    expected_funding = set(range(((start + grid-1)//grid)*grid, end, grid))
    for kind in data:
        covered(ranges[kind], start, end)
        require(set(data[kind]) == (expected_funding if kind == "funding" else expected_bars),
                "FUNDING_GRID_OR_CANDLE_COVERAGE:" + kind)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for ts in sorted(expected_bars):
        o, h, l, c, v = data["trade"][ts]
        mo, mh, ml, mc, _ = data["mark"][ts]
        writer.writerow(dict(zip(FIELDS, [ts, contract["symbol"], o, h, l, c, v, interval,
            data["funding"].get(ts, "0"), mo, mc, mh, ml, int(ts >= evaluation)])))
    compiled = output.getvalue().encode()
    proof = {"schema": "mvp_reference_input_bundle_v1", "contract_sha256": identity, "synthetic_only": synthetic,
             "manifest_sha256": sha(manifest_raw), "csv_sha256": sha(compiled),
             "bars": len(expected_bars), "funding_events": len(expected_funding),
             "raw_pages": len(pages), "complete": True, "historical_account_qualification": False,
             "source_start_ms": start, "evaluation_start_ms": evaluation, "end_exclusive_ms": end}
    return compiled, proof


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True, help="new directory, must not exist")
    args = p.parse_args()
    compiled, proof = compile_archive(args.manifest)
    args.output.mkdir(parents=False, exist_ok=False)
    with (args.output / "replay.csv").open("xb") as f:
        f.write(compiled)
    with (args.output / "input-proof.json").open("x") as f:
        json.dump(proof, f, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(proof, sort_keys=True))


if __name__ == "__main__":
    main()
