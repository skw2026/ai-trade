#!/usr/bin/env python3
"""Six once-only, anonymous read-only controls for the Aave input review.

No market prices, wallet credentials, account methods, signing or transactions.
Input availability is not execution identification or strategy qualification.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://ethereum-rpc.publicnode.com"
POOL = "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
SIGNATURE = "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
# Independently computed with OpenSSL 3.6.3 KECCAK-256; empty-input Ethereum vector checked.
TOPIC = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"
METHODS = {"chain": "eth_chainId", "finalized": "eth_getBlockByNumber",
           "historical": "eth_getBlockByNumber", "topic": "web3_sha3",
           "logs": "eth_getLogs", "receipt": "eth_getTransactionReceipt"}
ROOT = Path(".artifacts/aave-forced-flow-20261012/input")
LIMIT = 2 * 1024 * 1024
DEADLINE = dt.datetime(2026, 10, 12, 11, 24, tzinfo=dt.timezone.utc)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def now():
    return dt.datetime.now(dt.timezone.utc)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def exclusive(path, raw):
    with path.open("xb") as handle:
        handle.write(raw)


def result(root, step):
    record = json.loads((root / (step + ".json")).read_bytes())
    require(record["status"] == "CONTROL_PASS", "DEPENDENCY_NOT_PASSED")
    request_raw = (root / (step + ".request.json")).read_bytes()
    request = json.loads(request_raw)
    require(record.get("step") == step and record.get("endpoint") == ENDPOINT
            and record.get("method") == METHODS[step]
            and digest(request_raw) == record.get("request_sha256")
            and request.get("method") == METHODS[step]
            and request.get("jsonrpc") == "2.0" and type(request.get("id")) is int
            and request["id"] == 1, "REQUEST_IDENTITY_CHANGED")
    static_params = {"chain": [], "finalized": ["finalized", False],
                     "historical": [hex(20000000), False],
                     "topic": ["0x" + SIGNATURE.encode("ascii").hex()],
                     "logs": [{"address": POOL, "fromBlock": hex(20000000),
                               "toBlock": hex(20004999),
                               "topics": [TOPIC, "0x" + "0" * 24 + WETH[2:]]}]}
    if step in static_params:
        require(request.get("params") == static_params[step], "REQUEST_PARAMS_CHANGED")
    raw = (root / (step + ".response.json")).read_bytes()
    require(digest(raw) == record["response_sha256"], "RESPONSE_CHANGED")
    decoded = json.loads(raw)
    validate(step, decoded, request["params"])
    return decoded["result"]


def request_for(step, root):
    if step == "chain":
        return "eth_chainId", []
    predecessors = {"finalized": "chain", "historical": "finalized", "topic": "historical",
                    "logs": "topic", "receipt": "logs"}
    require(step in predecessors, "UNKNOWN_STEP")
    result(root, predecessors[step])
    require(result(root, "chain") == "0x1", "WRONG_CHAIN")
    if step == "finalized":
        return "eth_getBlockByNumber", ["finalized", False]
    if step == "historical":
        return "eth_getBlockByNumber", [hex(20000000), False]
    if step == "topic":
        return "web3_sha3", ["0x" + SIGNATURE.encode("ascii").hex()]
    if step == "logs":
        result(root, "historical")
        topic = result(root, "topic")
        return "eth_getLogs", [{"address": POOL, "fromBlock": hex(20000000),
                               "toBlock": hex(20004999),
                               "topics": [topic, "0x" + "0" * 24 + WETH[2:]]}]
    if step == "receipt":
        logs = result(root, "logs")
        require(bool(logs), "NO_CONTROL_EVENT_NOT_GLOBAL_ABSENCE")
        first = min(logs, key=lambda row: tuple(int(row[k], 16) for k in
                    ("blockNumber", "transactionIndex", "logIndex")))
        return "eth_getTransactionReceipt", [first["transactionHash"]]
    raise ValueError("UNKNOWN_STEP")


def valid_hex(value, width=None):
    return isinstance(value, str) and re.fullmatch(
        r"0x[0-9a-fA-F]{%d}" % width if width else r"0x[0-9a-fA-F]+", value) is not None


def validate(step, response, params):
    require(isinstance(response, dict) and response.get("jsonrpc") == "2.0"
            and type(response.get("id")) is int and response["id"] == 1, "RPC_ENVELOPE")
    require("error" not in response and "result" in response, "RPC_ERROR_OR_MISSING_RESULT")
    value = response["result"]
    if step == "chain":
        require(value == "0x1", "WRONG_CHAIN")
    elif step in ("finalized", "historical"):
        require(isinstance(value, dict) and valid_hex(value.get("number"))
                and valid_hex(value.get("timestamp")) and valid_hex(value.get("hash"), 64),
                "BLOCK_FIELDS")
        require(1438269973 <= int(value["timestamp"], 16) <= int(now().timestamp()) + 60,
                "BLOCK_TIMESTAMP_INVALID")
        if step == "historical":
            require(int(value["number"], 16) == 20000000, "HISTORICAL_BLOCK_MISMATCH")
        else:
            require(int(value["number"], 16) >= 20004999, "FINALIZED_BEFORE_CONTROL_DOMAIN")
    elif step == "topic":
        require(value == TOPIC, "EVENT_TOPIC_INVALID")
    elif step == "logs":
        require(isinstance(value, list), "LOGS_NOT_LIST")
        seen = set()
        for row in value:
            require(isinstance(row, dict) and row.get("address", "").lower() == POOL,
                    "POOL_MISMATCH")
            require(row.get("removed") is False and row.get("topics", [])[:2]
                    == params[0]["topics"], "LOG_FILTER_OR_REORG")
            require(len(row["topics"]) == 4 and all(valid_hex(x, 64) for x in row["topics"])
                    and valid_hex(row.get("data"), 256), "LOG_ABI_LAYOUT")
            require(all(valid_hex(row.get(k)) for k in
                        ("blockNumber", "transactionIndex", "logIndex"))
                    and valid_hex(row.get("transactionHash"), 64)
                    and valid_hex(row.get("blockHash"), 64), "LOG_IDENTITIES")
            require(20000000 <= int(row["blockNumber"], 16) <= 20004999, "BLOCK_OUTSIDE_RANGE")
            key = (row["blockHash"].lower(), row["transactionHash"].lower(), int(row["logIndex"], 16))
            require(key not in seen, "DUPLICATE_LOG")
            seen.add(key)
    elif step == "receipt":
        require(isinstance(value, dict) and value.get("transactionHash") == params[0]
                and value.get("status") == "0x1" and isinstance(value.get("logs"), list)
                and valid_hex(value.get("blockHash"), 64), "RECEIPT_IDENTITY_OR_STATUS")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("REDIRECT_NOT_ALLOWED")


def probe(step, root=ROOT):
    require(now() < DEADLINE, "BATCH_DEADLINE")
    method, params = request_for(step, root)
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = json.dumps(request, sort_keys=True).encode()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    exclusive(root / (step + ".request.json"), body)
    record = {"step": step, "method": method, "endpoint": ENDPOINT,
              "request_sha256": digest(body), "started_utc": now().isoformat(),
              "status": "CONTROL_FAIL", "account_access": False, "retries": 0}
    record["source_sha256"] = digest(Path(__file__).read_bytes())
    record["plan_sha256"] = digest(Path("docs/plans/2026-10-12-aave-forced-flow-research.md").read_bytes())
    try:
        context = ssl.create_default_context()
        if sys.platform == "darwin" and ssl.get_default_verify_paths().cafile is None:
            # Use the OS-managed public CA bundle; never disable TLS or hostname verification.
            context.load_verify_locations(cafile="/etc/ssl/cert.pem")
        require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED,
                "TLS_VERIFICATION_REQUIRED")
        req = urllib.request.Request(ENDPOINT, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context)).open(req, timeout=15) as reply:
            record["http_status"] = reply.status
            raw = reply.read(LIMIT + 1)
        require(len(raw) <= LIMIT, "RESPONSE_TOO_LARGE")
        exclusive(root / (step + ".response.json"), raw)
        record["response_sha256"] = digest(raw)
        decoded = json.loads(raw)
        validate(step, decoded, params)
        if step == "historical":
            head = result(root, "finalized")
            require(int(decoded["result"]["timestamp"], 16) < int(head["timestamp"], 16),
                    "BLOCK_TIME_ORDER")
        if step == "receipt":
            first = min(result(root, "logs"), key=lambda row: tuple(int(row[k], 16) for k in
                        ("blockNumber", "transactionIndex", "logIndex")))
            require(decoded["result"]["blockHash"] == first["blockHash"]
                    and first in decoded["result"]["logs"], "RECEIPT_LOG_MISMATCH")
        record["status"] = "CONTROL_PASS"
        if step == "logs":
            record["logs"] = len(decoded["result"])
        record["flow_identified"] = False
        record["complete_history"] = False
    except Exception as error:
        record["error_type"] = type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            record["http_status"] = error.code
            try:
                error_raw = error.read(LIMIT + 1)
                record["error_body_truncated"] = len(error_raw) > LIMIT
                exclusive(root / (step + ".error-body"), error_raw[:LIMIT])
                record["error_body_sha256"] = digest(error_raw[:LIMIT])
            except Exception as body_error:
                record["error_body_read_error"] = type(body_error).__name__
        elif isinstance(error, urllib.error.URLError):
            record["transport_error_type"] = type(error.reason).__name__
            record["transport_errno"] = getattr(error.reason, "errno", None)
        if isinstance(error, ValueError) and re.fullmatch(r"[A-Z_]+", str(error)):
            record["reason"] = str(error)
    record["finished_utc"] = now().isoformat()
    exclusive(root / (step + ".json"), (json.dumps(record, sort_keys=True) + "\n").encode())
    print(json.dumps(record, sort_keys=True))
    return 0 if record["status"] == "CONTROL_PASS" else 2


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("chain", "finalized", "historical", "topic", "logs", "receipt"))
    step = parser.parse_args().step
    root = ROOT
    original = ROOT / "chain.json"
    if original.exists():
        old = json.loads(original.read_bytes())
        if old.get("status") == "CONTROL_FAIL" and old.get("transport_error_type") == "SSLCertVerificationError":
            require(old["request_sha256"] == digest((ROOT / "chain.request.json").read_bytes()),
                    "ORIGINAL_REQUEST_CHANGED")
            root = ROOT / "tls-retry-once"
            if step == "chain":
                state = json.loads((ROOT.parent / "validation-state.json").read_bytes())
                active = state.get("active") or {}
                require(state.get("status") == "RUNNING"
                        and active.get("failure_id") == "d52ca53cc4354211ac1439e9a862726a"
                        and active.get("label") == "aave-chain-control", "RETRY_NOT_GATE_AUTHORIZED")
                review = Path(active["review_path"]).read_bytes()
                require(digest(review) == active["review_sha256"]
                        and json.loads(review).get("decision") == "retry", "RETRY_REVIEW_CHANGED")
    return probe(step, root)


if __name__ == "__main__":
    raise SystemExit(main())
