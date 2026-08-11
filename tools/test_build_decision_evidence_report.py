#!/usr/bin/env python3

from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import build_decision_evidence_report as builder  # noqa: E402


BENCHMARK_ID = "1" * 64
SUBSYSTEMS = ("miner", "market_alpha", "microstructure", "online_tuner")
UNSET = object()


def benchmark_report():
    return {
        "schema_version": "decision_evidence_benchmark_validation_v1",
        "identity_status": "VERIFIED",
        "benchmark_id": BENCHMARK_ID,
        "drifts": [],
    }


def alignment_report(statuses=None):
    statuses = statuses or {name: "ALIGNED" for name in SUBSYSTEMS}
    if any(status == "UNVERIFIABLE" for status in statuses.values()):
        overall = "UNVERIFIABLE"
    elif all(status == "ALIGNED" for status in statuses.values()):
        overall = "ALIGNED"
    else:
        overall = "NOT_ALIGNED"
    return {
        "schema_version": "objective_alignment_validation_v1",
        "benchmark_id": BENCHMARK_ID,
        "expected_benchmark_id": BENCHMARK_ID,
        "actual_benchmark_id": BENCHMARK_ID,
        "overall_status": overall,
        "subsystems": {
            name: {
                "status": statuses[name],
                "rho": 0.9 if statuses[name] == "ALIGNED" else -0.2,
                "p_value": 0.001 if statuses[name] == "ALIGNED" else 0.8,
            }
            for name in SUBSYSTEMS
        },
    }


def uplift_report(status="UPLIFT_PROVEN"):
    return {
        "schema_version": "evolution_uplift_validation_v1",
        "status": status,
        "benchmark_id": BENCHMARK_ID,
        "expected_benchmark_id": BENCHMARK_ID,
        "actual_benchmark_id": BENCHMARK_ID,
        "bootstrap": {"lower_confidence_bound": 0.2},
    }


def ledger_report(decision="ALLOW_NEXT_EXPERIMENT"):
    return {
        "schema_version": "experiment_budget_ledger_decision_v1",
        "operation": "audit-next",
        "decision": decision,
        "appended": False,
        "benchmark_id": BENCHMARK_ID,
        "expected_benchmark_id": BENCHMARK_ID,
        "actual_benchmark_id": BENCHMARK_ID,
        "remaining_budgets": {"family": 2, "information_set": 7},
        "reasons": [],
    }


def alpha_route_report(status="FAIL"):
    return {
        "schema_version": "alpha_source_route_v1",
        "status": status,
        "selected_route": None,
        "reason": "no_independently_gated_alpha_source_ready",
    }


