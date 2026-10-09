#!/usr/bin/env python3
"""Four fixed public metadata probes, not a data-intake or strategy gate.

No credentials, proxy environment, redirects, retries, archive-body downloads,
prices, account access or configurable target URLs. An empty availability reply
does not establish that an archive never existed. Exit zero means the diagnostic
receipt was written, NOT that inputs or a strategy were admitted.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import urllib.error
import urllib.parse
import urllib.request


MAX_BYTES = 262144
SHEET = "1ywc0ZqwfTqxfwHAxWpJlbMyrARoYI_81_wH2dFZUXc8"
TETHER = "app.tether.to/transparency.json"


def fixed_requests():
    available = "https://archive.org/wayback/available?"
    cdx = "https://web.archive.org/cdx/search/cdx?"
    requests = [("tether_" + day, available + urllib.parse.urlencode({
        "url": TETHER, "timestamp": day,
    })) for day in ("20230601", "20240601")]
    for name, target, start, end in (
        ("tether_index", TETHER, "20230601", "20240601"),
        ("crawler_sheet_index", "docs.google.com/spreadsheets/d/" + SHEET + "/*",
         "20220620", "20250507"),
    ):
        requests.append((name, cdx + urllib.parse.urlencode({
            "url": target, "output": "json", "filter": "statuscode:200",
            "from": start, "to": end, "collapse": "timestamp:6", "limit": 24,
            "fl": "timestamp,original,statuscode,mimetype,digest",
        })))
    return tuple(requests)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def summarize(payload, kind):
    if kind.startswith("tether_20"):
        if not isinstance(payload, dict) or not isinstance(payload.get("archived_snapshots"), dict):
            raise ValueError("invalid availability schema")
        closest = payload["archived_snapshots"].get("closest")
        if closest is None:
            return {"observation": "NO_CLOSEST_RETURNED_NOT_PROOF_OF_ABSENCE"}
        if not isinstance(closest, dict):
            raise ValueError("invalid closest record")
        stamp = closest.get("timestamp")
        url = closest.get("url")
        if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{14}", stamp):
            raise ValueError("invalid archive timestamp")
        dt.datetime.strptime(stamp, "%Y%m%d%H%M%S")
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in ("http", "https") or parsed.hostname != "web.archive.org":
            raise ValueError("invalid archive link")
        if parsed.username or parsed.password or len(url) > 2048:
            raise ValueError("invalid archive link")
        return {"observation": "CLOSEST_METADATA_ONLY", "timestamp": stamp,
                "url": url, "status": str(closest.get("status"))[:8],
                "available": closest.get("available") is True,
                "body_downloaded": False}
    columns = ["timestamp", "original", "statuscode", "mimetype", "digest"]
    if not isinstance(payload, list):
        raise ValueError("invalid CDX schema")
    if not payload:
        return {"observation": "EMPTY_INDEX_NOT_PROOF_OF_ABSENCE", "records": []}
    if payload[0] != columns or len(payload) > 25:
        raise ValueError("invalid CDX columns or limit")
    records = []
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("invalid CDX row")
        if any(not isinstance(v, str) or len(v) > 2048 for v in row):
            raise ValueError("invalid CDX field")
        if not re.fullmatch(r"[0-9]{14}", row[0]):
            raise ValueError("invalid CDX time")
        dt.datetime.strptime(row[0], "%Y%m%d%H%M%S")
        records.append(dict(zip(columns, row)))
    return {"observation": "INDEX_METADATA_ONLY", "records": records,
            "coverage_complete": False, "bodies_downloaded": False}


def collect_one(name, url, opener):
    receipt = {"name": name, "url": url, "http_status": None,
               "observed_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ai-trade-readonly-diagnostic/1"})
        with opener.open(request, timeout=10) as response:
            receipt["http_status"] = response.status
            raw = response.read(MAX_BYTES + 1)
        receipt.update(bytes_read=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        if len(raw) > MAX_BYTES:
            raise ValueError("response limit exceeded")
        receipt["metadata"] = summarize(json.loads(raw), name)
        receipt["parsed"] = True
    except urllib.error.HTTPError as exc:
        receipt.update(http_status=exc.code, parsed=False, error_type="HTTPError")
        exc.close()
    except (OSError, ValueError, urllib.error.URLError) as exc:
        # Do not serialize arbitrary errors, environment, response headers or HTML.
        receipt.update(parsed=False, error_type=type(exc).__name__)
    return receipt


def deadline(_signal, _frame):
    raise TimeoutError("fixed probe deadline")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    # Fail before network access if an existing receipt would be overwritten.
    with Path(args.output).open("x", encoding="utf-8") as output:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        old_handler = signal.signal(signal.SIGALRM, deadline)
        receipts = []
        try:
            for name, url in fixed_requests():
                signal.alarm(20)
                try:
                    receipts.append(collect_one(name, url, opener))
                finally:
                    signal.alarm(0)
        finally:
            signal.signal(signal.SIGALRM, old_handler)
        sha = os.environ.get("GITHUB_SHA", "")
        report = {
            "schema": "stablecoin_archive_access_diagnostic_v1",
            "diagnosis_completed": len(receipts) == 4,
            "all_responses_parsed": all(r["parsed"] for r in receipts),
            "input_admitted": False, "economic_attempts": 0,
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "commit_sha": sha if re.fullmatch(r"[a-f0-9]{40}", sha) else None,
            "requests": receipts,
        }
        json.dump(report, output, indent=2, sort_keys=True, allow_nan=False)
        output.write("\n")
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
