#!/usr/bin/env python3
"""
Audit whether the closed-loop self-optimization mechanism is actually proven.

This is intentionally one level above the ordinary strategy/replay/runtime gates:
it answers whether the loop can reject noise, accept a known-good objective,
optimize one economic target, and show that the model/optimizer influenced live
decisions. A strategy can be temporarily unprofitable; a mechanism that cannot
prove those properties is not ready for more parameter tuning.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


SCHEMA_VERSION = "closed_loop_mechanism_audit_v1"
DEFAULT_COST_BPS = 3.5
DEFAULT_MIN_SYNTHETIC_NET_BPS = 0.5
DEFAULT_MIN_LIVE_POLICY_APPLIED = 1
DEFAULT_MIN_REPLAY_TOTAL_FILLS = 20
EXPECTED_MODEL_OBJECTIVE = (
    "aggregate_model_net_bps_per_unit_turnover_after_cost"
)


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json_optional(path_text: str) -> Dict[str, Any]:
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        value_float = float(value)
        return value_float if math.isfinite(value_float) else None
    if isinstance(value, str):
        try:
            value_float = float(value)
        except ValueError:
            return None
        return value_float if math.isfinite(value_float) else None
    return None


def as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def json_status(fail_reasons: List[str], warn_reasons: List[str] | None = None) -> str:
    if fail_reasons:
        return "fail"
    if warn_reasons:
        return "pass_with_actions"
    return "pass"


def summarize_objective_samples(
    returns_bps: List[float],
    signals: List[int],
    cost_bps: float,
) -> Dict[str, Any]:
    samples = []
    for ret, sig in zip(returns_bps, signals):
        if sig == 0:
            continue
        samples.append(float(sig) * float(ret) - cost_bps)
    if not samples:
        return {
            "sample_count": 0,
            "mean_net_bps": None,
            "positive_ratio": None,
        }
    positives = sum(1 for item in samples if item > 0.0)
    return {
        "sample_count": len(samples),
        "mean_net_bps": sum(samples) / len(samples),
        "positive_ratio": positives / len(samples),
    }


def run_synthetic_controls(cost_bps: float) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    rng = random.Random(42)
    returns = [rng.gauss(0.0, 12.0) for _ in range(1024)]
    negative_signals = [1 if rng.random() >= 0.5 else -1 for _ in returns]
    negative = summarize_objective_samples(returns, negative_signals, cost_bps)
    neg_mean = as_float(negative.get("mean_net_bps"))
    negative_fail: List[str] = []
    if neg_mean is None:
        negative_fail.append("synthetic negative control produced no samples")
    elif neg_mean > 0.0:
        negative_fail.append(
            f"synthetic negative control passed unexpectedly: mean_net_bps={neg_mean:.6f}"
        )
    negative["status"] = json_status(negative_fail)
    negative["fail_reasons"] = negative_fail
    negative["control_type"] = "random_signal_vs_random_return"

    # A deliberately learnable positive-control signal. The point is not market
    # realism; it is ensuring the economic objective can accept a known-good
    # signal after paying the configured cost.
    positive_signals = [1 if ret >= 0.0 else -1 for ret in returns]
    positive = summarize_objective_samples(returns, positive_signals, cost_bps)
    pos_mean = as_float(positive.get("mean_net_bps"))
    positive_fail: List[str] = []
    if pos_mean is None:
        positive_fail.append("synthetic positive control produced no samples")
    elif pos_mean <= DEFAULT_MIN_SYNTHETIC_NET_BPS:
        positive_fail.append(
            "synthetic positive control did not clear net objective: "
            f"mean_net_bps={pos_mean:.6f} <= {DEFAULT_MIN_SYNTHETIC_NET_BPS:.6f}"
        )
    positive["status"] = json_status(positive_fail)
    positive["fail_reasons"] = positive_fail
    positive["control_type"] = "oracle_direction_after_cost"
    positive["min_mean_net_bps"] = DEFAULT_MIN_SYNTHETIC_NET_BPS
    return negative, positive


def audit_negative_control(integrator: Dict[str, Any], cost_bps: float) -> Dict[str, Any]:
    synthetic_negative, _ = run_synthetic_controls(cost_bps)
    fail_reasons = list(synthetic_negative.get("fail_reasons", []))
    warn_reasons: List[str] = []

    metrics = integrator.get("metrics_oos", {}) if isinstance(integrator, dict) else {}
    governance = integrator.get("governance", {}) if isinstance(integrator, dict) else {}
    thresholds = governance.get("thresholds", {}) if isinstance(governance, dict) else {}
    random_label_enabled = bool(thresholds.get("run_random_label_control"))
    random_label_trials = as_int(metrics.get("random_label_trials"))
    random_label_mean = as_float(metrics.get("random_label_auc_mean"))
    random_label_max = as_float(metrics.get("random_label_auc_max"))
    max_allowed = as_float(thresholds.get("max_random_label_auc"))

    if not integrator:
        fail_reasons.append("integrator_report missing; cannot verify random-label negative control")
    elif not random_label_enabled:
        fail_reasons.append("integrator random-label control disabled")
    elif random_label_trials <= 0:
        fail_reasons.append("integrator random-label control has zero trials")
    elif random_label_mean is None:
        fail_reasons.append("integrator random_label_auc_mean missing")
    elif max_allowed is not None and random_label_mean > max_allowed:
        fail_reasons.append(
            f"integrator random_label_auc_mean={random_label_mean:.6f} > max_allowed={max_allowed:.6f}"
        )

    if (
        random_label_max is not None
        and max_allowed is not None
        and random_label_max > max_allowed + 0.03
    ):
        warn_reasons.append(
            f"integrator random_label_auc_max={random_label_max:.6f} exceeds soft cap={max_allowed + 0.03:.6f}"
        )

    return {
        "status": json_status(fail_reasons, warn_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "synthetic_negative_control": synthetic_negative,
        "integrator_random_label_control": {
            "enabled": random_label_enabled,
            "trials": random_label_trials,
            "auc_mean": random_label_mean,
            "auc_max": random_label_max,
            "max_allowed": max_allowed,
        },
    }


def audit_positive_control(cost_bps: float) -> Dict[str, Any]:
    _, synthetic_positive = run_synthetic_controls(cost_bps)
    return {
        "status": synthetic_positive.get("status"),
        "fail_reasons": synthetic_positive.get("fail_reasons", []),
        "warn_reasons": [],
        "synthetic_positive_control": synthetic_positive,
        "scope": "objective_sanity_only",
        "note": (
            "This proves the net objective can accept a known-good signal; it does "
            "not prove the live alpha pipeline is profitable."
        ),
    }


def audit_alpha_mechanism_probe(probe: Dict[str, Any]) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    if not probe:
        fail_reasons.append("alpha_mechanism_probe_report missing")
        return {
            "status": json_status(fail_reasons),
            "fail_reasons": fail_reasons,
            "warn_reasons": warn_reasons,
            "observed": {},
        }

    mechanism_status = str(probe.get("mechanism_control_status", "")).lower()
    market_status = str(probe.get("market_alpha_family_status", "")).lower()
    if mechanism_status != "pass":
        fail_reasons.append(
            f"alpha mechanism controls did not pass: mechanism_control_status={mechanism_status or 'missing'}"
        )
    if market_status == "fail":
        fail_reasons.append(
            "alpha mechanism real market alpha family failed holdout: "
            "market_alpha_family_status=fail"
        )
    elif market_status not in {"pass", "pass_with_actions"}:
        warn_reasons.append(f"alpha mechanism market alpha status={market_status or 'unknown'}")

    return {
        "status": json_status(fail_reasons, warn_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "observed": {
            "probe_status": probe.get("status"),
            "mechanism_control_status": probe.get("mechanism_control_status"),
            "market_alpha_family_status": probe.get("market_alpha_family_status"),
            "candidate_pass_count": (
                probe.get("candidate_search", {}).get("pass_candidate_count")
                if isinstance(probe.get("candidate_search"), dict)
                else None
            ),
            "best_candidate": (
                probe.get("candidate_search", {}).get("best_candidate")
                if isinstance(probe.get("candidate_search"), dict)
                else None
            ),
        },
    }


def audit_target_consistency(
    integrator: Dict[str, Any],
    registry: Dict[str, Any],
    replay: Dict[str, Any],
    strategy: Dict[str, Any],
) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []

    metrics = integrator.get("metrics_oos", {}) if isinstance(integrator, dict) else {}
    data_contract = integrator.get("data", {}) if isinstance(integrator, dict) else {}
    train_config = integrator.get("train_config", {}) if isinstance(integrator, dict) else {}
    governance = integrator.get("governance", {}) if isinstance(integrator, dict) else {}
    thresholds = governance.get("thresholds", {}) if isinstance(governance, dict) else {}
    primary_objective = str(metrics.get("primary_objective", "")).strip()
    governance_primary_objective = str(governance.get("primary_objective", "")).strip()
    mean_model_net_edge_bps = as_float(metrics.get("mean_model_net_edge_bps"))
    positive_model_net_edge_ratio = as_float(metrics.get("positive_model_net_edge_ratio"))
    min_mean_model_net_edge_bps = as_float(thresholds.get("min_mean_model_net_edge_bps"))
    min_positive_model_net_edge_ratio = as_float(
        thresholds.get("min_positive_model_net_edge_ratio")
    )

    label_cost_bps = as_float(train_config.get("label_round_trip_cost_bps"))
    label_min_edge_bps = as_float(train_config.get("label_min_net_edge_bps"))
    if label_cost_bps is None or label_cost_bps <= 0.0:
        fail_reasons.append("integrator label is not explicitly cost-aware")
    if label_min_edge_bps is None or label_min_edge_bps < 0.0:
        fail_reasons.append("integrator label_min_net_edge_bps missing")

    if primary_objective != EXPECTED_MODEL_OBJECTIVE:
        fail_reasons.append("integrator metrics primary objective is not net economic edge")
    if governance_primary_objective != EXPECTED_MODEL_OBJECTIVE:
        fail_reasons.append("integrator governance primary objective is not net economic edge")
    if mean_model_net_edge_bps is None:
        fail_reasons.append("integrator mean_model_net_edge_bps missing")
    if positive_model_net_edge_ratio is None:
        fail_reasons.append("integrator positive_model_net_edge_ratio missing")
    if min_mean_model_net_edge_bps is None:
        fail_reasons.append("integrator min_mean_model_net_edge_bps threshold missing")
    if min_positive_model_net_edge_ratio is None:
        fail_reasons.append("integrator min_positive_model_net_edge_ratio threshold missing")
    if metrics.get("evidence_tier") != "offline_model_economic_prescreen":
        fail_reasons.append("integrator evidence tier is not offline economic prescreen")
    if metrics.get("authoritative_promotion_evidence") != "live_candidate_episode_canary":
        fail_reasons.append(
            "integrator did not delegate promotion authority to live candidate episodes"
        )
    if (
        metrics.get("required_offline_prescreen")
        != "independent_cpp_replay_next_bar_ohlc_touch"
    ):
        fail_reasons.append("integrator offline replay prescreen contract is missing")
    if as_int(metrics.get("model_net_total_trades")) <= 0:
        fail_reasons.append("integrator OOS economic evidence has no trades")
    if as_int(metrics.get("model_net_active_bar_count")) <= 0:
        fail_reasons.append("integrator OOS economic evidence has no active bars")
    if as_float(metrics.get("model_net_edge_lcb_bps")) is None:
        fail_reasons.append("integrator OOS net edge confidence bound missing")
    if as_float(metrics.get("oos_duplicate_bar_ratio")) != 0.0:
        fail_reasons.append("integrator OOS windows contain duplicate bars")
    time_axis_quality = (
        data_contract.get("time_axis_quality", {})
        if isinstance(data_contract, dict)
        else {}
    )
    if (
        not isinstance(time_axis_quality, dict)
        or time_axis_quality.get("pass") is not True
    ):
        fail_reasons.append("integrator raw time axis quality is not proven")

    anti_leakage = (
        integrator.get("anti_leakage", {})
        if isinstance(integrator, dict)
        else {}
    )
    if not isinstance(anti_leakage, dict):
        anti_leakage = {}
    if anti_leakage.get("split_axis") != "raw_bar_index_before_label_filter":
        fail_reasons.append("integrator split axis is not the raw bar index")
    if anti_leakage.get("oos_windows_non_overlapping") is not True:
        fail_reasons.append("integrator OOS windows are not proven non-overlapping")

    auc_mean = as_float(metrics.get("auc_mean"))
    min_auc_mean = as_float(thresholds.get("min_auc_mean"))
    if auc_mean is not None or min_auc_mean is not None:
        warn_reasons.append(
            "integrator still reports AUC diagnostics; ensure it is not the primary promotion gate"
        )

    replay_economics = replay.get("execution_economics", {}) if isinstance(replay, dict) else {}
    replay_net = as_float(replay_economics.get("mean_realized_net_per_fill_with_fills"))
    if replay_net is None:
        fail_reasons.append("replay execution_economics.mean_realized_net_per_fill_with_fills missing")

    activation_gate = replay.get("activation_gate", {}) if isinstance(replay, dict) else {}
    if not isinstance(activation_gate, dict) or not activation_gate:
        fail_reasons.append("replay activation_gate missing")
    elif str(activation_gate.get("status", "")).lower() not in {"pass", "pass_with_actions"}:
        fail_reasons.append(f"replay activation_gate status={activation_gate.get('status')}")
    replay_execution_contract = (
        replay.get("execution_evidence_contract", {})
        if isinstance(replay, dict)
        else {}
    )
    if (
        not isinstance(replay_execution_contract, dict)
        or replay_execution_contract.get("evidence_role")
        != "offline_conservative_execution_prescreen"
        or replay_execution_contract.get("production_promotion_authority") is not False
        or replay_execution_contract.get("live_candidate_episode_canary_required")
        is not True
    ):
        fail_reasons.append("replay execution evidence role is not a conservative prescreen")

    replay_identity = replay.get("candidate_identity", {}) if isinstance(replay, dict) else {}
    if not isinstance(replay_identity, dict):
        replay_identity = {}
    integrator_model_version = str(integrator.get("model_version", "")).strip()
    if not integrator_model_version:
        fail_reasons.append("integrator model version missing")
    if replay_identity.get("config_binds_candidate") is not True:
        fail_reasons.append("replay config does not independently bind the candidate")
    if str(replay_identity.get("model_version", "")).strip() != integrator_model_version:
        fail_reasons.append("replay candidate model version differs from integrator")
    replay_model_sha = str(replay_identity.get("model_sha256", "")).strip()
    replay_report_sha = str(
        replay_identity.get("integrator_report_sha256", "")
    ).strip()
    if not replay_model_sha or not replay_report_sha:
        fail_reasons.append("replay candidate model/report checksums missing")

    registry_gate_pass = None
    if registry:
        registry_gate = registry.get("gate", {}) if isinstance(registry, dict) else {}
        registry_activation_gate = registry.get("activation_gate", {})
        registry_gate_pass = bool(registry.get("gate_pass", registry_gate.get("pass")))
        if not registry_gate_pass:
            fail_reasons.append("registry gate did not pass")
        registry_model_version = str(registry.get("model_version", "")).strip()
        if registry_model_version != integrator_model_version:
            fail_reasons.append("registry model version differs from replayed candidate")
        if not registry_activation_gate and not activation_gate:
            fail_reasons.append("registry did not record replay activation gate evidence")
        registry_checksums = registry.get("checksums", {})
        if not isinstance(registry_checksums, dict):
            registry_checksums = {}
        if str(registry_checksums.get("model_sha256", "")).strip() != replay_model_sha:
            fail_reasons.append("registry model checksum differs from replayed candidate")
        if (
            str(registry_checksums.get("integrator_report_sha256", "")).strip()
            != replay_report_sha
        ):
            fail_reasons.append("registry report checksum differs from replayed candidate")
    else:
        warn_reasons.append("registry_report missing; target consistency can only inspect replay/integrator")

    strategy_status = str(strategy.get("status", "")).lower() if isinstance(strategy, dict) else ""
    if strategy_status == "fail":
        warn_reasons.append("strategy_diagnose raw edge is negative; execution optimization may be compensating for weak alpha")

    return {
        "status": json_status(fail_reasons, warn_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "observed": {
            "integrator_auc_mean": auc_mean,
            "integrator_min_auc_mean": min_auc_mean,
            "integrator_primary_objective": primary_objective or None,
            "integrator_governance_primary_objective": governance_primary_objective or None,
            "integrator_mean_model_net_edge_bps": mean_model_net_edge_bps,
            "integrator_positive_model_net_edge_ratio": positive_model_net_edge_ratio,
            "integrator_min_mean_model_net_edge_bps": min_mean_model_net_edge_bps,
            "integrator_min_positive_model_net_edge_ratio": min_positive_model_net_edge_ratio,
            "integrator_label_round_trip_cost_bps": label_cost_bps,
            "integrator_label_min_net_edge_bps": label_min_edge_bps,
            "replay_mean_realized_net_per_fill_with_fills": replay_net,
            "replay_activation_gate_status": activation_gate.get("status")
            if isinstance(activation_gate, dict)
            else None,
            "registry_gate_pass": registry_gate_pass,
            "replay_candidate_model_sha256": replay_model_sha or None,
            "replay_candidate_report_sha256": replay_report_sha or None,
            "strategy_diagnose_status": strategy_status or None,
        },
    }


def audit_feature_contract(
    integrator: Dict[str, Any],
    runtime: Dict[str, Any],
    replay: Dict[str, Any],
) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    data = integrator.get("data", {}) if isinstance(integrator, dict) else {}
    metrics = runtime.get("metrics", {}) if isinstance(runtime, dict) else {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(metrics, dict):
        metrics = {}

    training_symbol = str(data.get("training_symbol") or "").strip().upper()
    bar_interval_ms = as_int(data.get("bar_interval_ms"))
    online_bar_source = str(data.get("online_bar_source") or "").strip()
    source_venue = str(data.get("source_venue") or "").strip().lower()
    source_category = str(data.get("source_category") or "").strip().lower()
    price_type = str(data.get("price_type") or "").strip().lower()
    volume_unit = str(data.get("volume_unit") or "").strip().lower()
    replay_source_symbol = (
        str(replay.get("source_symbol") or "").strip().upper()
        if isinstance(replay, dict)
        else ""
    )
    runtime_training_symbol = str(
        metrics.get("integrator_feature_training_symbol_latest") or ""
    ).strip().upper()
    runtime_bar_interval_ms = as_int(
        metrics.get("integrator_feature_bar_interval_ms_latest")
    )
    runtime_bootstrap_count = as_int(
        metrics.get("integrator_history_bootstrap_count")
    )
    runtime_stale_count = as_int(metrics.get("integrator_feature_stale_count"))
    legacy_contract_count = as_int(
        metrics.get("integrator_legacy_feature_contract_count")
    )

    if not training_symbol:
        fail_reasons.append("integrator data.training_symbol missing")
    if bar_interval_ms <= 0:
        fail_reasons.append("integrator data.bar_interval_ms missing")
    if online_bar_source != "closed_ohlcv":
        fail_reasons.append("integrator online_bar_source is not closed_ohlcv")
    if source_venue != "bybit":
        fail_reasons.append("integrator source_venue is not bybit")
    if source_category != "linear":
        fail_reasons.append("integrator source_category is not linear")
    if price_type != "trade_price":
        fail_reasons.append("integrator price_type is not trade_price")
    if volume_unit != "base_asset":
        fail_reasons.append("integrator volume_unit is not base_asset")
    if replay_source_symbol and training_symbol and replay_source_symbol != training_symbol:
        fail_reasons.append(
            f"replay source_symbol={replay_source_symbol} != "
            f"training_symbol={training_symbol}"
        )
    if runtime_training_symbol and runtime_training_symbol != training_symbol:
        fail_reasons.append(
            "runtime training symbol differs from integrator report: "
            f"{runtime_training_symbol} != {training_symbol}"
        )
    if runtime_bar_interval_ms > 0 and runtime_bar_interval_ms != bar_interval_ms:
        fail_reasons.append(
            "runtime bar interval differs from integrator report: "
            f"{runtime_bar_interval_ms} != {bar_interval_ms}"
        )

    runtime_canary_or_active = (
        as_int(metrics.get("integrator_mode_canary_count")) > 0
        or as_int(metrics.get("integrator_mode_active_count")) > 0
    )
    if runtime_canary_or_active:
        if not runtime_training_symbol or runtime_bar_interval_ms <= 0:
            fail_reasons.append("runtime integrator feature contract was not logged")
        if runtime_bootstrap_count <= 0:
            fail_reasons.append("runtime integrator history bootstrap was not proven")
    if runtime_stale_count > 0:
        warn_reasons.append(
            f"runtime detected stale integrator features {runtime_stale_count} times"
        )
    if legacy_contract_count > 0:
        warn_reasons.append(
            "runtime used legacy feature-contract migration; replace active report"
        )

    return {
        "status": json_status(fail_reasons, warn_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "observed": {
            "training_symbol": training_symbol or None,
            "bar_interval_ms": bar_interval_ms,
            "online_bar_source": online_bar_source or None,
            "source_venue": source_venue or None,
            "source_category": source_category or None,
            "price_type": price_type or None,
            "volume_unit": volume_unit or None,
            "replay_source_symbol": replay_source_symbol or None,
            "runtime_training_symbol": runtime_training_symbol or None,
            "runtime_bar_interval_ms": runtime_bar_interval_ms,
            "runtime_bootstrap_count": runtime_bootstrap_count,
            "runtime_feature_stale_count": runtime_stale_count,
            "runtime_legacy_contract_count": legacy_contract_count,
        },
    }


def audit_model_influence(runtime: Dict[str, Any], min_live_policy_applied: int) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    metrics = runtime.get("metrics", {}) if isinstance(runtime, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    shadow_count = as_int(metrics.get("integrator_mode_shadow_count"))
    canary_count = as_int(metrics.get("integrator_mode_canary_count"))
    active_count = as_int(metrics.get("integrator_mode_active_count"))
    policy_applied = as_int(metrics.get("integrator_policy_applied_count"))
    policy_proposed = as_int(metrics.get("integrator_policy_proposed_count"))
    policy_enqueued = as_int(metrics.get("integrator_policy_enqueued_count"))
    policy_filled = as_int(metrics.get("integrator_policy_filled_count"))
    unique_filled_orders = as_int(
        metrics.get("integrator_policy_unique_filled_order_count")
    )
    complete_episodes = as_int(
        metrics.get("integrator_policy_complete_episode_count")
    )
    canary_applied = as_int(metrics.get("integrator_policy_canary_count"))
    active_applied = as_int(metrics.get("integrator_policy_active_count"))
    scored_count = as_int(metrics.get("integrator_shadow_scored_runtime_count"))

    if not runtime:
        fail_reasons.append("runtime_assess_report missing; cannot verify model influence")
    elif canary_count <= 0 and active_count <= 0:
        fail_reasons.append(
            f"integrator never entered canary/active mode; shadow_count={shadow_count}, scored_count={scored_count}"
        )
    elif policy_applied < min_live_policy_applied:
        fail_reasons.append(
            f"integrator policy applied count {policy_applied} < required {min_live_policy_applied}"
        )
    elif complete_episodes < min_live_policy_applied:
        fail_reasons.append(
            "integrator complete candidate episode count "
            f"{complete_episodes} < required {min_live_policy_applied}"
        )

    return {
        "status": json_status(fail_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": [],
        "observed": {
            "integrator_mode_shadow_count": shadow_count,
            "integrator_mode_canary_count": canary_count,
            "integrator_mode_active_count": active_count,
            "integrator_policy_proposed_count": policy_proposed,
            "integrator_policy_enqueued_count": policy_enqueued,
            "integrator_policy_applied_count": policy_applied,
            "integrator_policy_filled_count": policy_filled,
            "integrator_policy_unique_filled_order_count": unique_filled_orders,
            "integrator_policy_complete_episode_count": complete_episodes,
            "integrator_policy_canary_count": canary_applied,
            "integrator_policy_active_count": active_applied,
            "integrator_shadow_scored_runtime_count": scored_count,
        },
    }


def audit_sample_sufficiency(
    runtime: Dict[str, Any],
    replay: Dict[str, Any],
    min_replay_total_fills: int,
) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    runtime_metrics = runtime.get("metrics", {}) if isinstance(runtime, dict) else {}
    if not isinstance(runtime_metrics, dict):
        runtime_metrics = {}
    replay_summary = replay.get("aggregate_summary", {}) if isinstance(replay, dict) else {}
    if not isinstance(replay_summary, dict):
        replay_summary = {}

    live_fills = as_int(
        runtime_metrics.get("integrator_policy_unique_filled_order_count")
    )
    live_episodes = as_int(
        runtime_metrics.get("integrator_policy_complete_episode_count")
    )
    replay_fills = as_int(replay_summary.get("total_fills"))
    positive_ratio = as_float(replay_summary.get("positive_filled_segment_ratio"))
    replay_net = as_float(replay_summary.get("mean_realized_net_per_fill_with_fills"))
    replay_status = str(replay.get("status", "")).lower() if isinstance(replay, dict) else ""

    if replay_fills < min_replay_total_fills:
        fail_reasons.append(
            f"replay total_fills={replay_fills} < required {min_replay_total_fills}"
        )
    if replay_net is None:
        warn_reasons.append("replay mean_realized_net_per_fill_with_fills missing")
    if positive_ratio is None:
        warn_reasons.append("replay positive_filled_segment_ratio missing")
    if replay_status == "fail":
        warn_reasons.append("replay status=fail; mechanism proof cannot rely on replay economics yet")
    if live_fills <= 0:
        warn_reasons.append(
            "candidate-attributed live fills are zero; live feedback loop is still unproven"
        )
    if live_episodes <= 0:
        warn_reasons.append(
            "complete candidate episodes are zero; partial fills cannot prove candidate economics"
        )

    return {
        "status": json_status(fail_reasons, warn_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "observed": {
            "live_fills": live_fills,
            "live_complete_episodes": live_episodes,
            "replay_total_fills": replay_fills,
            "replay_positive_filled_segment_ratio": positive_ratio,
            "replay_mean_realized_net_per_fill_with_fills": replay_net,
            "replay_status": replay_status or None,
        },
    }


def infer_cost_bps(replay: Dict[str, Any]) -> float:
    exit_capture = replay.get("exit_capture", {}) if isinstance(replay, dict) else {}
    if isinstance(exit_capture, dict):
        fee_bps = as_float(exit_capture.get("mean_fee_bps_per_fill"))
        if fee_bps is not None and fee_bps > 0.0:
            return fee_bps
    economics = replay.get("execution_economics", {}) if isinstance(replay, dict) else {}
    if isinstance(economics, dict):
        fee_bps = as_float(economics.get("mean_fee_bps_per_fill"))
        if fee_bps is not None and fee_bps > 0.0:
            return fee_bps
    return DEFAULT_COST_BPS


def audit_microstructure_route(
    route: Dict[str, Any], lifecycle: Dict[str, Any], binding: Dict[str, Any]
) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    policy = route.get("selection_policy", {}) if isinstance(route, dict) else {}
    if not isinstance(policy, dict):
        policy = {}
    candidate_id = str(lifecycle.get("candidate_id") or "")
    source = route.get("sources", {}).get("microstructure_demo", {})
    if not isinstance(source, dict):
        source = {}
    if not (
        route.get("schema_version") == "alpha_source_route_v1"
        and route.get("status") == "PASS"
        and route.get("selected_route") == "microstructure_demo"
        and route.get("demo_only") is True
        and route.get("live_promotion_eligible") is False
    ):
        fail_reasons.append("microstructure_demo is not the valid selected alpha route")
    if not (
        policy.get("method") == "fixed_predeclared_precedence"
        and policy.get("cross_source_return_comparison_permitted") is False
        and policy.get("nonselected_source_failure_blocks_selected_route") is False
    ):
        fail_reasons.append("alpha route selection is not leakage-isolated")
    if not (
        len(candidate_id) == 64
        and source.get("candidate_id") == candidate_id
        and source.get("readiness") == "READY"
    ):
        fail_reasons.append("selected route does not bind the lifecycle candidate")
    if not (
        binding.get("schema_version") == "microstructure_demo_binding_v1"
        and binding.get("status") == "PASS"
        and binding.get("selected_route") == "microstructure_demo"
        and binding.get("candidate_id") == candidate_id
        and binding.get("demo_entry_eligible") is True
        and binding.get("live_promotion_eligible") is False
    ):
        fail_reasons.append("demo sidecar/runtime binding did not pass for selected candidate")
    return {
        "status": json_status(fail_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": [],
        "observed": {
            "selected_route": route.get("selected_route"),
            "candidate_id": candidate_id or None,
            "binding_signal_status": binding.get("signal_status"),
            "binding_health_age_ms": binding.get("health_age_ms"),
            "binding_signal_age_ms": binding.get("signal_age_ms"),
            "live_promotion_eligible": False,
        },
    }


def read_verified_reference(
    reference: Any, *, label: str, fail_reasons: List[str]
) -> Dict[str, Any]:
    if not isinstance(reference, dict):
        fail_reasons.append(f"microstructure lifecycle evidence missing: {label}")
        return {}
    path = Path(str(reference.get("path") or ""))
    expected_hash = str(reference.get("sha256") or "")
    if (
        not path.is_file()
        or len(expected_hash) != 64
        or sha256_file(path) != expected_hash
    ):
        fail_reasons.append(f"microstructure lifecycle evidence identity mismatch: {label}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        fail_reasons.append(f"microstructure lifecycle evidence unreadable: {label}")
        return {}
    if not isinstance(payload, dict):
        fail_reasons.append(f"microstructure lifecycle evidence is not an object: {label}")
        return {}
    return payload


def audit_microstructure_lifecycle(lifecycle: Dict[str, Any]) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    candidate_id = str(lifecycle.get("candidate_id") or "")
    state = lifecycle.get("state", {})
    if not isinstance(state, dict):
        state = {}
    if not (
        lifecycle.get("schema_version") == "microstructure_alpha_lifecycle_v1"
        and lifecycle.get("status") == "PASS"
        and lifecycle.get("fully_verifiable") is True
        and lifecycle.get("phase") == "demo_ready"
        and lifecycle.get("demo_entry_eligible") is True
        and lifecycle.get("live_promotion_eligible") is False
        and lifecycle.get("promotion_eligible") is False
        and len(candidate_id) == 64
    ):
        fail_reasons.append("microstructure lifecycle is not fully verified demo_ready")
    if not (
        state.get("candidate_id") == candidate_id
        and state.get("phase") == "demo_ready"
        and state.get("demo_entry_eligible") is True
        and state.get("live_promotion_eligible") is False
    ):
        fail_reasons.append("microstructure lifecycle state/candidate isolation mismatch")

    evidence = state.get("evidence", {})
    artifacts = state.get("artifacts", {})
    if not isinstance(evidence, dict):
        evidence = {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    development = read_verified_reference(
        artifacts.get("development_report"),
        label="development_report",
        fail_reasons=fail_reasons,
    )
    selection = read_verified_reference(
        evidence.get("selection_passed"),
        label="selection_passed",
        fail_reasons=fail_reasons,
    )
    holdout = read_verified_reference(
        evidence.get("final_holdout_passed"),
        label="final_holdout_passed",
        fail_reasons=fail_reasons,
    )
    raw_replay = read_verified_reference(
        evidence.get("raw_replay_passed"),
        label="raw_replay_passed",
        fail_reasons=fail_reasons,
    )
    target = development.get("target_contract", {})
    validation = development.get("validation_contract", {})
    negative_control = development.get("negative_control", {})
    model_contract = development.get("model_contract", {})
    capture_merge = development.get("capture_merge_contract", {})
    capture_merge_audit = development.get("data", {}).get(
        "capture_merge_audit", {}
    )
    if not (
        development.get("schema_version") == "microstructure_alpha_development_v2"
        and development.get("status") == "PASS"
        and development.get("fully_verifiable") is True
        and development.get("research_domain") == "forward_development_only"
        and development.get("promotion_evidence") is False
        and development.get("promotion_eligible") is False
        and development.get("economic_screen", {}).get("development_passed") is True
        and isinstance(negative_control, dict)
        and negative_control.get("method")
        == "deterministic_oos_prediction_time_permutation"
        and negative_control.get("fully_verifiable") is True
        and negative_control.get("passed") is True
        and as_int(negative_control.get("trial_count")) >= 5
        and isinstance(capture_merge, dict)
        and capture_merge.get("method")
        == "drop_shared_adjacent_boundary_buckets_v1"
        and capture_merge.get("boundary_action")
        == "drop_entire_shared_one_second_bucket"
        and capture_merge.get("non_boundary_action") == "fail_closed"
        and isinstance(capture_merge_audit, dict)
        and capture_merge_audit.get("method") == capture_merge.get("method")
        and as_int(capture_merge_audit.get("input_segment_count")) > 0
        and as_int(capture_merge_audit.get("manifest_feature_row_count"))
        - as_int(capture_merge_audit.get("output_feature_row_count"))
        == 2 * as_int(capture_merge_audit.get("dropped_boundary_bucket_count"))
        and as_int(capture_merge_audit.get("shared_adjacent_boundary_bucket_count"))
        == as_int(capture_merge_audit.get("dropped_boundary_bucket_count"))
        and as_int(capture_merge_audit.get("conflicting_shared_boundary_bucket_count"))
        + as_int(capture_merge_audit.get("identical_shared_boundary_bucket_count"))
        == as_int(capture_merge_audit.get("dropped_boundary_bucket_count"))
        and len(
            str(capture_merge_audit.get("dropped_boundary_timestamps_sha256") or "")
        )
        == 64
        and isinstance(target, dict)
        and target.get("objective")
        == "joint_direction_and_exit_horizon_executable_net_return"
        and target.get("overlapping_episodes_forbidden") is True
        and isinstance(validation, dict)
        and validation.get("method") == "rolling_purged_nested_validation"
        and validation.get("score_threshold_floor_bps") is None
        and validation.get("negative_model_score_threshold_permitted") is True
        and validation.get("threshold_viability_contract")
        == "realized_base_and_stress_net_lcb_positive_in_nested_validation"
        and validation.get("calibration_scope")
        == "independent_per_action_then_economic_selection"
        and validation.get("frozen_action_aggregation")
        == "mode_of_nested_split_selected_actions"
        and as_float(validation.get("minimum_action_consensus_ratio")) is not None
        and float(as_float(validation.get("minimum_action_consensus_ratio")))
        >= 0.60
        and validation.get("oos_windows_non_overlapping") is True
        and isinstance(model_contract, dict)
        and model_contract.get("loss_function") == "MultiRMSE"
        and model_contract.get("training_target")
        == "fit_only_independent_active_action_stress_profitability"
        and model_contract.get("target_normalization")
        == "per_active_action_zero_mean_unit_variance_on_fit_domain_only"
        and model_contract.get("inference_score")
        == "clipped_fit_probability_weighted_action_conditional_base_net_return_bps"
        and model_contract.get("policy_selection")
        == "nested_per_action_threshold_then_mode_action_freeze"
        and model_contract.get("economic_acceptance_target")
        == "untransformed_executable_base_and_stress_net_return"
        and model_contract.get("validation_or_test_target_statistics_used_for_fit")
        is False
    ):
        fail_reasons.append("microstructure development economic/anti-leakage contract failed")
    for label, payload, domain in (
        ("selection", selection, "independent_forward_selection"),
        ("holdout", holdout, "untouched_final_holdout"),
    ):
        if not (
            payload.get("schema_version") == "microstructure_alpha_future_domain_v1"
            and payload.get("status") == "PASS"
            and payload.get("fully_verifiable") is True
            and payload.get("candidate_id") == candidate_id
            and payload.get("research_domain") == domain
            and payload.get("policy_frozen") is True
            and payload.get("threshold_tuning_permitted") is False
        ):
            fail_reasons.append(f"microstructure {label} frozen-policy economics failed")
    if not (
        raw_replay.get("schema_version") == "microstructure_alpha_raw_replay_v1"
        and raw_replay.get("status") == "PASS"
        and raw_replay.get("fully_verifiable") is True
        and raw_replay.get("candidate_id") == candidate_id
        and raw_replay.get("research_domain")
        == "untouched_final_holdout_replay"
        and raw_replay.get("raw_to_feature_parity") is True
        and raw_replay.get("fixed_model_prediction_economics_deterministic") is True
        and raw_replay.get("demo_entry_eligible") is True
        and raw_replay.get("live_promotion_eligible") is False
    ):
        fail_reasons.append("microstructure raw replay determinism/economics failed")
    replay_economic = raw_replay.get("economic_replay", {})
    if not isinstance(replay_economic, dict):
        replay_economic = {}
    return {
        "status": json_status(fail_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": [],
        "observed": {
            "candidate_id": candidate_id or None,
            "objective": target.get("objective") if isinstance(target, dict) else None,
            "selection_episode_count": as_int(selection.get("episode_count")),
            "holdout_episode_count": as_int(holdout.get("episode_count")),
            "raw_replay_episode_count": as_int(replay_economic.get("episode_count")),
            "raw_to_feature_parity": raw_replay.get("raw_to_feature_parity"),
            "fixed_model_prediction_economics_deterministic": raw_replay.get(
                "fixed_model_prediction_economics_deterministic"
            ),
            "development_prediction_permutation_control_passed": (
                negative_control.get("passed")
            ),
            "live_promotion_eligible": False,
        },
    }


def audit_microstructure_negative_control(
    lifecycle_check: Dict[str, Any], cost_bps: float
) -> Dict[str, Any]:
    synthetic_negative, _ = run_synthetic_controls(cost_bps)
    fail_reasons = list(synthetic_negative.get("fail_reasons", []))
    if lifecycle_check.get("status") != "pass":
        fail_reasons.append(
            "independent selection/holdout cannot reject an overfit development candidate"
        )
    return {
        "status": json_status(fail_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": [],
        "synthetic_negative_control": synthetic_negative,
        "source_negative_control": {
            "method": "frozen_policy_independent_future_selection_and_untouched_holdout",
            "status": lifecycle_check.get("status"),
        },
    }


def audit_microstructure_model_influence(
    runtime: Dict[str, Any], candidate_id: str, min_live_policy_applied: int
) -> Dict[str, Any]:
    metrics = runtime.get("metrics", {}) if isinstance(runtime, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    canary_count = as_int(metrics.get("integrator_mode_canary_count"))
    accepted_count = as_int(
        metrics.get("microstructure_demo_signal_accepted_count")
    )
    accepted = metrics.get("microstructure_demo_accepted_candidate_ids", [])
    proposed = metrics.get("integrator_policy_proposed_candidate_ids", [])
    filled = metrics.get("integrator_policy_filled_candidate_ids", [])
    if not isinstance(accepted, list):
        accepted = []
    if not isinstance(proposed, list):
        proposed = []
    if not isinstance(filled, list):
        filled = []
    accepted = [str(item) for item in accepted if str(item)]
    proposed = [str(item) for item in proposed if str(item)]
    filled = [str(item) for item in filled if str(item)]
    failures: List[str] = []
    if not runtime:
        failures.append(
            "runtime_assess_report missing; cannot verify microstructure policy consumption"
        )
    elif canary_count <= 0:
        failures.append("microstructure route never entered canary mode")
    if accepted_count <= 0 or not accepted:
        failures.append(
            "runtime never accepted an integrity-checked microstructure candidate signal"
        )
    if accepted and any(item != candidate_id for item in accepted):
        failures.append("runtime accepted a candidate outside the selected route")
    if proposed and any(item != candidate_id for item in proposed):
        failures.append("runtime proposed a candidate outside the selected microstructure route")
    if filled and any(item != candidate_id for item in filled):
        failures.append("runtime fills were attributed to a different candidate")
    return {
        "status": json_status(failures),
        "fail_reasons": failures,
        "warn_reasons": [],
        "observed": {
            "expected_candidate_id": candidate_id or None,
            "integrator_mode_canary_count": canary_count,
            "accepted_signal_count": accepted_count,
            "accepted_candidate_ids": accepted,
            "proposed_candidate_ids": proposed,
            "filled_candidate_ids": filled,
            "required_live_policy_applied_for_economic_convergence": (
                min_live_policy_applied
            ),
        },
    }


def audit_microstructure_sample_sufficiency(
    runtime: Dict[str, Any], lifecycle_check: Dict[str, Any], min_replay_total_fills: int
) -> Dict[str, Any]:
    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    observed = lifecycle_check.get("observed", {})
    if not isinstance(observed, dict):
        observed = {}
    replay_episodes = as_int(observed.get("raw_replay_episode_count"))
    metrics = runtime.get("metrics", {}) if isinstance(runtime, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    live_fills = as_int(metrics.get("integrator_policy_unique_filled_order_count"))
    live_episodes = as_int(metrics.get("integrator_policy_complete_episode_count"))
    if replay_episodes < min_replay_total_fills:
        fail_reasons.append(
            f"raw replay episodes={replay_episodes} < required {min_replay_total_fills}"
        )
    return {
        "status": json_status(fail_reasons, warn_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "observed": {
            "raw_replay_episode_count": replay_episodes,
            "live_fills": live_fills,
            "live_complete_episodes": live_episodes,
        },
    }


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    integrator = read_json_optional(args.integrator_report)
    registry = read_json_optional(args.registry_report)
    runtime = read_json_optional(args.runtime_assess_report)
    replay = read_json_optional(args.replay_validation_report)
    if not replay:
        replay = read_json_optional(args.replay_optimization_report)
    strategy = read_json_optional(args.strategy_diagnose_report)
    alpha_probe = read_json_optional(args.alpha_mechanism_probe_report)
    run_manifest = read_json_optional(args.run_manifest)
    route = read_json_optional(getattr(args, "alpha_source_route_report", ""))
    lifecycle = read_json_optional(
        getattr(args, "microstructure_alpha_lifecycle_report", "")
    )
    binding = read_json_optional(
        getattr(args, "microstructure_demo_binding_report", "")
    )

    cost_bps = float(args.control_cost_bps) if args.control_cost_bps is not None else infer_cost_bps(replay)

    selected_route = str(route.get("selected_route") or "")
    if selected_route == "microstructure_demo":
        lifecycle_check = audit_microstructure_lifecycle(lifecycle)
        candidate_id = str(lifecycle.get("candidate_id") or "")
        checks = {
            "alpha_source_route": audit_microstructure_route(route, lifecycle, binding),
            "negative_control": audit_microstructure_negative_control(
                lifecycle_check, cost_bps
            ),
            "positive_control": audit_positive_control(cost_bps),
            "target_consistency": lifecycle_check,
            "feature_contract": {
                "status": lifecycle_check.get("status"),
                "fail_reasons": list(lifecycle_check.get("fail_reasons", [])),
                "warn_reasons": [],
                "observed": lifecycle_check.get("observed", {}),
            },
            "model_influence": audit_microstructure_model_influence(
                runtime=runtime,
                candidate_id=candidate_id,
                min_live_policy_applied=int(args.min_live_policy_applied),
            ),
            "sample_sufficiency": audit_microstructure_sample_sufficiency(
                runtime=runtime,
                lifecycle_check=lifecycle_check,
                min_replay_total_fills=int(args.min_replay_total_fills),
            ),
            "alpha_mechanism_probe": {
                "status": "not_applicable",
                "fail_reasons": [],
                "warn_reasons": [],
                "evidence_role": "nonselected_legacy_route_diagnostic",
            },
        }
    elif selected_route == "legacy_integrator" or not route:
        selected_route = "legacy_integrator"
        checks = {
            "negative_control": audit_negative_control(integrator, cost_bps),
            "positive_control": audit_positive_control(cost_bps),
            "alpha_mechanism_probe": audit_alpha_mechanism_probe(alpha_probe),
            "target_consistency": audit_target_consistency(
                integrator=integrator,
                registry=registry,
                replay=replay,
                strategy=strategy,
            ),
            "feature_contract": audit_feature_contract(
                integrator=integrator,
                runtime=runtime,
                replay=replay,
            ),
            "model_influence": audit_model_influence(
                runtime=runtime,
                min_live_policy_applied=int(args.min_live_policy_applied),
            ),
            "sample_sufficiency": audit_sample_sufficiency(
                runtime=runtime,
                replay=replay,
                min_replay_total_fills=int(args.min_replay_total_fills),
            ),
        }
    else:
        selected_route = "unresolved"
        checks = {
            "alpha_source_route": {
                "status": "fail",
                "fail_reasons": [
                    "alpha source route evidence exists but selects no valid route"
                ],
                "warn_reasons": [],
                "observed": {
                    "route_status": route.get("status"),
                    "selected_route": route.get("selected_route"),
                    "reason": route.get("reason"),
                },
            },
            "positive_control": audit_positive_control(cost_bps),
        }

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []
    for name, check in checks.items():
        for item in check.get("fail_reasons", []):
            fail_reasons.append(f"{name}: {item}")
        for item in check.get("warn_reasons", []):
            warn_reasons.append(f"{name}: {item}")

    status = json_status(fail_reasons, warn_reasons)
    if status == "fail":
        readiness = "FAIL"
        conclusion = "MECHANISM_NOT_PROVEN"
    elif status == "pass_with_actions":
        readiness = "PASS_WITH_ACTIONS"
        conclusion = "MECHANISM_PARTIALLY_PROVEN"
    else:
        readiness = "PASS"
        conclusion = "MECHANISM_PROVEN"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now_utc_iso(),
        "status": status,
        "readiness_status": readiness,
        "conclusion": conclusion,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "control_cost_bps": cost_bps,
        "selected_alpha_route": selected_route,
        "checks": checks,
        "run_context": {
            "run_id": run_manifest.get("run_id") if isinstance(run_manifest, dict) else None,
            "action": run_manifest.get("action") if isinstance(run_manifest, dict) else None,
            "stage": run_manifest.get("stage") if isinstance(run_manifest, dict) else None,
            "git": run_manifest.get("git") if isinstance(run_manifest, dict) else None,
        },
        "next_actions": [
            "Replace AUC-primary promotion with net economic objective governance.",
            "Run a real pipeline positive-control experiment before more strategy tuning.",
            "Move integrator to canary only after replay/strategy evidence passes, then require live policy_applied samples.",
            "Do not treat smoke/runtime protection PASS as proof of trading convergence.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit closed-loop mechanism proof")
    parser.add_argument("--output", required=True, help="Output JSON report path")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write the evidence verdict without using it as the process exit status",
    )
    parser.add_argument("--run_manifest", default="", help="run_manifest.json path")
    parser.add_argument("--integrator_report", default="", help="integrator_report.json path")
    parser.add_argument("--registry_report", default="", help="model_registry_entry.json path")
    parser.add_argument("--runtime_assess_report", default="", help="runtime_assess.json path")
    parser.add_argument("--replay_validation_report", default="", help="replay_validation_report.json path")
    parser.add_argument("--replay_optimization_report", default="", help="replay_optimization_report.json path")
    parser.add_argument("--strategy_diagnose_report", default="", help="strategy_diagnose_report.json path")
    parser.add_argument("--alpha_mechanism_probe_report", default="", help="alpha_mechanism_probe_report.json path")
    parser.add_argument("--alpha_source_route_report", default="", help="alpha_source_route_report.json path")
    parser.add_argument("--microstructure_alpha_lifecycle_report", default="", help="microstructure_alpha_lifecycle_report.json path")
    parser.add_argument("--microstructure_demo_binding_report", default="", help="microstructure_demo_binding_report.json path")
    parser.add_argument(
        "--control_cost_bps",
        type=float,
        default=None,
        help="Optional cost bps for synthetic controls; defaults to replay fee estimate",
    )
    parser.add_argument(
        "--min_live_policy_applied",
        type=int,
        default=DEFAULT_MIN_LIVE_POLICY_APPLIED,
        help="Minimum live integrator policy_applied samples required",
    )
    parser.add_argument(
        "--min_replay_total_fills",
        type=int,
        default=DEFAULT_MIN_REPLAY_TOTAL_FILLS,
        help="Minimum replay fills for mechanism sample sufficiency",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": report["status"], "conclusion": report["conclusion"]}, ensure_ascii=False))
    if args.report_only:
        return 0
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
