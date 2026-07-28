#!/usr/bin/env python3

import importlib.util
import argparse
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("model_registry.py")
    spec = importlib.util.spec_from_file_location("model_registry", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REGISTRY = load_module()


class ModelRegistryTest(unittest.TestCase):
    def test_final_holdout_consumption_binds_candidate_and_verified_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            ledger = root / "holdout.jsonl"
            candidate_sha256 = "a" * 64
            claim = {
                "schema_version": "final_holdout_consumption_v2",
                "experiment_id": "experiment-final-1",
                "candidate_identity_sha256": candidate_sha256,
                "symbol": "SOLUSDT",
                "bar_interval_ms": 300000,
                "holdout_start_ts_ms": 1000,
                "holdout_end_ts_ms": 2000,
                "dataset_path": str(root / "holdout.csv"),
                "dataset_sha256": "b" * 64,
                "opened_at_utc": "2026-07-28T00:00:00Z",
                "status": "opened_before_evaluation",
                "previous_entry_sha256": "0" * 64,
            }
            claim["entry_sha256"] = hashlib.sha256(
                json.dumps(
                    claim,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            ledger.write_text(
                json.dumps(
                    claim,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            checkpoint = ledger.with_suffix(
                ledger.suffix + ".checkpoint.json"
            )
            checkpoint.write_text(
                json.dumps(
                    {
                        "schema_version": "final_holdout_checkpoint_v1",
                        "entry_count": 1,
                        "tail_entry_sha256": claim["entry_sha256"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            binding = {
                "schema_version": "final_holdout_consumption_binding_v1",
                "ledger_path": str(ledger),
                "experiment_id": "experiment-final-1",
                "claimed_before_evaluation": True,
                "claims": [claim],
            }

            reasons, symbols = REGISTRY.validate_final_holdout_consumption(
                binding,
                candidate_identity_sha256=candidate_sha256,
            )
            self.assertEqual(reasons, [])
            self.assertEqual(symbols, ["SOLUSDT"])

            reasons, _ = REGISTRY.validate_final_holdout_consumption(
                binding,
                candidate_identity_sha256="c" * 64,
            )
            self.assertTrue(
                any("identity mismatch" in reason for reason in reasons),
                reasons,
            )

            checkpoint.write_text(
                json.dumps(
                    {
                        "schema_version": "final_holdout_checkpoint_v1",
                        "entry_count": 0,
                        "tail_entry_sha256": claim["entry_sha256"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            reasons, _ = REGISTRY.validate_final_holdout_consumption(
                binding,
                candidate_identity_sha256=candidate_sha256,
            )
            self.assertTrue(
                any("checkpoint mismatch" in reason for reason in reasons),
                reasons,
            )

    def test_economic_objective_contract_rejects_semantic_drift(self):
        policy_sha256 = "a" * 64
        trade_bot_sha256 = "b" * 64
        contract = {
            "schema_version": "economic_objective_contract_v1",
            "primary_metric": "mean_realized_net_per_fill",
            "authoritative_execution": "cpp_trade_bot_replay",
            "fill_model": "next_bar_ohlc_first_touch_v1",
            "terminal_position_policy": (
                "force_close_and_charge_exit_cost"
            ),
            "funding_policy": "per_bar_rate_from_replay_dataset",
            "accounting_source": "replay_terminal_account_state",
            "gross_pnl_formula": "realized_net_plus_fee_plus_funding_paid",
            "fee_sensitivity_formula": "gross_minus_funding_paid_minus_scaled_fee",
            "funding_sensitivity_policy": "fixed_while_scaling_fee",
            "fill_count_source": "all_fill_applied_events_current_boot",
            "terminal_settlement_evidence_required": True,
            "incomplete_economics_policy": "hard_fail",
            "state_isolation_policy": "fresh_wal_per_symbol_segment",
            "cost_policy_source": "execution_policy_v2",
            "execution_policy_sha256": policy_sha256,
            "trade_bot_sha256": trade_bot_sha256,
            "selection_and_final_share_contract": True,
            "thresholds": {
                "assess_stage": "S5",
                "min_runtime_status": 30,
                "min_execution_active_runs": 3,
                "min_execution_pass_runs": 3,
                "min_total_fills": 20,
                "min_mean_realized_net_per_fill": 0.0,
                "min_break_even_fee_multiplier": 1.25,
                "warn_mean_filtered_cost_ratio": 0.8,
                "min_tradable_symbols": 2,
                "min_positive_filled_segment_ratio": (
                    REGISTRY.MIN_POSITIVE_FILLED_SEGMENT_RATIO
                ),
            },
            "segment_sampling": {
                "target_bucket": "trend",
                "selection_policy": (
                    "chronological_quantiles_without_outcome_v1"
                ),
                "max_segments": 12,
                "min_segment_bars": 96,
                "final_outcome_ranking_forbidden": True,
            },
            "implementation_sha256": {
                "replay_runner": REGISTRY.sha256_file(
                    REGISTRY.PROJECT_ROOT
                    / "tools"
                    / "run_replay_validation.py"
                ),
                "runtime_assessor": REGISTRY.sha256_file(
                    REGISTRY.PROJECT_ROOT / "tools" / "assess_run_log.py"
                ),
                "policy_contract": REGISTRY.sha256_file(
                    REGISTRY.PROJECT_ROOT
                    / "tools"
                    / "config_policy_contract.py"
                ),
            },
            "governance_contract": {
                "path": str(REGISTRY.CLOSED_LOOP_CONTRACT_PATH),
                "sha256": REGISTRY.sha256_file(
                    REGISTRY.CLOSED_LOOP_CONTRACT_PATH
                ),
            },
        }
        contract["sha256"] = hashlib.sha256(
            json.dumps(
                contract,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        self.assertEqual(
            REGISTRY.validate_economic_objective_contract(
                contract,
                execution_policy_sha256=policy_sha256,
                trade_bot_sha256=trade_bot_sha256,
            ),
            [],
        )

        drifted = json.loads(json.dumps(contract))
        drifted["fill_model"] = "optimistic_same_bar_fill"
        drifted_payload = dict(drifted)
        drifted_payload.pop("sha256", None)
        drifted["sha256"] = hashlib.sha256(
            json.dumps(
                drifted_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reasons = REGISTRY.validate_economic_objective_contract(
            drifted,
            execution_policy_sha256=policy_sha256,
            trade_bot_sha256=trade_bot_sha256,
        )
        self.assertTrue(
            any("fill_model differs" in reason for reason in reasons),
            reasons,
        )

        drifted = json.loads(json.dumps(contract))
        drifted["funding_sensitivity_policy"] = "scaled_with_fee"
        drifted_payload = dict(drifted)
        drifted_payload.pop("sha256", None)
        drifted["sha256"] = hashlib.sha256(
            json.dumps(
                drifted_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reasons = REGISTRY.validate_economic_objective_contract(
            drifted,
            execution_policy_sha256=policy_sha256,
            trade_bot_sha256=trade_bot_sha256,
        )
        self.assertTrue(
            any("funding_sensitivity_policy differs" in reason for reason in reasons),
            reasons,
        )

        drifted = json.loads(json.dumps(contract))
        drifted["implementation_sha256"]["replay_runner"] = "0" * 64
        drifted_payload = dict(drifted)
        drifted_payload.pop("sha256", None)
        drifted["sha256"] = hashlib.sha256(
            json.dumps(
                drifted_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        reasons = REGISTRY.validate_economic_objective_contract(
            drifted,
            execution_policy_sha256=policy_sha256,
            trade_bot_sha256=trade_bot_sha256,
        )
        self.assertIn(
            "replay economic objective implementation checksum mismatch: "
            "replay_runner",
            reasons,
        )

    def bind_final_holdout_contract(
        self,
        root: pathlib.Path,
        payload: dict,
        symbols: list[str],
    ) -> dict[str, pathlib.Path]:
        feature_paths: dict[str, pathlib.Path] = {}
        domain_contract_by_symbol = {}
        per_symbol_selection = {}
        for symbol in symbols:
            feature_path = root / f"{symbol}_holdout.csv"
            feature_path.write_text(
                f"timestamp,close\n1,{len(symbol)}\n",
                encoding="utf-8",
            )
            feature_paths[symbol] = feature_path
            domain_contract_by_symbol[symbol] = {
                "status": "pass",
                "holdout_feature_csv": str(feature_path),
                "holdout_feature_sha256": REGISTRY.sha256_file(feature_path),
            }
            per_symbol_selection[symbol] = {
                "selection_mode": "selection_manifest_holdout",
                "candidate_set_frozen": True,
                "corpus_written": False,
                "corpus_refreshed": False,
                "dynamic_appended_segment_count": 0,
            }

        payload["symbols"] = list(symbols)
        payload["source_symbols"] = {symbol: symbol for symbol in symbols}
        payload["feature_csv_by_symbol"] = {
            symbol: str(path) for symbol, path in feature_paths.items()
        }
        payload["feature_build"] = {
            "symbols": list(symbols),
            "domain_contract_status": "pass",
            "domain_contract_by_symbol": domain_contract_by_symbol,
            "failed_symbols": [],
            "missing_symbols": [],
        }
        payload["selection"] = {
            "selection_mode": "per_symbol",
            "per_symbol_selection": per_symbol_selection,
        }
        payload.setdefault(
            "activation_gate",
            {"status": "pass", "fail_reasons": [], "warn_reasons": []},
        )
        return feature_paths

    def test_feature_parity_gate_requires_complete_cpp_golden_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            contract = json.loads(
                REGISTRY.CLOSED_LOOP_CONTRACT_PATH.read_text(encoding="utf-8")
            )
            anchor = contract["trust_anchors"]["feature_parity_fixture"]
            report_path = root / "feature_parity.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schema_version": "feature_parity_report_v1",
                        "status": "PASS",
                        "engine": "cpp_online_feature_engine",
                        "golden_source": "python_integrator_train",
                        "bars_fixture": (
                            "tools/fixtures/feature_parity_bars_v1.csv"
                        ),
                        "expected_fixture": (
                            "tools/fixtures/feature_parity_expected_v1.tsv"
                        ),
                        "check_count": 27,
                        "passed_count": 27,
                        "max_abs_error": 1e-12,
                        "failures": [],
                        "fixture_contract": {
                            "schema_version": (
                                "feature_parity_fixture_contract_v1"
                            ),
                            "bars_fixture": anchor["bars_fixture"],
                            "bars_fixture_sha256": anchor[
                                "bars_fixture_sha256"
                            ],
                            "expected_fixture": anchor["expected_fixture"],
                            "expected_fixture_sha256": anchor[
                                "expected_fixture_sha256"
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            passed, reasons, _ = REGISTRY.gate_feature_parity_report(
                report_path, require_report=True
            )
            self.assertTrue(passed, reasons)

            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload["passed_count"] = 26
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            passed, reasons, _ = REGISTRY.gate_feature_parity_report(
                report_path, require_report=True
            )
            self.assertFalse(passed)
            self.assertTrue(
                any("coverage invalid" in item for item in reasons)
            )

            payload["passed_count"] = 27
            payload["fixture_contract"]["bars_fixture_sha256"] = "0" * 64
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            passed, reasons, _ = REGISTRY.gate_feature_parity_report(
                report_path, require_report=True
            )
            self.assertFalse(passed)
            self.assertTrue(
                any("immutable trust anchor" in item for item in reasons)
            )

    def test_research_domain_gate_binds_three_isolated_domains(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            development = root / "development.csv"
            selection = root / "selection.csv"
            holdout = root / "holdout.csv"
            for path, content in (
                (development, "timestamp,close\n1,1\n"),
                (selection, "timestamp,close\n3,2\n"),
                (holdout, "timestamp,close\n5,3\n"),
            ):
                path.write_text(content, encoding="utf-8")
            replay_path = root / "replay.json"
            replay_path.write_text(
                json.dumps(
                    {
                        "feature_csv": str(holdout),
                        "execution_evidence_contract": {
                            "evidence_role": (
                                "offline_conservative_execution_prescreen"
                            ),
                            "fill_model": (
                                "next_bar_ohlc_touch_at_limit_no_queue_position"
                            ),
                            "production_promotion_authority": False,
                            "live_candidate_episode_canary_required": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            alpha_path = root / "alpha.json"
            alpha_path.write_text(
                json.dumps(
                    {
                        "by_symbol": {
                            "SOLUSDT": {"feature_csv": str(selection)}
                        }
                    }
                ),
                encoding="utf-8",
            )
            split_path = root / "split.json"
            split_path.write_text(
                json.dumps(
                    {
                        "schema_version": "research_domain_split_v2",
                        "status": "PASS",
                        "contract": {
                            "domains_overlap": False,
                            "holdout_must_not_influence_candidate_selection": True,
                            "candidate_selection_domain": "selection_validation",
                            "economic_validation_domain": (
                                "untouched_final_holdout"
                            ),
                            "holdout_consumption_ledger_required": True,
                            "final_holdout_disjoint_from_prior_experiments": True,
                            "selection_disjoint_from_prior_final_experiments": True,
                            "prior_final_reuse_policy": (
                                "historical_training_only_never_selection_or_final"
                            ),
                        },
                        "boundaries": {
                            "development_end_ts_ms": 1,
                            "selection_start_ts_ms": 3,
                            "selection_end_ts_ms": 3,
                            "holdout_start_ts_ms": 5,
                        },
                        "holdout_consumption": {
                            "ledger_path": str(root / "holdout.jsonl"),
                            "prior_matching_entry_count": 0,
                            "last_consumed_holdout_end_ts_ms": None,
                            "current_holdout_is_fresh": True,
                        },
                        "artifacts": {
                            "development_csv": {
                                "path": str(development),
                                "sha256": REGISTRY.sha256_file(development),
                            },
                            "selection_feature_csv": {
                                "path": str(selection),
                                "sha256": REGISTRY.sha256_file(selection),
                            },
                            "holdout_feature_csv": {
                                "path": str(holdout),
                                "sha256": REGISTRY.sha256_file(holdout),
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            integrator = {
                "data": {
                    "research_domain": "development",
                    "csv_sha256": REGISTRY.sha256_file(development),
                }
            }

            passed, reasons, _ = REGISTRY.gate_research_domain_split(
                split_path,
                integrator_report=integrator,
                replay_report_path=replay_path,
                alpha_probe_report_path=alpha_path,
                require_report=True,
            )
            self.assertTrue(passed, reasons)
            self.assertEqual(reasons, [])

            holdout.write_text(
                "timestamp,close\n5,999\n", encoding="utf-8"
            )
            passed, reasons, _ = REGISTRY.gate_research_domain_split(
                split_path,
                integrator_report=integrator,
                replay_report_path=replay_path,
                alpha_probe_report_path=alpha_path,
                require_report=True,
            )
            self.assertFalse(passed)
            self.assertTrue(
                any("holdout_feature_csv checksum mismatch" in item for item in reasons)
            )

    def test_gate_accepts_complete_non_overlapping_economic_contract(self):
        gate_pass, fail_reasons, _, summary = REGISTRY.gate_integrator_report(
            report={
                "data": {
                    "training_symbol": "SOLUSDT",
                    "bar_interval_ms": 300000,
                    "online_bar_source": "closed_ohlcv",
                    "source_venue": "bybit",
                    "source_category": "linear",
                    "price_type": "trade_price",
                    "volume_unit": "base_asset",
                    "time_axis_quality": {"pass": True},
                },
                "anti_leakage": {
                    "split_axis": "raw_bar_index_before_label_filter",
                    "oos_windows_non_overlapping": True,
                },
                "train_config": {
                    "label_round_trip_cost_bps": 13.0,
                    "execution_latency_bars": 1,
                },
                "metrics_oos": {
                    "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                    "evidence_tier": "offline_model_economic_prescreen",
                    "authoritative_promotion_evidence": "live_candidate_episode_canary",
                    "required_offline_prescreen": (
                        "independent_cpp_replay_next_bar_ohlc_touch"
                    ),
                    "mean_model_net_edge_bps": 0.20,
                    "median_model_net_edge_bps": 0.10,
                    "positive_model_net_edge_ratio": 0.60,
                    "model_net_objective_sample_count": 400,
                    "model_net_total_trades": 30,
                    "model_net_active_bar_count": 300,
                    "positive_model_net_edge_ratio_by_split": 0.60,
                    "model_net_edge_lcb_bps": 0.01,
                    "model_net_edge_lcb_method": "non_overlapping_oos_split_student_t_95",
                    "oos_duplicate_bar_ratio": 0.0,
                    "net_objective_round_trip_cost_bps": 13.0,
                    "auc_mean": 0.51,
                    "delta_auc_vs_baseline": 0.01,
                    "split_trained_count": 5,
                    "split_count": 5,
                    "split_trained_ratio": 1.0,
                },
                "governance": {
                    "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                    "pass": True,
                    "fail_reasons": [],
                    "warn_reasons": [],
                },
                "model_artifact_status": "published",
            },
            min_auc_mean=0.48,
            min_delta_auc_vs_baseline=0.0,
            min_mean_model_net_edge_bps=0.0,
            min_positive_model_net_edge_ratio=0.50,
            min_split_trained_count=1,
            min_split_trained_ratio=0.50,
            min_model_net_total_trades=20,
            min_model_net_active_bars=100,
            min_positive_model_net_splits_ratio=0.50,
            min_model_net_edge_lcb_bps=0.0,
        )

        self.assertTrue(gate_pass, fail_reasons)
        self.assertEqual(fail_reasons, [])
        self.assertEqual(
            summary["primary_objective"], REGISTRY.EXPECTED_MODEL_OBJECTIVE
        )

    def test_gate_integrator_report_propagates_governance_fail_reasons(self):
        gate_pass, fail_reasons, warn_reasons, summary = REGISTRY.gate_integrator_report(
            report={
                "metrics_oos": {
                    "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                    "mean_model_net_edge_bps": 0.20,
                    "median_model_net_edge_bps": 0.10,
                    "positive_model_net_edge_ratio": 0.60,
                    "model_net_objective_sample_count": 100,
                    "auc_mean": 0.51,
                    "delta_auc_vs_baseline": 0.02,
                    "split_trained_count": 5,
                    "split_count": 5,
                    "split_trained_ratio": 1.0,
                    "auc_stdev": 0.12,
                    "train_test_auc_gap_mean": 0.18,
                    "random_label_auc": 0.58,
                    "random_label_auc_mean": 0.57,
                    "random_label_auc_stdev": 0.03,
                    "random_label_auc_max": 0.61,
                },
                "governance": {
                    "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                    "pass": False,
                    "fail_reasons": [
                        "auc_stdev=0.120000 > max_auc_stdev=0.080000",
                        "random_label_auc=0.580000 > max_random_label_auc=0.550000",
                    ],
                    "warn_reasons": [
                        "random_label_auc_max=0.610000 > soft_cap=0.580000",
                    ],
                },
            },
            min_auc_mean=0.48,
            min_delta_auc_vs_baseline=0.0,
            min_mean_model_net_edge_bps=0.0,
            min_positive_model_net_edge_ratio=0.50,
            min_split_trained_count=1,
            min_split_trained_ratio=0.5,
        )

        self.assertFalse(gate_pass)
        self.assertIn("integrator_report.governance.pass != true", fail_reasons)
        self.assertIn(
            "governance: auc_stdev=0.120000 > max_auc_stdev=0.080000",
            fail_reasons,
        )
        self.assertIn(
            "governance: random_label_auc=0.580000 > max_random_label_auc=0.550000",
            fail_reasons,
        )
        self.assertIn(
            "governance: random_label_auc_max=0.610000 > soft_cap=0.580000",
            warn_reasons,
        )
        self.assertEqual(summary["auc_mean"], 0.51)
        self.assertEqual(summary["auc_stdev"], 0.12)
        self.assertEqual(summary["train_test_auc_gap_mean"], 0.18)
        self.assertEqual(summary["random_label_auc"], 0.58)
        self.assertEqual(summary["random_label_auc_mean"], 0.57)
        self.assertEqual(summary["random_label_auc_stdev"], 0.03)
        self.assertEqual(summary["random_label_auc_max"], 0.61)

    def test_replay_fail_prevents_activation_even_when_integrator_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            model_file = root / "integrator_latest.cbm"
            integrator_report = root / "integrator_report.json"
            walkforward_report = root / "walkforward_report.json"
            replay_report = root / "replay_validation_report.json"
            registration_out = root / "registry_out.json"

            model_file.write_bytes(b"fake model")
            integrator_report.write_text(
                json.dumps(
                    {
                        "model_version": "integrator_cb_v1",
                        "feature_schema_version": "feature_schema_v1",
                        "factor_set_version": "factor_set_v1",
                        "metrics_oos": {
                            "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                            "mean_model_net_edge_bps": 0.20,
                            "median_model_net_edge_bps": 0.10,
                            "positive_model_net_edge_ratio": 0.60,
                            "model_net_objective_sample_count": 100,
                            "auc_mean": 0.56,
                            "delta_auc_vs_baseline": 0.02,
                            "split_trained_count": 5,
                            "split_count": 5,
                            "split_trained_ratio": 1.0,
                        },
                        "governance": {
                            "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                            "pass": True,
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            walkforward_report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "avg_split_return": 0.001,
                            "enabled_avg_split_return": 0.001,
                            "traded_avg_split_return": 0.001,
                            "traded_split_count": 4,
                            "total_trades": 20,
                        }
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "fail",
                        "fail_reasons": ["ROBUST filled replay segments are all net-negative"],
                        "warn_reasons": [],
                        "aggregate_validation": {
                            "execution_active_runs": 10,
                            "execution_pass_runs": 10,
                            "total_fills": 10,
                            "negative_realized_net_with_fills_runs": 10,
                            "mean_realized_net_per_fill": -0.001,
                        },
                    }
                ),
                encoding="utf-8",
            )

            code = REGISTRY.run_register(
                argparse.Namespace(
                    model_file=str(model_file),
                    integrator_report=str(integrator_report),
                    miner_report="",
                    walkforward_report=str(walkforward_report),
                    replay_validation_report=str(replay_report),
                    registry_dir=str(root / "registry"),
                    max_versions=20,
                    active_model_path=str(root / "active" / "integrator_latest.cbm"),
                    active_report_path=str(root / "active" / "integrator_report.json"),
                    active_miner_report_path=str(root / "active" / "miner_report.json"),
                    active_meta_path=str(root / "active" / "integrator_active.json"),
                    min_auc_mean=0.50,
                    min_delta_auc_vs_baseline=0.0,
                    min_mean_model_net_edge_bps=0.0,
                    min_positive_model_net_edge_ratio=0.50,
                    min_split_trained_count=1,
                    min_split_trained_ratio=0.5,
                    activate_on_pass=True,
                    require_walkforward_positive=True,
                    min_walkforward_avg_split_return=0.0,
                    min_walkforward_enabled_avg_split_return=0.0,
                    min_walkforward_traded_avg_split_return=0.0,
                    require_replay_validation_pass=True,
                    registration_out=str(registration_out),
                )
            )

            self.assertEqual(code, 3)
            payload = json.loads(registration_out.read_text(encoding="utf-8"))
            self.assertFalse(payload["activated"])
            self.assertFalse(payload["gate"]["pass"])
            self.assertFalse((root / "active" / "integrator_latest.cbm").exists())
            self.assertTrue(
                any(
                    "replay_validation: replay_validation status=fail != pass" in reason
                    for reason in payload["gate"]["fail_reasons"]
                )
            )
            self.assertTrue(
                any(
                    "ROBUST filled replay segments are all net-negative" in reason
                    for reason in payload["gate"]["fail_reasons"]
                )
            )

    def test_replay_optimizer_fail_prevents_activation_even_when_replay_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            model_file = root / "integrator_latest.cbm"
            integrator_report = root / "integrator_report.json"
            replay_report = root / "replay_validation_report.json"
            registration_out = root / "registry_out.json"

            model_file.write_bytes(b"fake model")
            integrator_report.write_text(
                json.dumps(
                    {
                        "model_version": "integrator_cb_v1",
                        "feature_schema_version": "feature_schema_v1",
                        "factor_set_version": "factor_set_v1",
                        "metrics_oos": {
                            "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                            "mean_model_net_edge_bps": 0.20,
                            "median_model_net_edge_bps": 0.10,
                            "positive_model_net_edge_ratio": 0.60,
                            "model_net_objective_sample_count": 100,
                            "auc_mean": 0.56,
                            "delta_auc_vs_baseline": 0.02,
                            "split_trained_count": 5,
                            "split_count": 5,
                            "split_trained_ratio": 1.0,
                        },
                        "governance": {
                            "primary_objective": REGISTRY.EXPECTED_MODEL_OBJECTIVE,
                            "pass": True,
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            replay_report.write_text(
                json.dumps(
                    {
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                        "execution_optimizer": {
                            "status": "fail",
                            "fail_reasons": [
                                "no_deployable_prefilter_candidate_positive_after_costs"
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            code = REGISTRY.run_register(
                argparse.Namespace(
                    model_file=str(model_file),
                    integrator_report=str(integrator_report),
                    miner_report="",
                    walkforward_report="",
                    replay_validation_report=str(replay_report),
                    registry_dir=str(root / "registry"),
                    max_versions=20,
                    active_model_path=str(root / "active" / "integrator_latest.cbm"),
                    active_report_path=str(root / "active" / "integrator_report.json"),
                    active_miner_report_path=str(root / "active" / "miner_report.json"),
                    active_meta_path=str(root / "active" / "integrator_active.json"),
                    min_auc_mean=0.50,
                    min_delta_auc_vs_baseline=0.0,
                    min_mean_model_net_edge_bps=0.0,
                    min_positive_model_net_edge_ratio=0.50,
                    min_split_trained_count=1,
                    min_split_trained_ratio=0.5,
                    activate_on_pass=True,
                    require_walkforward_positive=False,
                    min_walkforward_avg_split_return=0.0,
                    min_walkforward_enabled_avg_split_return=0.0,
                    min_walkforward_traded_avg_split_return=0.0,
                    require_replay_validation_pass=True,
                    registration_out=str(registration_out),
                )
            )

            self.assertEqual(code, 3)
            payload = json.loads(registration_out.read_text(encoding="utf-8"))
            self.assertFalse(payload["activated"])
            self.assertTrue(
                any(
                    "replay_validation: replay execution_optimizer status=fail" in reason
                    for reason in payload["gate"]["fail_reasons"]
                )
            )

    def test_replay_symbol_quarantine_is_observation_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            payload = {
                        "status": "pass",
                        "source_symbol": "BTCUSDT",
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "median_realized_net_per_fill_with_fills": 0.01,
                            "positive_filled_segment_ratio": 0.80,
                            "symbol_tradeability": {
                                "tradable_symbols": ["ETHUSDT", "SOLUSDT"],
                                "quarantined_symbols": ["BTCUSDT"],
                                "decisions": {
                                    "BTCUSDT": {"status": "quarantined"},
                                    "ETHUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    },
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    },
                                },
                            },
                        },
                    }
            self.bind_final_holdout_contract(
                root, payload, ["BTCUSDT", "ETHUSDT"]
            )
            replay_report.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertTrue(passed, fail_reasons)
            self.assertEqual(fail_reasons, [])
            self.assertEqual(warn_reasons, [])
            self.assertFalse(
                summary["symbol_quarantine_observation"]["promotion_authority"]
            )
            self.assertEqual(summary["source_symbol"], "BTCUSDT")

    def test_profitable_symbols_cannot_exempt_negative_aggregate(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            payload = {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "activation_gate": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "median_realized_net_per_fill_with_fills": -0.02,
                            "positive_filled_segment_ratio": 0.25,
                            "symbol_tradeability": {
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": ["ETHUSDT"],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.008,
                                        "positive_filled_segment_ratio": 0.75,
                                        "total_fills": 20,
                                    },
                                    "ETHUSDT": {
                                        "status": "quarantined",
                                        "median_realized_net_per_fill_with_fills": -0.05,
                                        "positive_filled_segment_ratio": 0.0,
                                    },
                                },
                            },
                        },
                    }
            self.bind_final_holdout_contract(
                root, payload, ["SOLUSDT", "ETHUSDT"]
            )
            replay_report.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertTrue(
                any(
                    "aggregate_validation median_realized_net_per_fill_with_fills"
                    in reason
                    for reason in fail_reasons
                ),
                fail_reasons,
            )
            self.assertEqual(warn_reasons, [])
            self.assertEqual(
                summary["economic_gate_basis"],
                "aggregate_validation",
            )

            payload = json.loads(replay_report.read_text(encoding="utf-8"))
            payload["feature_build"]["domain_contract_by_symbol"]["SOLUSDT"] = {
                "status": "fail"
            }
            replay_report.write_text(json.dumps(payload), encoding="utf-8")
            passed, fail_reasons, _, _ = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )
            self.assertFalse(passed)
            self.assertTrue(
                any(
                    "research-domain contract failed" in item
                    for item in fail_reasons
                )
            )

    def test_replay_holdout_feature_path_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            payload = {
                "status": "pass",
                "source_symbol": "SOLUSDT",
                "aggregate_validation": {
                    "status": "pass",
                    "fail_reasons": [],
                    "warn_reasons": [],
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.80,
                },
            }
            self.bind_final_holdout_contract(root, payload, ["SOLUSDT"])
            wrong_path = root / "wrong_holdout.csv"
            wrong_path.write_text("timestamp,close\n1,8\n", encoding="utf-8")
            payload["feature_csv_by_symbol"]["SOLUSDT"] = str(wrong_path)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(json.dumps(payload), encoding="utf-8")

            passed, fail_reasons, _, _ = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertTrue(
                any(
                    "replay holdout feature path mismatch for SOLUSDT" in reason
                    for reason in fail_reasons
                ),
                fail_reasons,
            )

    def test_replay_holdout_feature_sha_missing_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            payload = {
                "status": "pass",
                "source_symbol": "SOLUSDT",
                "aggregate_validation": {
                    "status": "pass",
                    "fail_reasons": [],
                    "warn_reasons": [],
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.80,
                },
            }
            self.bind_final_holdout_contract(root, payload, ["SOLUSDT"])
            del payload["feature_build"]["domain_contract_by_symbol"]["SOLUSDT"][
                "holdout_feature_sha256"
            ]
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(json.dumps(payload), encoding="utf-8")

            passed, fail_reasons, _, _ = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertIn(
                "replay holdout feature sha256 missing or invalid for SOLUSDT",
                fail_reasons,
            )

    def test_replay_holdout_feature_checksum_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            payload = {
                "status": "pass",
                "source_symbol": "SOLUSDT",
                "aggregate_validation": {
                    "status": "pass",
                    "fail_reasons": [],
                    "warn_reasons": [],
                    "median_realized_net_per_fill_with_fills": 0.01,
                    "positive_filled_segment_ratio": 0.80,
                },
            }
            feature_paths = self.bind_final_holdout_contract(
                root, payload, ["SOLUSDT"]
            )
            feature_paths["SOLUSDT"].write_text(
                "timestamp,close\n1,999\n",
                encoding="utf-8",
            )
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(json.dumps(payload), encoding="utf-8")

            passed, fail_reasons, _, _ = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertIn(
                "replay holdout feature checksum mismatch for SOLUSDT",
                fail_reasons,
            )

    def test_alpha_mechanism_probe_market_alpha_fail_blocks_registry_gate(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            alpha_report = root / "alpha_mechanism_probe_report.json"
            alpha_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "mechanism_control_status": "pass",
                        "market_alpha_family_status": "fail",
                        "candidate_search": {
                            "pass_candidate_count": 0,
                            "best_candidate": {"name": "trend_inverse"},
                        },
                        "deployable_candidate_manifest": {
                            "status": "fail",
                            "selected_candidate": None,
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_alpha_mechanism_probe_report(alpha_report)
            )

            self.assertFalse(passed)
            self.assertEqual(warn_reasons, [])
            self.assertIn(
                "alpha mechanism market alpha family failed holdout after cost",
                fail_reasons,
            )
            self.assertEqual(summary["market_alpha_family_status"], "fail")

    def test_replay_tradeability_pass_cannot_suppress_aggregate_failures(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            payload = {
                        "status": "fail",
                        "source_symbol": "SOLUSDT",
                        "activation_gate": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                        },
                        "aggregate_validation": {
                            "status": "fail",
                            "fail_reasons": ["aggregate median net negative"],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": ["ETHUSDT"],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.75,
                                        "total_fills": 20,
                                    },
                                    "ETHUSDT": {
                                        "status": "quarantined",
                                        "median_realized_net_per_fill_with_fills": -0.05,
                                        "positive_filled_segment_ratio": 0.0,
                                    },
                                },
                            },
                        },
                    }
            self.bind_final_holdout_contract(
                root, payload, ["SOLUSDT", "ETHUSDT"]
            )
            replay_report.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertIn("aggregate median net negative", fail_reasons)
            self.assertEqual(warn_reasons, [])
            self.assertIn(
                summary["economic_gate_basis"],
                {"aggregate_validation"},
            )
            self.assertEqual(summary["suppressed_aggregate_fail_reasons"], [])

    def test_unrerun_optimizer_candidate_cannot_suppress_aggregate_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            selected_candidate = {
                "name": "strong_liquid_q50",
                "diagnostic_only": False,
                "status": "pass",
                "aggregate_summary": {
                    "median_realized_net_per_fill_with_fills": 0.002,
                    "positive_filled_segment_ratio": 0.70,
                    "total_fills": 24,
                },
            }
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "source_symbol": "SOLUSDT",
                        "activation_gate": {
                            "status": "pass_with_actions",
                            "basis": "execution_optimizer.best_deployable_candidate",
                            "selected_candidate": selected_candidate,
                            "fail_reasons": [],
                            "warn_reasons": [
                                "activation_gate_selected_optimizer_candidate=strong_liquid_q50",
                                "aggregate_validation_failed_but_optimizer_candidate_passed: aggregate median net negative",
                            ],
                        },
                        "execution_optimizer": {
                            "status": "pass",
                            "best_deployable_candidate": selected_candidate,
                        },
                        "exit_capture": {
                            "sample_count": 3,
                            "primary_diagnosis": "exit_capture_low",
                            "mean_gross_capture_of_path_mfe": 0.05,
                        },
                        "aggregate_validation": {
                            "status": "fail",
                            "fail_reasons": ["aggregate median net negative"],
                            "warn_reasons": [],
                            "median_realized_net_per_fill_with_fills": -0.02,
                            "positive_filled_segment_ratio": 0.25,
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertTrue(
                any(
                    "diagnostic only" in reason
                    for reason in fail_reasons
                ),
                fail_reasons,
            )
            self.assertTrue(warn_reasons)
            self.assertEqual(summary["economic_gate_basis"], "aggregate_validation")

    def test_replay_activation_gate_blocking_warning_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "source_symbol": "SOLUSDT",
                        "activation_gate": {
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "warn_reasons": [
                                "execution_cost_plan.candidate_requires_rerun: lower-cost candidate needs replay rerun"
                            ],
                        },
                        "aggregate_validation": {
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "warn_reasons": [
                                "execution_cost_plan.candidate_requires_rerun"
                            ],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    }
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertTrue(warn_reasons)
            self.assertIn(
                "replay activation_gate pass_with_actions has blocking warnings",
                "; ".join(fail_reasons),
            )
            self.assertEqual(summary["activation_gate"]["status"], "pass_with_actions")

    def test_replay_tradeability_decisions_are_not_promotion_authority(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            payload = {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "median_realized_net_per_fill_with_fills": 0.01,
                            "positive_filled_segment_ratio": 0.80,
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {},
                            },
                        },
                    }
            self.bind_final_holdout_contract(root, payload, ["SOLUSDT"])
            replay_report.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertTrue(passed, fail_reasons)
            self.assertEqual(fail_reasons, [])
            self.assertEqual(warn_reasons, [])
            self.assertEqual(
                summary["economic_gate_basis"],
                "aggregate_validation",
            )

    def test_replay_exit_capture_low_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "exit_capture": {
                            "sample_count": 5,
                            "primary_diagnosis": "exit_capture_low",
                            "mean_gross_capture_of_path_mfe": 0.05,
                        },
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    }
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = (
                REGISTRY.gate_replay_validation_report(replay_report, True)
            )

            self.assertFalse(passed)
            self.assertEqual(warn_reasons, [])
            self.assertIn(
                "replay exit_capture_low: path MFE covers cost but gross capture is too low",
                fail_reasons,
            )
            self.assertEqual(summary["exit_capture"]["sample_count"], 5)

    def test_replay_skip_report_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass_with_actions",
                        "validation_skipped": True,
                        "skip_reason": "feature_store_missing",
                        "selection": {
                            "selection_mode": "not_run",
                            "stop_reason": "feature_store_missing",
                        },
                        "aggregate_validation": {
                            "status": "pass_with_actions",
                            "fail_reasons": [],
                            "warn_reasons": ["skipped"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, _, _ = REGISTRY.gate_replay_validation_report(
                replay_report,
                True,
            )

            self.assertFalse(passed)
            self.assertIn(
                "replay_validation skipped/not_run: reason=feature_store_missing",
                fail_reasons,
            )

    def test_replay_feature_build_failure_on_tradable_symbol_prevents_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            replay_report = root / "replay_validation_report.json"
            replay_report.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "source_symbol": "SOLUSDT",
                        "feature_build": {"failed_symbols": ["SOLUSDT"]},
                        "aggregate_validation": {
                            "status": "pass",
                            "fail_reasons": [],
                            "warn_reasons": [],
                            "symbol_tradeability": {
                                "status": "pass",
                                "tradable_symbols": ["SOLUSDT"],
                                "quarantined_symbols": [],
                                "decisions": {
                                    "SOLUSDT": {
                                        "status": "tradable",
                                        "median_realized_net_per_fill_with_fills": 0.01,
                                        "positive_filled_segment_ratio": 0.80,
                                        "total_fills": 20,
                                    }
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, _, _ = REGISTRY.gate_replay_validation_report(
                replay_report,
                True,
            )

            self.assertFalse(passed)
            self.assertIn(
                "replay real-market feature build failed for final holdout candidate symbols=SOLUSDT",
                fail_reasons,
            )

    def test_walkforward_focus_bucket_does_not_waive_global_negative_returns(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            walkforward_report = root / "walkforward_report.json"
            walkforward_report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "avg_split_return": -0.001,
                            "enabled_avg_split_return": -0.001,
                            "traded_avg_split_return": -0.001,
                            "traded_split_count": 5,
                            "total_trades": 10,
                            "regime_bucket_summary": {
                                "trend": {
                                    "bars": 1500,
                                    "trades": 4,
                                    "sharpe": 2.0,
                                },
                                "range": {"bars": 2000, "trades": 6, "sharpe": -2.0},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = REGISTRY.gate_walkforward_report(
                walkforward_report,
                True,
                0.0,
                0.0,
                0.0,
                focus_bucket="trend",
                min_focus_bucket_bars=1000,
                min_focus_bucket_trades=1,
                min_focus_bucket_sharpe=0.0,
            )

            self.assertFalse(passed)
            self.assertTrue(fail_reasons)
            self.assertEqual(warn_reasons, [])
            self.assertEqual(
                summary["focus_bucket_validation"]["status"],
                "pass",
            )

    def test_walkforward_focus_bucket_primary_downgrades_global_negative_returns(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            walkforward_report = root / "walkforward_report.json"
            walkforward_report.write_text(
                json.dumps(
                    {
                        "summary": {
                            "avg_split_return": -0.001,
                            "enabled_avg_split_return": -0.001,
                            "traded_avg_split_return": -0.001,
                            "traded_split_count": 5,
                            "total_trades": 10,
                            "regime_bucket_summary": {
                                "trend": {
                                    "bars": 1500,
                                    "trades": 4,
                                    "sharpe": 2.0,
                                },
                                "range": {"bars": 2000, "trades": 6, "sharpe": -2.0},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            passed, fail_reasons, warn_reasons, summary = REGISTRY.gate_walkforward_report(
                walkforward_report,
                True,
                0.0,
                0.0,
                0.0,
                focus_bucket="trend",
                min_focus_bucket_bars=1000,
                min_focus_bucket_trades=1,
                min_focus_bucket_sharpe=0.0,
                focus_bucket_primary=True,
            )

            self.assertTrue(passed)
            self.assertEqual(fail_reasons, [])
            self.assertTrue(warn_reasons)
            self.assertTrue(summary["focus_bucket_validation"]["primary"])


if __name__ == "__main__":
    unittest.main()
