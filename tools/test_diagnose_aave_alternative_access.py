import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest import mock
import urllib.error

import diagnose_aave_alternative_access as diag


class AlternativeDiagnostics(unittest.TestCase):
    def setUp(self):
        clock = mock.patch.object(diag.frozen, "now", return_value=dt.datetime(
            2026, 10, 12, 7, 50, tzinfo=dt.timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)

    def log(self):
        return {"address": diag.frozen.POOL, "removed": False,
                "topics": [diag.frozen.TOPIC, "0x" + "0" * 24 + diag.frozen.WETH[2:],
                           "0x" + "0" * 64, "0x" + "0" * 64],
                "data": "0x" + "0" * 256, "blockNumber": hex(20000000),
                "transactionIndex": "0x0", "logIndex": "0x0",
                "transactionHash": "0x" + "a" * 64, "blockHash": "0x" + "b" * 64}

    def test_partition_exact_original_domain(self):
        values = [n for lo, hi in diag.RANGES for n in range(lo, hi + 1)]
        self.assertEqual(values, list(range(20000000, 20005000)))
        self.assertEqual(len(diag.RANGES), 7)
        self.assertTrue(all(hi - lo + 1 <= 800 for lo, hi in diag.RANGES))

    def test_whitelist_before_network(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diag.urllib.request, "build_opener") as net:
            for endpoint, method, kind in (("https://example.com", "eth_chainId", "chain"),
                      (diag.ENDPOINTS["flashbots"], "eth_sendRawTransaction", "chain"),
                      (diag.ENDPOINTS["flashbots"], "eth_chainId", "unknown")):
                with self.assertRaises(ValueError):
                    diag.request(Path(tmp), endpoint, "chain", method, [], kind)
            net.assert_not_called()

    def invoke_response(self, value, kind="chain", params=None):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diag.urllib.request, "build_opener") as net:
            reply = net.return_value.open.return_value.__enter__.return_value
            reply.status = 200
            reply.read.return_value = json.dumps(value).encode()
            return diag.request(Path(tmp), diag.ENDPOINTS["flashbots"], "sample",
                                {"chain": "eth_chainId", "logs": "eth_getLogs",
                                 "event_block": "eth_getBlockByNumber"}[kind],
                                [] if params is None else params, kind)

    def envelope(self, value):
        return {"jsonrpc": "2.0", "id": 1, "result": value}

    def test_chain_and_errors(self):
        self.assertEqual(self.invoke_response(self.envelope("0x1")), "0x1")
        for bad in (self.envelope("0x89"), {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603}},
                    {"jsonrpc": "2.0", "id": True, "result": "0x1"}):
            with self.assertRaises(ValueError):
                self.invoke_response(bad)

    def test_logs_cannot_escape_chunk(self):
        row = self.log()
        params = [{"fromBlock": hex(20000000), "toBlock": hex(20000799), "topics": row["topics"][:2]}]
        self.assertEqual(len(self.invoke_response(self.envelope([row]), "logs", params)), 1)
        row["blockNumber"] = hex(20000800)
        with self.assertRaisesRegex(ValueError, "LOG_OUTSIDE_CHUNK"):
            self.invoke_response(self.envelope([row]), "logs", params)

    def test_event_block_identity(self):
        with self.assertRaisesRegex(ValueError, "EVENT_BLOCK_FIELDS"):
            self.invoke_response(self.envelope({"number": hex(20000001)}),
                                 "event_block", [hex(20000000), False])

    def test_http_failure_retained_and_no_retry(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diag.urllib.request, "build_opener") as net:
            root = Path(tmp)
            net.return_value.open.side_effect = urllib.error.HTTPError(
                diag.ENDPOINTS["flashbots"], 403, "Forbidden", {}, io.BytesIO(b"denied"))
            with self.assertRaises(urllib.error.HTTPError):
                diag.request(root, diag.ENDPOINTS["flashbots"], "chain", "eth_chainId", [], "chain")
            receipt = json.loads((root / "chain.json").read_bytes())
            self.assertEqual(receipt["http_status"], 403)
            self.assertEqual((root / "chain.error-body").read_bytes(), b"denied")
            with self.assertRaises(FileExistsError):
                diag.request(root, diag.ENDPOINTS["flashbots"], "chain", "eth_chainId", [], "chain")
            self.assertEqual(net.return_value.open.call_count, 1)

    def test_tls_cannot_be_disabled(self):
        with mock.patch.object(diag.ssl, "create_default_context") as context:
            context.return_value.check_hostname = False
            context.return_value.verify_mode = ssl.CERT_NONE
            with self.assertRaisesRegex(ValueError, "TLS_VERIFICATION_REQUIRED"):
                self.invoke_response(self.envelope("0x1"))

    def fixtures(self, has_event=False, broken_receipt=False, broken_time=False):
        log = self.log()
        def respond(root, endpoint, name, method, params, kind):
            if name == "chain": return "0x1"
            if name == "finalized": return {"timestamp": hex(1800000000)}
            if name == "historical": return {"timestamp": hex(1700000000), "hash": log["blockHash"]}
            if name == "topic": return diag.frozen.TOPIC
            if name.startswith("logs-"): return [log] if has_event and name == "logs-0" else []
            if name == "receipt": return {"blockHash": log["blockHash"], "logs": [] if broken_receipt else [log]}
            if name == "event-block": return {"blockHash": log["blockHash"], "hash": log["blockHash"],
                   "number": log["blockNumber"], "timestamp": hex(1900000000 if broken_time else 1700000000)}
            raise AssertionError(name)
        return respond

    def scenario(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diag, "request", side_effect=self.fixtures(**kwargs)) as call:
            with contextlib.redirect_stdout(io.StringIO()):
                code = diag.run("flashbots", Path(tmp))
            summary = json.loads((Path(tmp) / "flashbots/summary.json").read_bytes())
            with self.assertRaises(FileExistsError):
                diag.run("flashbots", Path(tmp))
            return code, summary, call.call_count

    def test_empty_logs_not_economic_failure(self):
        code, summary, calls = self.scenario()
        self.assertEqual((code, summary["status"], calls), (0, "NO_CONTROL_EVENT", 11))
        self.assertFalse(summary["complete_history"])
        self.assertFalse(summary["receipt_verified"])

    def test_receipt_bound_success(self):
        code, summary, calls = self.scenario(has_event=True)
        self.assertEqual((code, summary["status"], calls), (0, "CONTROL_ACCESS_SUPPORTED", 13))
        self.assertFalse(summary["flow_identified"])

    def test_bad_receipt_stops_event_request(self):
        code, summary, calls = self.scenario(has_event=True, broken_receipt=True)
        self.assertEqual((code, summary["status"], calls), (2, "INPUT_ACCESS_BLOCKED", 12))

    def test_future_event_time_blocked(self):
        code, summary, calls = self.scenario(has_event=True, broken_time=True)
        self.assertEqual((code, summary["reason"], calls), (2, "EVENT_TIME_ORDER", 13))

    def test_first_failure_stops_all_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diag, "request", side_effect=TimeoutError) as call:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(diag.run("cloudflare", Path(tmp)), 2)
            self.assertEqual(call.call_count, 1)

    def test_local_keccak_exact_signature(self):
        with mock.patch.object(diag.subprocess, "run") as run:
            run.side_effect = [mock.Mock(stdout=b"KECCAK-256(stdin)= c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470\n"),
                               mock.Mock(stdout=("KECCAK-256(stdin)= " + diag.frozen.TOPIC[2:]).encode())]
            self.assertEqual(diag.local_topic()["event_topic"], diag.frozen.TOPIC)
            self.assertEqual(run.call_args_list[1].kwargs["input"], diag.frozen.SIGNATURE.encode())

    def test_bad_local_keccak_rejected(self):
        with mock.patch.object(diag.subprocess, "run", return_value=mock.Mock(stdout=b"hash= wrong")):
            with self.assertRaisesRegex(ValueError, "LOCAL_KECCAK_EMPTY_VECTOR"):
                diag.local_topic()

    def test_cache_hash_and_exact_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
            response = json.dumps(self.envelope("0x1")).encode()
            diag.frozen.exclusive(root / "chain.request.json", request)
            diag.frozen.exclusive(root / "chain.response.json", response)
            record = {"status": "DIAGNOSTIC_RESPONSE_VALID", "endpoint": diag.ENDPOINTS["flashbots"],
                      "step": "chain", "method": "eth_chainId", "source_sha256": diag.EXECUTED_HASH,
                      "plan_sha256": diag.frozen.digest(diag.PLAN.read_bytes()),
                      "request_sha256": diag.frozen.digest(request), "response_sha256": diag.frozen.digest(response)}
            diag.save(root / "chain.json", record)
            self.assertEqual(diag.cached_control(root, "chain", []), "0x1")
            with self.assertRaisesRegex(ValueError, "CACHED_CONTROL_CHANGED"):
                diag.cached_control(root, "chain", ["changed"])
            (root / "chain.response.json").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "CACHED_CONTROL_CHANGED"):
                diag.cached_control(root, "chain", [])

    def test_followup_missing_evidence_never_requests(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(diag, "request") as net:
            root = Path(tmp)
            (root / "flashbots").mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(diag.run("flashbots", root, remaining_logs=True), 2)
            net.assert_not_called()

    def test_followup_never_repeats_initial_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "flashbots"
            original.mkdir()
            for name in ("chain", "finalized", "historical", "topic"):
                diag.frozen.exclusive(original / (name + ".request.json"), b"{}")
            error_raw = b'{"error":{"code":-32601,"message":"rpc method is not whitelisted"}}'
            diag.frozen.exclusive(original / "topic.error-body", error_raw)
            diag.frozen.exclusive(root / "executed-before-followup.py", b"synthetic")
            review = root / "review.json"
            diag.save(review, {"rejected_body_sha256": diag.frozen.digest(error_raw),
                     "decision": "bounded_read_only_diagnostic_followup_not_formal_retry"})
            with mock.patch.object(diag, "REVIEW", review), \
                 mock.patch.object(diag, "EXECUTED_HASH", diag.frozen.digest(b"synthetic")), \
                 mock.patch.object(diag, "cached_control", side_effect=["0x1", {"timestamp": hex(1800000000)},
                            {"timestamp": hex(1700000000), "hash": "0x" + "b" * 64}]), \
                 mock.patch.object(diag, "local_topic", return_value={"event_topic": diag.frozen.TOPIC}), \
                 mock.patch.object(diag, "request", side_effect=self.fixtures()) as net, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(diag.run("flashbots", root, remaining_logs=True), 0)
                self.assertEqual(net.call_count, 7)
                self.assertTrue(all(call.args[2].startswith("logs-") for call in net.call_args_list))
                with self.assertRaises(FileExistsError):
                    diag.run("flashbots", root, remaining_logs=True)


if __name__ == "__main__":
    unittest.main()
