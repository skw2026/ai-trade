#!/usr/bin/env python3
"""Closure governance tests; all payoff inputs here are synthetic, not evidence."""

import copy
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import audit_option_candidate_closure as closure
import test_audit_option_lifecycle_economics_v1 as fixtures


class CandidateClosureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = fixtures.OptionLifecycleEconomicsV1Test()
        case.setUpClass()
        cls.registry = closure.decode(closure.read(closure.ROOT / "config/option_candidate_closure_v1.json"))
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / fixtures.capture.CAPTURE_ROOT_NAME
            delivery = case.archive(root, 6, premium=0.1)
            cls.negative = case.run_audit(root, delivery + 1000)
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / fixtures.capture.CAPTURE_ROOT_NAME
            delivery = case.archive(root, 6, premium=500.0)
            cls.positive = case.run_audit(root, delivery + 1000)
        for report in (cls.negative, cls.positive):
            report["identities"]["executed_release_sha"] = cls.registry["closure_anchor"]["executed_release_sha"]

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.anchor = copy.deepcopy(self.negative)
        self.current = copy.deepcopy(self.positive)
        self.record = copy.deepcopy(self.registry)

    def bundle(self):
        source = closure.decode(closure.read(closure.ROOT / "config/option_lifecycle_economic_v1.json"))["source_contract"]
        reports = {"economics.json": self.anchor}
        for name, kind, schema, decision in (
            ("report.json", "capture", "option_lifecycle_audit_v4", "PASS_FIRST_COMPLETE_LIFECYCLE_FOR_PAYOFF_RECONSTRUCTION_ONLY"),
            ("payoff.json", "payoff", "option_lifecycle_payoff_audit_v2", "RECONCILED_FIRST_LIFECYCLE_PAYOFF_FOR_DIAGNOSTIC_ONLY"),
        ):
            reports[name] = {
                "schema_version": schema, "decision": decision,
                "identities": {"executed_release_sha": self.registry["closure_anchor"]["executed_release_sha"],
                               "policy_canonical_sha256": source[f"{kind}_policy_canonical_sha256"],
                               "manifest_canonical_sha256": source[f"{kind}_manifest_canonical_sha256"]},
                **{key: False for key in closure.AUTHORITY_FIELDS},
            }
        reports["payoff.json"].update({
            "lifecycle": {"lifecycle_id": self.anchor["lifecycles"][0]["lifecycle_id"]},
            "actions": self.anchor["lifecycles"][0]["actions"],
            "profitability_evidence": False, "sharpe_evidence": False, "drawdown_evidence": False,
        })
        return reports

    def prepare(self, reports=None):
        archive = self.root / "anchor.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            for name, payload in (self.bundle() if reports is None else reports).items():
                handle.writestr(name, json.dumps(payload))
        self.record["closure_anchor"]["artifact_zip_sha256"] = closure.sha(archive.read_bytes())
        registry = self.root / "registry.json"
        registry.write_text(json.dumps(self.record), encoding="utf-8")
        current = self.root / "current.json"
        current.write_text(json.dumps(self.current), encoding="utf-8")
        return registry, archive, current

    def run_audit(self, reports=None):
        registry, archive, current = self.prepare(reports)
        with patch.object(closure, "REGISTRY_SHA256", closure.capture.canonical_sha256(self.record)):
            return closure.audit(registry_path=registry, anchor_zip=archive, current_path=current)

    def test_closed_candidate_stays_closed_after_later_batch_passes(self):
        self.current = copy.deepcopy(self.negative)
        start = self.current["lifecycles"][-1]["delivery_time_epoch_ms"] + 60000
        for index in range(12):
            row = copy.deepcopy(self.positive["lifecycles"][index % 6])
            row["lifecycle_id"] = f"synthetic-later-positive-{index}"
            row["selected_epoch_ms"] = start + index * 780000
            row["delivery_time_epoch_ms"] = row["selected_epoch_ms"] + 720000
            self.current["lifecycles"].append(row)
        self.current["progress"].update(complete_lifecycle_count=18, observed_lifecycle_count=18)
        policy = closure.decode(closure.read(closure.ROOT / "config/option_lifecycle_economic_v1.json"))
        self.current["action_aggregates"] = closure.economics.aggregate_actions(self.current["lifecycles"], policy["aggregation_contract"])
        self.current["decision"] = "PASS_MULTI_LIFECYCLE_ECONOMICS_FOR_DEMO_REVIEW_ONLY"
        self.current["demo_review_eligible"] = True
        self.current["reason_code"] = "FROZEN_STRESS_ECONOMIC_GATES_PASS"
        primary = self.current["action_aggregates"]["short_selected_straddle"]
        self.assertGreater(primary["stress_mean_lcb_bps"], 0)
        self.assertGreaterEqual(primary["stress_positive_ratio"], 2 / 3)
        report = self.run_audit()
        self.assertEqual(report["decision"], "CLOSED_CANDIDATE_NO_REOPEN")
        self.assertEqual(report["current_batch_decision"], "PASS_MULTI_LIFECYCLE_ECONOMICS_FOR_DEMO_REVIEW_ONLY")
        self.assertTrue(report["closure_latched"])
        self.assertFalse(report["demo_review_eligible"])
        self.assertFalse(report["demo_activation_authorized"])
        self.assertFalse(report["full_cashflow_attribution_complete"])
        self.assertEqual(len(report["anchor_lifecycles"]), 6)
        self.assertEqual(report["current_complete_lifecycle_count"], 18)

    def test_repeated_audit_is_identical_and_does_not_modify_inputs(self):
        registry, archive, current = self.prepare()
        before = [path.read_bytes() for path in (registry, archive, current)]
        with patch.object(closure, "REGISTRY_SHA256", closure.capture.canonical_sha256(self.record)):
            one = closure.audit(registry_path=registry, anchor_zip=archive, current_path=current)
            two = closure.audit(registry_path=registry, anchor_zip=archive, current_path=current)
        self.assertEqual(one, two)
        self.assertEqual(before, [path.read_bytes() for path in (registry, archive, current)])

    def test_production_registry_is_frozen(self):
        closure.load_registry(closure.ROOT / "config/option_candidate_closure_v1.json")
        for field, value in (("status", "OPEN"), ("candidate_experiment_id", "renamed"), ("reopening_allowed", True)):
            record = copy.deepcopy(self.registry)
            record[field] = value
            path = self.root / "drift.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "registry identity"):
                closure.load_registry(path)

    def test_corrupt_or_different_zip_rejected(self):
        registry, archive, current = self.prepare()
        archive.write_bytes(archive.read_bytes() + b"drift")
        with patch.object(closure, "REGISTRY_SHA256", closure.capture.canonical_sha256(self.record)):
            with self.assertRaisesRegex(ValueError, "ZIP digest"):
                closure.audit(registry_path=registry, anchor_zip=archive, current_path=current)

    def test_missing_or_extra_zip_member_rejected(self):
        for extra in (False, True):
            reports = self.bundle()
            if extra:
                reports["../unexpected.json"] = {}
            else:
                del reports["payoff.json"]
            with self.assertRaisesRegex(ValueError, "exactly the three"):
                self.run_audit(reports)

    def test_duplicate_zip_members_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            for name in ("report.json", "payoff.json", "economics.json", "report.json"):
                with self.assertWarns(UserWarning) if name == "report.json" and handle.namelist() else contextlib.nullcontext():
                    handle.writestr(name, "{}")
        self.record["closure_anchor"]["artifact_zip_sha256"] = closure.sha(buffer.getvalue())
        with self.assertRaisesRegex(ValueError, "exactly the three"):
            closure.load_anchor(buffer.getvalue(), self.record)

    def test_non_stop_anchor_rejected(self):
        self.anchor = copy.deepcopy(self.positive)
        with self.assertRaisesRegex(ValueError, "not a completed economic STOP"):
            self.run_audit()

    def test_aggregate_and_cashflow_mismatch_rejected(self):
        self.anchor["action_aggregates"]["short_selected_straddle"]["stress_mean_bps"] += 1
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.run_audit()
        self.anchor = copy.deepcopy(self.negative)
        self.anchor["lifecycles"][0]["actions"][1]["base_net_pnl_usdt"] += 1
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.run_audit()

    def test_duplicate_lifecycles_and_wrong_population_rejected(self):
        self.anchor["lifecycles"][1]["lifecycle_id"] = self.anchor["lifecycles"][0]["lifecycle_id"]
        with self.assertRaisesRegex(ValueError, "duplicate lifecycle"):
            self.run_audit()
        self.anchor = copy.deepcopy(self.negative)
        self.anchor["progress"]["complete_lifecycle_count"] = 7
        with self.assertRaisesRegex(ValueError, "progress mismatch"):
            self.run_audit()

    def test_claim_and_release_drift_rejected(self):
        self.current["live_activation_authorized"] = True
        with self.assertRaisesRegex(ValueError, "claim/authority"):
            self.run_audit()
        self.current = copy.deepcopy(self.positive)
        self.anchor["identities"]["executed_release_sha"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "anchor release"):
            self.run_audit()

    def test_new_name_or_changed_contract_cannot_reopen(self):
        for key in ("experiment_id", "policy_canonical_sha256"):
            self.current = copy.deepcopy(self.positive)
            self.current["identities"][key] = "unregistered"
            with self.assertRaisesRegex(ValueError, "identity"):
                self.run_audit()

    def test_nonfinite_duplicate_json_and_bool_money_rejected(self):
        for raw in (b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with self.assertRaises(ValueError):
                closure.decode(raw)
        self.anchor["lifecycles"][0]["actions"][1]["gross_pnl_usdt"] = True
        with self.assertRaisesRegex(ValueError, "finite numeric"):
            self.run_audit()

    def test_annotations_are_bounded_allowlisted_and_lossless(self):
        self.anchor["private_unexpected_field"] = "do-not-publish"
        report = self.run_audit()
        lines = closure.annotations(report)
        self.assertEqual(len(lines), 8)
        self.assertNotIn("do-not-publish", "".join(lines))
        for line in lines:
            self.assertLess(len(line.encode()), 3600)
            json.loads(line.split("::", 2)[2])
        self.assertEqual(len([line for line in lines if '"kind":"lifecycle"' in line]), 6)

    def test_cli_missing_anchor_fails_without_success_output(self):
        output = self.root / "output.json"
        result = subprocess.run([sys.executable, str(closure.ROOT / "tools/audit_option_candidate_closure.py"),
                                 "--registry", str(closure.ROOT / "config/option_candidate_closure_v1.json"),
                                 "--anchor-zip", str(self.root / "missing.zip"), "--current", str(self.root / "missing.json"),
                                 "--output", str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
