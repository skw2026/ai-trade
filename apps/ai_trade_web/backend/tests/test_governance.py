#!/usr/bin/env python3

import json
import pathlib
import tempfile
import unittest

import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.governance import GovernanceError, GovernanceService  # noqa: E402
from app.services.report_store import ReportStore  # noqa: E402


class GovernanceServiceTest(unittest.TestCase):
    def _seed_store(self, base: pathlib.Path) -> tuple[ReportStore, pathlib.Path]:
        reports = base / "reports"
        models = base / "models"
        config = base / "config"
        control = base / "control"

        (reports / "summary").mkdir(parents=True, exist_ok=True)
        models.mkdir(parents=True, exist_ok=True)
        config.mkdir(parents=True, exist_ok=True)

        (reports / "latest_runtime_assess.json").write_text(
            json.dumps(
                {
                    "stage": "DEPLOY",
                    "verdict": "PASS",
                    "metrics": {
                        "runtime_status_count": 0,
                        "critical_count": 0,
                        "ws_unhealthy_count": 0,
                    },
                    "fail_reasons": [],
                    "warn_reasons": [],
                }
            ),
            encoding="utf-8",
        )
        (reports / "latest_closed_loop_report.json").write_text(
            json.dumps({"overall_status": "PASS", "sections": {"runtime": {}}}),
            encoding="utf-8",
        )
        (reports / "latest_run_meta.json").write_text(
            json.dumps({"run_id": "20260215T010000Z", "overall_status": "PASS"}),
            encoding="utf-8",
        )
        (reports / "summary" / "daily_latest.json").write_text(
            json.dumps({"summary_type": "daily", "overall_status": "PASS"}),
            encoding="utf-8",
        )
        (reports / "summary" / "weekly_latest.json").write_text(
            json.dumps({"summary_type": "weekly", "overall_status": "PASS"}),
            encoding="utf-8",
        )

        (models / "integrator_active.json").write_text(
            json.dumps({"model_version": "integrator_cb_v1_seed"}), encoding="utf-8"
        )

        (config / "bybit.demo.evolution.yaml").write_text(
            "system:\n  id: old\nexchange:\n  platform: bybit\nrisk:\n  max_abs_notional_usd: 100\nexecution:\n  max_order_notional: 50\nstrategy:\n  signal_notional_usd: 10\n",
            encoding="utf-8",
        )

        return (
            ReportStore(reports_root=reports, models_root=models, config_root=config),
            control,
        )

    def _publish_with_preview(
        self, gov: GovernanceService, draft_id: str, actor: str = "tester"
    ) -> dict:
        preview = gov.preview_draft(draft_id)
        guard = preview["publish_guard"]
        return gov.publish_draft(
            draft_id=draft_id,
            actor=actor,
            preview_digest=guard["preview_digest"],
            confirm_phrase=guard["confirm_phrase"],
        )

    def test_state_bootstrap_and_update(self):
        with tempfile.TemporaryDirectory() as td:
            store, control = self._seed_store(pathlib.Path(td))
            gov = GovernanceService(report_store=store, control_root=control)
            state = gov.get_state()
            self.assertFalse(state["read_only_mode"])
            self.assertTrue(state["high_risk_two_man_rule"])

            updated = gov.update_state(
                {
                    "read_only_mode": True,
                    "high_risk_required_approvals": 2,
                    "high_risk_cooldown_seconds": 60,
                },
                actor="tester",
            )
            self.assertTrue(updated["read_only_mode"])
            self.assertEqual(updated["high_risk_required_approvals"], 2)
            self.assertEqual(updated["high_risk_cooldown_seconds"], 60)

    def test_create_validate_publish_and_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            store, control = self._seed_store(pathlib.Path(td))
            gov = GovernanceService(report_store=store, control_root=control)

            draft = gov.create_draft(
                profile_name="bybit.demo.evolution.yaml",
                content=(
                    "system:\n  id: new\n"
                    "exchange:\n  platform: bybit\n"
                    "risk:\n  max_abs_notional_usd: 120\n"
                    "execution:\n  max_order_notional: 55\n"
                    "strategy:\n  signal_notional_usd: 12\n"
                ),
                actor="tester",
                note="update limits",
            )
            draft_id = draft["draft_id"]
            checked = gov.validate_draft(draft_id, actor="tester")
            self.assertTrue(checked["validation"]["ok"])

            publish = self._publish_with_preview(gov, draft_id=draft_id, actor="tester")
            self.assertTrue(publish["published"])
            self.assertEqual(publish["profile_name"], "bybit.demo.evolution.yaml")
            self.assertIsNotNone(publish["backup_file"])

            rolled = gov.rollback(
                backup_file=publish["backup_file"],
                target_profile="bybit.demo.evolution.yaml",
                actor="tester",
            )
            self.assertTrue(rolled["rolled_back"])

    def test_publish_blocked_when_latest_fail(self):
        with tempfile.TemporaryDirectory() as td:
            store, control = self._seed_store(pathlib.Path(td))
            # force fail latest status
            (store.reports_root / "latest_closed_loop_report.json").write_text(
                json.dumps({"overall_status": "FAIL", "sections": {"runtime": {}}}),
                encoding="utf-8",
            )

            gov = GovernanceService(report_store=store, control_root=control)
            draft = gov.create_draft(
                profile_name="bybit.demo.evolution.yaml",
                content=(
                    "system:\n  id: new\n"
                    "exchange:\n  platform: bybit\n"
                    "risk:\n  max_abs_notional_usd: 120\n"
                    "execution:\n  max_order_notional: 55\n"
                    "strategy:\n  signal_notional_usd: 12\n"
                ),
                actor="tester",
            )
            with self.assertRaises(GovernanceError):
                self._publish_with_preview(gov, draft_id=draft["draft_id"], actor="tester")

    def test_create_draft_reject_missing_sections(self):
        with tempfile.TemporaryDirectory() as td:
            store, control = self._seed_store(pathlib.Path(td))
            gov = GovernanceService(report_store=store, control_root=control)
            draft = gov.create_draft(
                profile_name="bybit.demo.evolution.yaml",
                content="system:\n  id: bad\n",
                actor="tester",
            )
            self.assertFalse(draft["validation"]["ok"])
            self.assertIn("exchange", draft["validation"]["checks"]["missing_sections"])

    def test_preview_draft_has_diff_and_risk_flags(self):
        with tempfile.TemporaryDirectory() as td:
            store, control = self._seed_store(pathlib.Path(td))
            gov = GovernanceService(report_store=store, control_root=control)
            draft = gov.create_draft(
                profile_name="bybit.demo.evolution.yaml",
                content=(
                    "system:\n  id: new\n"
                    "exchange:\n  platform: bybit\n  testnet: true\n"
                    "risk:\n  max_abs_notional_usd: 250\n"
                    "execution:\n  max_order_notional: 120\n"
                    "strategy:\n  signal_notional_usd: 12\n"
                ),
                actor="tester",
                note="preview risk check",
            )

            preview = gov.preview_draft(draft["draft_id"])
            self.assertTrue(preview["base_exists"])
            self.assertEqual(preview["profile_name"], "bybit.demo.evolution.yaml")

            changed = {item["key"] for item in preview["diff"]["changed"]}
            self.assertIn("risk.max_abs_notional_usd", changed)
            self.assertIn("execution.max_order_notional", changed)

            flags = preview["risk_flags"]
            self.assertGreaterEqual(len(flags), 2)
            flagged_keys = {item["key"] for item in flags}
            self.assertIn("risk.max_abs_notional_usd", flagged_keys)
            self.assertIn("exchange.testnet", flagged_keys)

    def test_high_risk_publish_requires_dual_approval_and_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            store, control = self._seed_store(pathlib.Path(td))
            gov = GovernanceService(
                report_store=store,
                control_root=control,
                default_high_risk_cooldown_seconds=300,
            )
            draft = gov.create_draft(
                profile_name="bybit.demo.evolution.yaml",
                content=(
                    "system:\n  id: new\n"
                    "exchange:\n  platform: bybit\n  testnet: true\n"
                    "risk:\n  max_abs_notional_usd: 250\n"
                    "execution:\n  max_order_notional: 120\n"
                    "strategy:\n  signal_notional_usd: 12\n"
                ),
                actor="tester",
            )
            draft_id = draft["draft_id"]
            preview = gov.preview_draft(draft_id)
            guard = preview["publish_guard"]
            self.assertTrue(guard["high_risk_enforced"])

            with self.assertRaises(GovernanceError):
                gov.publish_draft(
                    draft_id=draft_id,
                    actor="tester",
                    preview_digest=guard["preview_digest"],
                    confirm_phrase=guard["confirm_phrase"],
                )

            gov.approve_draft(draft_id, actor="alice", note="checked")
            gov.approve_draft(draft_id, actor="bob", note="checked")
            preview_after_approval = gov.preview_draft(draft_id)
            guard_after_approval = preview_after_approval["publish_guard"]
            self.assertTrue(guard_after_approval["approval_satisfied"])
            self.assertGreater(guard_after_approval["cooldown_remaining_seconds"], 0)

            draft_path = control / "drafts" / f"{draft_id}.json"
            draft_payload = json.loads(draft_path.read_text(encoding="utf-8"))
            for item in draft_payload.get("approvals", []):
                item["approved_at_utc"] = "2000-01-01T00:00:00Z"
            draft_path.write_text(
                json.dumps(draft_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            publish = self._publish_with_preview(gov, draft_id=draft_id, actor="release")
            self.assertTrue(publish["published"])


if __name__ == "__main__":
    unittest.main()
