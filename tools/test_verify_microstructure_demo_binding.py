#!/usr/bin/env python3

import argparse
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import verify_microstructure_demo_binding as binding


class DemoBindingTest(unittest.TestCase):
    def write(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def fixtures(self, root: pathlib.Path, now: int):
        candidate = "a" * 64
        lifecycle = root / "lifecycle.json"
        self.write(lifecycle, {
            "schema_version": "microstructure_alpha_lifecycle_v1",
            "status": "PASS", "phase": "demo_ready", "candidate_id": candidate,
            "fully_verifiable": True, "promotion_eligible": False,
            "demo_entry_eligible": True, "live_promotion_eligible": False,
            "registry": {"state_sha256": "b" * 64},
            "state": {
                "candidate_id": candidate, "phase": "demo_ready",
                "artifacts": {
                    "model": {"sha256": "c" * 64},
                    "development_report": {"sha256": "d" * 64},
                },
            },
        })
        route = root / "route.json"
        self.write(route, {
            "schema_version": "alpha_source_route_v1", "status": "PASS",
            "selected_route": "microstructure_demo", "live_promotion_eligible": False,
            "sources": {"microstructure_demo": {"evidence": {
                "sha256": hashlib.sha256(lifecycle.read_bytes()).hexdigest()
            }}},
        })
        health = root / "health.json"
        self.write(health, {
            "schema_version": "microstructure_demo_policy_health_v1",
            "state": "active", "candidate_id": candidate,
            "last_heartbeat_epoch_ms": now - 100,
        })
        signal = root / "signal.json"
        self.write(signal, {
            "schema_version": "microstructure_demo_signal_v2", "status": "FLAT",
            "source": "bybit_public_websocket_v5",
            "candidate_id": candidate, "generated_at_epoch_ms": now - 100,
            "exchange_timestamp_ms": now - 1_000,
            "lifecycle_state_sha256": "b" * 64, "model_sha256": "c" * 64,
            "development_report_sha256": "d" * 64,
            "demo_entry_eligible": True, "live_promotion_eligible": False,
        })
        return route, lifecycle, health, signal

    def test_fresh_exact_candidate_binding_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            now = 1_800_000_000_000
            paths = self.fixtures(root, now)
            args = argparse.Namespace(
                route_report=str(paths[0]), lifecycle_report=str(paths[1]),
                health=str(paths[2]), signal=str(paths[3]), output="",
                max_stale_ms=10_000, now_epoch_ms=now,
            )
            payload = binding.verify(args)
            self.assertEqual(payload["status"], "PASS")
            self.assertFalse(payload["live_promotion_eligible"])

    def test_stale_signal_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            now = 1_800_000_000_000
            paths = self.fixtures(root, now)
            signal = json.loads(paths[3].read_text(encoding="utf-8"))
            signal["generated_at_epoch_ms"] = 1
            self.write(paths[3], signal)
            args = argparse.Namespace(
                route_report=str(paths[0]), lifecycle_report=str(paths[1]),
                health=str(paths[2]), signal=str(paths[3]), output="",
                max_stale_ms=10_000, now_epoch_ms=now,
            )
            self.assertEqual(binding.verify(args)["status"], "FAIL")

    def test_stale_exchange_heartbeat_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            now = 1_800_000_000_000
            paths = self.fixtures(root, now)
            signal = json.loads(paths[3].read_text(encoding="utf-8"))
            signal["exchange_timestamp_ms"] = now - 20_000
            self.write(paths[3], signal)
            args = argparse.Namespace(
                route_report=str(paths[0]), lifecycle_report=str(paths[1]),
                health=str(paths[2]), signal=str(paths[3]), output="",
                max_stale_ms=10_000, now_epoch_ms=now,
            )
            self.assertEqual(binding.verify(args)["status"], "FAIL")

    def test_active_signal_binds_fresh_heartbeat_to_fixed_action_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            now = 1_800_000_000_000
            paths = self.fixtures(root, now)
            signal = json.loads(paths[3].read_text(encoding="utf-8"))
            signal.update(
                {
                    "status": "ACTIVE",
                    "exchange_timestamp_ms": now - 1_000,
                    "active_until_exchange_ms": now + 15_000,
                    "action": {
                        "started_exchange_ms": now - 1_000,
                        "direction": 1,
                        "horizon_seconds": 15,
                        "execution_latency_seconds": 1,
                    },
                }
            )
            self.write(paths[3], signal)
            args = argparse.Namespace(
                route_report=str(paths[0]), lifecycle_report=str(paths[1]),
                health=str(paths[2]), signal=str(paths[3]), output="",
                max_stale_ms=10_000, now_epoch_ms=now,
            )
            self.assertEqual(binding.verify(args)["status"], "PASS")

            signal["exchange_timestamp_ms"] = now
            signal["action"]["started_exchange_ms"] = now
            self.write(paths[3], signal)
            self.assertEqual(binding.verify(args)["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
