#!/usr/bin/env python3
"""Bounded read-only diagnostics; never retries or changes the halted intake gate."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

import probe_aave_forced_flow as frozen

ENDPOINTS = {"cloudflare": "https://web3-trial.cloudflare-eth.com/v1/mainnet",
             "flashbots": "https://rpc.flashbots.net/"}
ROOT = Path(".artifacts/aave-alternative-access-20261012")
PLAN = Path("docs/plans/2026-10-12-aave-alternative-access.md")
DEADLINE = dt.datetime(2026, 10, 12, 8, 31, tzinfo=dt.timezone.utc)
SOURCE_HASH = "bc63b75f31be0e1f200e0c4889d6bd028edc59922684939aaa86d4ca0bddee06"
RANGES = tuple((start, min(start + 799, 20004999))
               for start in range(20000000, 20005000, 800))
REVIEW = Path("docs/reviews/2026-10-12-aave-alternative-access-review.json")
EXECUTED_HASH = "6e2e36bec326288da20e43f14b144c4714a68e6b40aedc8880863ae7eba1d4ae"


def save(path, value):
    frozen.exclusive(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def protect():
    manifest = ROOT / "protected.json"
    if not manifest.exists():
        old = json.loads(Path(".artifacts/stablecoin-access-diagnostic-20261009/freeze.json").read_bytes())["protected"]
        protected = dict(old)
        for path in Path(".artifacts/aave-forced-flow-20261012").rglob("*"):
            if path.is_file():
                protected[str(path)] = frozen.digest(path.read_bytes())
        for path in (Path(frozen.__file__), Path("docs/plans/2026-10-12-aave-forced-flow-research.md")):
            protected[str(path)] = frozen.digest(path.read_bytes())
        save(manifest, protected)
    protected = json.loads(manifest.read_bytes())
    frozen.require(all(frozen.digest(Path(p).read_bytes()) == h for p, h in protected.items()),
                   "PROTECTED_ORIGINAL_CHANGED")
    frozen.require(frozen.digest(Path(frozen.__file__).read_bytes()) == SOURCE_HASH,
                   "FROZEN_VALIDATOR_CHANGED")
    return len(protected)


def request(root, endpoint, name, method, params, kind):
    frozen.require(endpoint in ENDPOINTS.values(), "ENDPOINT_NOT_ALLOWED")
    frozen.require(method in frozen.METHODS.values(), "METHOD_NOT_READ_ONLY")
    expected = dict(frozen.METHODS, event_block="eth_getBlockByNumber")
    frozen.require(expected.get(kind) == method, "METHOD_KIND_MISMATCH")
    frozen.require(frozen.now() < DEADLINE, "DIAGNOSTIC_DEADLINE")
    raw_request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                              "params": params}, sort_keys=True).encode()
    frozen.exclusive(root / (name + ".request.json"), raw_request)
    record = {"endpoint": endpoint, "method": method, "step": name,
              "request_sha256": frozen.digest(raw_request), "started_utc": frozen.now().isoformat(),
              "status": "DIAGNOSTIC_FAIL", "automatic_retries": 0,
              "source_sha256": frozen.digest(Path(__file__).read_bytes()),
              "plan_sha256": frozen.digest(PLAN.read_bytes())}
    try:
        context = ssl.create_default_context()
        if sys.platform == "darwin" and ssl.get_default_verify_paths().cafile is None:
            context.load_verify_locations(cafile="/etc/ssl/cert.pem")
        frozen.require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED,
                       "TLS_VERIFICATION_REQUIRED")
        req = urllib.request.Request(endpoint, data=raw_request,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.build_opener(frozen.NoRedirect(), urllib.request.HTTPSHandler(context=context)).open(req, timeout=15) as reply:
            record["http_status"] = reply.status
            raw = reply.read(frozen.LIMIT + 1)
        frozen.require(len(raw) <= frozen.LIMIT, "RESPONSE_TOO_LARGE")
        frozen.exclusive(root / (name + ".response.json"), raw)
        record["response_sha256"] = frozen.digest(raw)
        decoded = json.loads(raw)
        if isinstance(decoded, dict) and isinstance(decoded.get("error"), dict):
            record["rpc_error_code"] = decoded["error"].get("code")
        frozen.validate(kind, decoded, params)
        value = decoded["result"]
        if kind == "event_block":
            frozen.require(isinstance(value, dict) and value.get("number") == params[0]
                           and frozen.valid_hex(value.get("hash"), 64)
                           and frozen.valid_hex(value.get("timestamp")), "EVENT_BLOCK_FIELDS")
        if kind == "logs":
            low, high = (int(params[0][key], 16) for key in ("fromBlock", "toBlock"))
            frozen.require(all(low <= int(row["blockNumber"], 16) <= high for row in value),
                           "LOG_OUTSIDE_CHUNK")
        record["status"] = "DIAGNOSTIC_RESPONSE_VALID"
    except Exception as error:
        record["error_type"] = type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            record["http_status"] = error.code
            raw = error.read(frozen.LIMIT + 1)
            frozen.exclusive(root / (name + ".error-body"), raw[:frozen.LIMIT])
            record["error_body_sha256"] = frozen.digest(raw[:frozen.LIMIT])
            record["error_body_truncated"] = len(raw) > frozen.LIMIT
            error.close()
        if isinstance(error, urllib.error.URLError):
            record["transport_error_type"] = type(error.reason).__name__
        if isinstance(error, ValueError):
            record["reason"] = str(error)[:160]
        record["finished_utc"] = frozen.now().isoformat()
        save(root / (name + ".json"), record)
        raise
    record["finished_utc"] = frozen.now().isoformat()
    save(root / (name + ".json"), record)
    return value


def finish_logs(call, summary, old, head):
    logs = []
    for index, (low, high) in enumerate(RANGES):
        params = [{"address": frozen.POOL, "fromBlock": hex(low), "toBlock": hex(high),
                   "topics": [frozen.TOPIC, "0x" + "0" * 24 + frozen.WETH[2:]]}]
        logs.extend(call("logs-%d" % index, "eth_getLogs", params, "logs"))
    ordered = sorted(logs, key=lambda row: tuple(int(row[k], 16) for k in
                     ("blockNumber", "transactionIndex", "logIndex")))
    summary.update(control_range_complete=True, logs=len(logs), historical_block_hash=old["hash"],
                   ordered_logs_sha256=frozen.digest(json.dumps(ordered, sort_keys=True).encode()))
    if logs:
        first = ordered[0]
        receipt = call("receipt", "eth_getTransactionReceipt", [first["transactionHash"]], "receipt")
        frozen.require(receipt["blockHash"] == first["blockHash"] and first in receipt["logs"],
                       "RECEIPT_LOG_MISMATCH")
        block = call("event-block", "eth_getBlockByNumber", [first["blockNumber"], False], "event_block")
        frozen.require(isinstance(block, dict) and block.get("hash") == first["blockHash"]
                       and block.get("number") == first["blockNumber"]
                       and frozen.valid_hex(block.get("timestamp")), "EVENT_BLOCK_MISMATCH")
        frozen.require(int(old["timestamp"], 16) <= int(block["timestamp"], 16)
                       < int(head["timestamp"], 16), "EVENT_TIME_ORDER")
        summary.update(status="CONTROL_ACCESS_SUPPORTED", receipt_verified=True)
    else:
        summary.update(status="NO_CONTROL_EVENT", receipt_verified=False)


def cached_control(root, kind, params):
    record = json.loads((root / (kind + ".json")).read_bytes())
    raw = (root / (kind + ".response.json")).read_bytes()
    raw_request = (root / (kind + ".request.json")).read_bytes()
    expected = {"jsonrpc": "2.0", "id": 1, "method": frozen.METHODS[kind], "params": params}
    frozen.require(record["status"] == "DIAGNOSTIC_RESPONSE_VALID"
                   and record["endpoint"] == ENDPOINTS["flashbots"]
                   and record["step"] == kind and record["method"] == frozen.METHODS[kind]
                   and record["source_sha256"] == EXECUTED_HASH
                   and record["plan_sha256"] == frozen.digest(PLAN.read_bytes())
                   and record["request_sha256"] == frozen.digest(raw_request)
                   and json.loads(raw_request) == expected
                   and record["response_sha256"] == frozen.digest(raw), "CACHED_CONTROL_CHANGED")
    decoded = json.loads(raw)
    frozen.validate(kind, decoded, params)
    return decoded["result"]


def local_topic():
    def keccak(raw):
        result = subprocess.run(["/opt/homebrew/bin/openssl", "dgst", "-keccak-256"],
                                input=raw, capture_output=True, check=True, timeout=10)
        return "0x" + result.stdout.decode("ascii").split()[-1]
    empty = keccak(b"")
    frozen.require(empty == "0xc5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470",
                   "LOCAL_KECCAK_EMPTY_VECTOR")
    topic = keccak(frozen.SIGNATURE.encode("ascii"))
    frozen.require(topic == frozen.TOPIC, "LOCAL_KECCAK_EVENT_TOPIC")
    return {"event_topic": topic, "empty_vector": empty, "network_calls": 0}


def run(provider, root=ROOT, remaining_logs=False):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root == ROOT:
        protect()
    target = root / provider
    original = target
    if remaining_logs:
        frozen.require(provider == "flashbots", "FOLLOWUP_ONLY_FLASHBOTS")
        target = original / "remaining-logs-once"
    target.mkdir(mode=0o700)  # Once only, including interrupted attempts.
    endpoint = ENDPOINTS[provider]
    summary = {"provider": provider, "endpoint": endpoint, "status": "INPUT_ACCESS_BLOCKED",
               "flow_identified": False, "complete_history": False, "economic_attempts": 0}
    def call(name, method, params, kind):
        return request(target, endpoint, name, method, params, kind)
    try:
        if remaining_logs:
            review_raw = REVIEW.read_bytes()
            review = json.loads(review_raw)
            frozen.require(frozen.digest((root / "executed-before-followup.py").read_bytes())
                           == EXECUTED_HASH, "EXECUTED_SOURCE_CHANGED")
            error_raw = (original / "topic.error-body").read_bytes()
            refusal = json.loads(error_raw)
            frozen.require(frozen.digest(error_raw) == review["rejected_body_sha256"]
                           and refusal["error"]["code"] == -32601
                           and refusal["error"]["message"] == "rpc method is not whitelisted",
                           "NOT_CONFIRMED_METHOD_REFUSAL")
            frozen.require(review["decision"] == "bounded_read_only_diagnostic_followup_not_formal_retry",
                           "FOLLOWUP_REVIEW_REQUIRED")
            frozen.require(len(list(original.glob("*.request.json"))) == 4,
                           "ORIGINAL_REQUEST_COUNT_CHANGED")
            cached_control(original, "chain", [])
            head = cached_control(original, "finalized", ["finalized", False])
            old = cached_control(original, "historical", [hex(20000000), False])
            frozen.require(int(old["timestamp"], 16) < int(head["timestamp"], 16), "BLOCK_TIME_ORDER")
            topic = local_topic()
            topic["review_sha256"] = frozen.digest(review_raw)
            save(target / "local-topic.json", topic)
            finish_logs(call, summary, old, head)
        else:
            initial_controls(call, summary)
    except Exception as error:
        summary.update(error_type=type(error).__name__, internal_cause="UNKNOWN")
        if isinstance(error, ValueError):
            summary["reason"] = str(error)[:160]
    summary["rpc_attempts"] = len(list(target.glob("*.request.json")))
    summary["finished_utc"] = frozen.now().isoformat()
    save(target / "summary.json", summary)
    print(json.dumps(summary, sort_keys=True))
    return 2 if summary["status"] == "INPUT_ACCESS_BLOCKED" else 0


def initial_controls(call, summary):
    call("chain", "eth_chainId", [], "chain")
    head = call("finalized", "eth_getBlockByNumber", ["finalized", False], "finalized")
    old = call("historical", "eth_getBlockByNumber", [hex(20000000), False], "historical")
    frozen.require(int(old["timestamp"], 16) < int(head["timestamp"], 16), "BLOCK_TIME_ORDER")
    call("topic", "web3_sha3", ["0x" + frozen.SIGNATURE.encode().hex()], "topic")
    finish_logs(call, summary, old, head)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=tuple(ENDPOINTS))
    parser.add_argument("--remaining-logs", action="store_true",
                        help="Once-only reviewed diagnostic using original successful responses")
    args = parser.parse_args()
    return run(args.provider, remaining_logs=args.remaining_logs)


if __name__ == "__main__":
    raise SystemExit(main())