class DecisionEvidenceReportTest(unittest.TestCase):
    def build(
        self,
        benchmark=UNSET,
        alignment=UNSET,
        uplift=UNSET,
        ledger=UNSET,
        alpha_route=UNSET,
    ):
        return builder.build_report(
            benchmark_report() if benchmark is UNSET else benchmark,
            alignment_report() if alignment is UNSET else alignment,
            uplift_report() if uplift is UNSET else uplift,
            ledger_report() if ledger is UNSET else ledger,
            alpha_route_report() if alpha_route is UNSET else alpha_route,
        )

    def assert_no_authority(self, report):
        self.assertFalse(report["promotion_authority"])
        self.assertFalse(report["demo_activation_authorized"])
        self.assertFalse(report["live_activation_authorized"])
        self.assertTrue(report["research_decision_only"])

    def test_all_three_decisive_evidence_channels_must_pass_to_continue(self):
        report = self.build()

        self.assertEqual(report["research_decision"], "CONTINUE")
        self.assertEqual(report["reason_codes"], ["DECISIVE_EVIDENCE_ALL_PASSED"])
        self.assertEqual(report["benchmark"]["status"], "VERIFIED")
        self.assertEqual(report["alignment"]["status"], "ALIGNED")
        self.assertEqual(report["uplift"]["status"], "UPLIFT_PROVEN")
        self.assertEqual(report["ledger"]["status"], "ALLOW_NEXT_EXPERIMENT")
        self.assert_no_authority(report)

    def test_complete_negative_evidence_changes_information_set(self):
        cases = []
        statuses = {name: "ALIGNED" for name in SUBSYSTEMS}
        statuses["market_alpha"] = "NOT_ALIGNED"
        cases.append(
            (
                {"alignment": alignment_report(statuses)},
                ["ALIGNMENT_MARKET_ALPHA_NOT_ALIGNED"],
            )
        )
        cases.append(
            ({"uplift": uplift_report("NOT_PROVEN")}, ["UPLIFT_NOT_PROVEN"])
        )
        cases.append(
            (
                {"ledger": ledger_report("STOP_CURRENT_FAMILY")},
                ["LEDGER_STOP_CURRENT_FAMILY"],
            )
        )
        for overrides, reasons in cases:
            with self.subTest(reasons=reasons):
                report = self.build(**overrides)
                self.assertEqual(report["research_decision"], "CHANGE_INFORMATION_SET")
                self.assertEqual(report["reason_codes"], reasons)
                self.assert_no_authority(report)

        statuses = {name: "NOT_ALIGNED" for name in SUBSYSTEMS}
        report = self.build(
            alignment=alignment_report(statuses),
            uplift=uplift_report("NOT_PROVEN"),
            ledger=ledger_report("STOP_CURRENT_FAMILY"),
        )
        self.assertEqual(
            report["reason_codes"],
            [
                "ALIGNMENT_MINER_NOT_ALIGNED",
                "ALIGNMENT_MARKET_ALPHA_NOT_ALIGNED",
                "ALIGNMENT_MICROSTRUCTURE_NOT_ALIGNED",
                "ALIGNMENT_ONLINE_TUNER_NOT_ALIGNED",
                "UPLIFT_NOT_PROVEN",
                "LEDGER_STOP_CURRENT_FAMILY",
            ],
        )

    def test_unverifiable_or_unknown_evidence_has_priority_and_stops(self):
        statuses = {name: "ALIGNED" for name in SUBSYSTEMS}
        statuses["miner"] = "UNVERIFIABLE"
        cases = [
            (
                {"alignment": alignment_report(statuses)},
                "ALIGNMENT_MINER_UNVERIFIABLE",
            ),
            ({"uplift": uplift_report("UNVERIFIABLE")}, "UPLIFT_UNVERIFIABLE"),
            (
                {"ledger": ledger_report("BLOCK_INVALID_LEDGER")},
                "LEDGER_BLOCK_INVALID_LEDGER",
            ),
            ({"uplift": uplift_report("MAYBE")}, "UPLIFT_UNKNOWN_STATUS"),
            (
                {"ledger": ledger_report("UNKNOWN")},
                "LEDGER_UNKNOWN_STATUS",
            ),
        ]
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                report = self.build(**overrides)
                self.assertEqual(report["research_decision"], "STOP")
                self.assertIn(reason, report["reason_codes"])
                self.assert_no_authority(report)

        invalid_benchmark = benchmark_report()
        invalid_benchmark["identity_status"] = "UNVERIFIABLE"
        report = self.build(benchmark=invalid_benchmark)
        self.assertEqual(report["research_decision"], "STOP")
        self.assertEqual(report["reason_codes"], ["BENCHMARK_UNVERIFIABLE"])

    def test_missing_or_damaged_input_only_invalidates_its_own_section(self):
        good_alignment = alignment_report()
        good_uplift = uplift_report()
        good_ledger = ledger_report()
        cases = [
            ("benchmark", None, "benchmark", "BENCHMARK_UNVERIFIABLE"),
            ("alignment", None, "alignment", "ALIGNMENT_INPUT_UNVERIFIABLE"),
            (
                "uplift",
                {"read_error": "JSONDecodeError:bad"},
                "uplift",
                "UPLIFT_INPUT_UNVERIFIABLE",
            ),
            ("ledger", [], "ledger", "LEDGER_INPUT_UNVERIFIABLE"),
        ]
        for argument, damaged, section_name, reason in cases:
            with self.subTest(argument=argument):
                kwargs = {
                    "benchmark": benchmark_report(),
                    "alignment": good_alignment,
                    "uplift": good_uplift,
                    "ledger": good_ledger,
                }
                kwargs[argument] = damaged
                report = self.build(**kwargs)
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(report[section_name]["status"], "UNVERIFIABLE")
                self.assertIn(reason, report["reason_codes"])
                if section_name != "alignment":
                    self.assertEqual(report["alignment"]["report"], good_alignment)
                if section_name != "uplift":
                    self.assertEqual(report["uplift"]["report"], good_uplift)
                if section_name != "ledger":
                    self.assertEqual(report["ledger"]["report"], good_ledger)

    def test_each_child_benchmark_mismatch_only_invalidates_that_child(self):
        cases = (
            ("alignment", alignment_report),
            ("uplift", uplift_report),
            ("ledger", ledger_report),
        )
        for section_name, factory in cases:
            with self.subTest(section=section_name):
                mismatched = factory()
                mismatched["benchmark_id"] = "2" * 64
                kwargs = {section_name: mismatched}
                report = self.build(**kwargs)
                section = report[section_name]
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(section["status"], "UNVERIFIABLE")
                self.assertEqual(section["expected_benchmark_id"], BENCHMARK_ID)
                self.assertEqual(section["actual_benchmark_id"], "2" * 64)
                self.assertFalse(section["benchmark_match"])
                for other in {"alignment", "uplift", "ledger"} - {section_name}:
                    self.assertNotEqual(report[other]["status"], "UNVERIFIABLE")
                    self.assertTrue(report[other]["benchmark_match"])

    def test_each_child_must_carry_its_own_primary_benchmark_id(self):
        cases = (
            ("alignment", alignment_report),
            ("uplift", uplift_report),
            ("ledger", ledger_report),
        )
        for section_name, factory in cases:
            with self.subTest(section=section_name):
                missing_id = factory()
                missing_id.pop("benchmark_id")
                report = self.build(**{section_name: missing_id})
                self.assertEqual(report["research_decision"], "STOP")
                self.assertEqual(report[section_name]["status"], "UNVERIFIABLE")
                self.assertFalse(report[section_name]["benchmark_match"])
                self.assertIn(
                    f"{section_name.upper()}_BENCHMARK_MISMATCH",
                    report["reason_codes"],
                )

    def test_failed_alpha_route_is_observation_only_and_never_skips_evidence(self):
        report = self.build(alpha_route=alpha_route_report("FAIL"))

        self.assertEqual(report["research_decision"], "CONTINUE")
        self.assertEqual(report["alpha_route_observation"]["status"], "FAIL")
        self.assertFalse(report["alpha_route_observation"]["affects_research_decision"])
        self.assertEqual(report["alignment"]["status"], "ALIGNED")
        self.assertEqual(report["uplift"]["status"], "UPLIFT_PROVEN")
        self.assertEqual(report["ledger"]["status"], "ALLOW_NEXT_EXPERIMENT")
        self.assertNotIn("SKIPPED_DUE_TO_PRIOR_FAILURE", json.dumps(report))
        self.assert_no_authority(report)

    def test_builder_is_pure_and_cli_atomically_writes_a_machine_readable_report(self):
        inputs = (
            benchmark_report(),
            alignment_report(),
            uplift_report(),
            ledger_report(),
            alpha_route_report(),
        )
        before = copy.deepcopy(inputs)
        builder.build_report(*inputs)
        self.assertEqual(inputs, before)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            names = ("benchmark", "alignment", "uplift", "ledger", "alpha")
            paths = {}
            for name, payload in zip(names, inputs):
                path = root / f"{name}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                paths[name] = path
            output = root / "decision-evidence.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(builder.__file__)),
                    "--benchmark-report",
                    str(paths["benchmark"]),
                    "--alignment-report",
                    str(paths["alignment"]),
                    "--uplift-report",
                    str(paths["uplift"]),
                    "--ledger-report",
                    str(paths["ledger"]),
                    "--alpha-route-report",
                    str(paths["alpha"]),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["research_decision"], "CONTINUE")
            self.assertEqual(json.loads(completed.stdout), written)
            self.assertEqual(list(root.glob(f"{output.name}.tmp.*")), [])

            paths["uplift"].write_text("{bad", encoding="utf-8")
            failed = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(builder.__file__)),
                    "--benchmark-report",
                    str(paths["benchmark"]),
                    "--alignment-report",
                    str(paths["alignment"]),
                    "--uplift-report",
                    str(paths["uplift"]),
                    "--ledger-report",
                    str(paths["ledger"]),
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(failed.returncode, 2, failed.stderr)
            stopped = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(stopped["research_decision"], "STOP")
            self.assertEqual(stopped["uplift"]["status"], "UNVERIFIABLE")
            self.assertEqual(stopped["alignment"]["status"], "ALIGNED")
            self.assertEqual(stopped["ledger"]["status"], "ALLOW_NEXT_EXPERIMENT")


if __name__ == "__main__":
    unittest.main()
