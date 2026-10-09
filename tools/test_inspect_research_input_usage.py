"""Synthetic privacy/boundary tests; no remote or real price/account access."""
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest import mock
import inspect_research_input_usage as probe


def record(symbol="ETHUSDT", first=1704067200000, last=1735689599999):
    return {"schema_version": "market_alpha_history_v1", "symbols": [symbol],
            "quality": {"first_timestamp": first, "last_timestamp": last},
            "account_secret": "PRIVATE_MUST_NOT_ESCAPE", "profit": 987654321}


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/"research").mkdir()

    def put(self, name="market_alpha_history_report.json", data=None):
        p = self.root/"research"/name
        p.write_text(json.dumps(record() if data is None else data))
        return p

    def test_overlap_without_echoing_prices_or_secrets(self):
        p = self.put()
        before = p.read_bytes()
        r = probe.inventory(self.root)
        self.assertTrue(r["target_eth_exposure_found"])
        self.assertFalse(r["independent_confirmation_admitted"])
        self.assertNotIn("PRIVATE_MUST_NOT_ESCAPE", json.dumps(r))
        self.assertNotIn("987654321", json.dumps(r))
        self.assertEqual(before, p.read_bytes())
        self.assertEqual(r["files"][0]["sha256"], hashlib.sha256(before).hexdigest())

    def test_btc_or_later_eth_not_target(self):
        for value in (record("BTCUSDT"), record(first=1767225600000, last=1767312000000)):
            self.put(data=value)
            self.assertFalse(probe.inventory(self.root)["target_eth_exposure_found"])

    def test_missing_is_never_nonuse_proof(self):
        r = probe.inventory(self.root)
        self.assertTrue(r["diagnosis_completed"])
        self.assertFalse(r["non_use_proven"])
        self.assertEqual(r["files"], [])
        self.assertEqual(r["scope_ledgers_present"], [])

    def test_unknown_schema_not_silently_empty(self):
        self.put(data={"schema_version": "unknown", "secret": "DO_NOT_ECHO"})
        r = probe.inventory(self.root)
        self.assertEqual(r["files"][0]["unparsed_records"], 1)
        self.assertEqual(r["files"][0]["status"], "PARTIAL")
        self.assertNotIn("DO_NOT_ECHO", json.dumps(r))

    def test_only_allowlisted_names_opened(self):
        self.put(name="account.json")
        self.put(name="raw_prices.csv")
        self.assertEqual(probe.inventory(self.root)["files"], [])

    def test_symlink_leaf_rejected(self):
        target = self.put(name="not_allowed.json")
        (self.root/"research/market_alpha_history_report.json").symlink_to(target)
        r = probe.inventory(self.root)
        self.assertFalse(r["diagnosis_completed"])
        self.assertEqual(r["files"][0]["status"], "UNREADABLE_OR_UNSAFE")

    def test_symlink_parent_rejected(self):
        (self.root/"linked").symlink_to(self.root/"research", target_is_directory=True)
        self.put()
        fd = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(OSError):
                probe.read_relative(fd, ("linked", "market_alpha_history_report.json"))
        finally:
            os.close(fd)

    def test_duplicate_json_fields_rejected(self):
        p = self.put()
        p.write_text('{"schema_version":1,"schema_version":2}')
        self.assertFalse(probe.inventory(self.root)["diagnosis_completed"])

    def test_size_limit_stops(self):
        self.put()
        with mock.patch.object(probe, "MAX_TOTAL", 5):
            self.assertFalse(probe.inventory(self.root)["diagnosis_completed"])

    def test_entry_cap_stops(self):
        self.put()
        with mock.patch.object(probe, "MAX_ENTRIES", 0):
            self.assertFalse(probe.inventory(self.root)["diagnosis_completed"])

    def test_time_axis_and_symbols_are_strict(self):
        for s,a,b in (("SECRET",1704067200000,1735689599999),
                      ("ETHUSDT",True,1735689599999), ("ETHUSDT",1735689599999,1704067200000)):
            with self.assertRaises(ValueError):
                probe.interval(s,a,b)

    def test_domain_and_holdout_schemas(self):
        a,b=1704067200000,1735689599999
        domain={"schema_version":"research_domain_split_v2", "parameters":{"symbol":"ETHUSDT"},
                "boundaries":{"development_start_ts_ms":a,"holdout_end_ts_ms":b}}
        ledger={"schema_version":"final_holdout_consumption_v2","symbol":"ETHUSDT",
                "holdout_start_ts_ms":a,"holdout_end_ts_ms":b}
        self.assertEqual(probe.extract(domain),probe.extract(ledger))

    def test_workflow_embedded_python_and_manual_only_boundary(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root/".github/workflows/research-input-inventory.yml").read_text()
        run = workflow.split("        run: |\n", 1)[1].split("\n      - name: Retain", 1)[0]
        lines = textwrap.dedent(run).strip().splitlines()
        self.assertEqual(lines[0], "python3 -B - <<'PY'")
        self.assertEqual(lines[-1], "PY")
        ast.parse("\n".join(lines[1:-1]))
        self.assertIn("  workflow_dispatch:\n", workflow)
        for forbidden in ("  push:", "  schedule:", "  pull_request:", "sudo ", "docker ", "systemctl "):
            self.assertNotIn(forbidden, workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("contents: read", workflow)


if __name__ == "__main__":
    unittest.main()
