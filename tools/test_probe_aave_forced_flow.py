import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import contextlib
import io

import probe_aave_forced_flow as probe


class InputControls(unittest.TestCase):
    def envelope(self, value):
        return {"jsonrpc": "2.0", "id": 1, "result": value}

    def test_chain(self):
        probe.validate("chain", self.envelope("0x1"), [])
        for bad in ("0x89", 1, None):
            with self.assertRaises(ValueError):
                probe.validate("chain", self.envelope(bad), [])

    def test_rpc_error_and_bool_id(self):
        for bad in ({"jsonrpc": "2.0", "id": 1, "error": {}},
                    {"jsonrpc": "2.0", "id": True, "result": "0x1"}):
            with self.assertRaises(ValueError):
                probe.validate("chain", bad, [])

    def test_historical_block(self):
        row = {"number": hex(20000000), "hash": "0x" + "a" * 64, "timestamp": hex(1700000000)}
        probe.validate("historical", self.envelope(row), [])
        row["number"] = hex(20000001)
        with self.assertRaises(ValueError):
            probe.validate("historical", self.envelope(row), [])

    def test_topic(self):
        probe.validate("topic", self.envelope(probe.TOPIC), [])
        for invalid in ("0x1", "0x" + "a" * 64):
            with self.assertRaises(ValueError):
                probe.validate("topic", self.envelope(invalid), [])

    def log(self):
        return {"address": probe.POOL, "removed": False,
                "topics": ["0x" + "a" * 64, "0x" + "0" * 24 + probe.WETH[2:],
                           "0x" + "b" * 64, "0x" + "c" * 64],
                "data": "0x" + "0" * 256, "blockNumber": hex(20000000),
                "transactionIndex": "0x0", "logIndex": "0x0",
                "transactionHash": "0x" + "d" * 64, "blockHash": "0x" + "e" * 64}

    def test_valid_and_empty_logs(self):
        row = self.log()
        params = [{"topics": row["topics"][:2]}]
        probe.validate("logs", self.envelope([row]), params)
        probe.validate("logs", self.envelope([]), params)

    def test_invalid_logs(self):
        row = self.log()
        for key, value in (("address", "0x0"), ("removed", True), ("data", "0x01"),
                           ("blockNumber", hex(20005000)), ("transactionHash", "0x1")):
            bad = copy.deepcopy(row)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                probe.validate("logs", self.envelope([bad]), [{"topics": row["topics"][:2]}])

    def test_duplicate_logs(self):
        row = self.log()
        with self.assertRaises(ValueError):
            probe.validate("logs", self.envelope([row, row]), [{"topics": row["topics"][:2]}])

    def test_receipt(self):
        tx = "0x" + "a" * 64
        row = {"transactionHash": tx, "status": "0x1", "blockHash": "0x" + "b" * 64, "logs": []}
        probe.validate("receipt", self.envelope(row), [tx])
        row["status"] = "0x0"
        with self.assertRaises(ValueError):
            probe.validate("receipt", self.envelope(row), [tx])

    def test_exclusive_and_dependency_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = json.dumps(self.envelope("0x1")).encode()
            request = json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "method": "eth_chainId", "params": []}).encode()
            probe.exclusive(root / "chain.request.json", request)
            probe.exclusive(root / "chain.response.json", raw)
            probe.exclusive(root / "chain.json", json.dumps({"status": "CONTROL_PASS",
                            "step": "chain", "method": "eth_chainId", "endpoint": probe.ENDPOINT,
                            "request_sha256": probe.digest(request),
                            "response_sha256": probe.digest(raw)}).encode())
            self.assertEqual(probe.request_for("finalized", root),
                             ("eth_getBlockByNumber", ["finalized", False]))
            with self.assertRaises(FileNotFoundError):
                probe.request_for("historical", root)
            with self.assertRaises(FileExistsError):
                probe.exclusive(root / "chain.response.json", raw)
            (root / "chain.response.json").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                probe.result(root, "chain")

    def test_no_redirect(self):
        with self.assertRaises(ValueError):
            probe.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://example.com")

    def test_failed_predecessor_blocks_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe.exclusive(root / "finalized.json", b'{"status":"CONTROL_FAIL"}')
            with self.assertRaisesRegex(ValueError, "DEPENDENCY_NOT_PASSED"):
                probe.request_for("historical", root)

    def test_probe_once_and_request_identity(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(probe.urllib.request, "build_opener") as build:
            reply = build.return_value.open.return_value.__enter__.return_value
            reply.status = 200
            reply.read.return_value = json.dumps(self.envelope("0x1")).encode()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.probe("chain", Path(tmp)), 0)
            self.assertEqual(probe.result(Path(tmp), "chain"), "0x1")
            with self.assertRaises(FileExistsError):
                probe.probe("chain", Path(tmp))
            self.assertEqual(build.return_value.open.call_count, 1)
            (Path(tmp) / "chain.request.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "REQUEST_IDENTITY_CHANGED"):
                probe.result(Path(tmp), "chain")

    def test_finalized_time_and_domain(self):
        valid = {"number": hex(20005000), "timestamp": hex(1700000000), "hash": "0x" + "a" * 64}
        probe.validate("finalized", self.envelope(valid), [])
        for key, value in (("timestamp", "0x1"), ("timestamp", hex(9999999999)),
                           ("number", hex(19999999))):
            bad = dict(valid, **{key: value})
            with self.assertRaises(ValueError):
                probe.validate("finalized", self.envelope(bad), [])


if __name__ == "__main__":
    unittest.main()
