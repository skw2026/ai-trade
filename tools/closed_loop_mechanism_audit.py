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


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json_optional(path_text: str) -> Dict[str, Any]:
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


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
        warn_reasons.append("alpha mechanism probe controls pass, but real market alpha family failed holdout")
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

    if primary_objective != "model_net_edge_bps_after_cost":
        fail_reasons.append("integrator metrics primary objective is not net economic edge")
    if governance_primary_objective != "model_net_edge_bps_after_cost":
        fail_reasons.append("integrator governance primary objective is not net economic edge")
    if mean_model_net_edge_bps is None:
        fail_reasons.append("integrator mean_model_net_edge_bps missing")
    if positive_model_net_edge_ratio is None:
        fail_reasons.append("integrator positive_model_net_edge_ratio missing")
    if min_mean_model_net_edge_bps is None:
        fail_reasons.append("integrator min_mean_model_net_edge_bps threshold missing")
    if min_positive_model_net_edge_ratio is None:
        fail_reasons.append("integrator min_positive_model_net_edge_ratio threshold missing")

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

    if registry:
        registry_gate = registry.get("gate", {}) if isinstance(registry, dict) else {}
        registry_activation_gate = registry.get("activation_gate", {})
        registry_gate_pass = bool(registry.get("gate_pass", registry_gate.get("pass")))
        if not registry_gate_pass:
            fail_reasons.append("registry gate did not pass")
        if not registry_activation_gate and not activation_gate:
            fail_reasons.append("registry did not record replay activation gate evidence")
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
            "registry_gate_pass": bool(registry.get("gate_pass")) if registry else None,
            "strategy_diagnose_status": strategy_status or None,
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

    return {
        "status": json_status(fail_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": [],
        "observed": {
            "integrator_mode_shadow_count": shadow_count,
            "integrator_mode_canary_count": canary_count,
            "integrator_mode_active_count": active_count,
            "integrator_policy_applied_count": policy_applied,
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

    live_fills = max(
        as_int(runtime_metrics.get("funnel_fills_runtime_count")),
        as_int(runtime_metrics.get("trend_candidate_probe_fill_count")),
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
        warn_reasons.append("live fills are zero; live feedback loop is still unproven")

    return {
        "status": json_status(fail_reasons, warn_reasons),
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "observed": {
            "live_fills": live_fills,
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

    cost_bps = float(args.control_cost_bps) if args.control_cost_bps is not None else infer_cost_bps(replay)

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
    parser.add_argument("--run_manifest", default="", help="run_manifest.json path")
    parser.add_argument("--integrator_report", default="", help="integrator_report.json path")
    parser.add_argument("--registry_report", default="", help="model_registry_entry.json path")
    parser.add_argument("--runtime_assess_report", default="", help="runtime_assess.json path")
    parser.add_argument("--replay_validation_report", default="", help="replay_validation_report.json path")
    parser.add_argument("--replay_optimization_report", default="", help="replay_optimization_report.json path")
    parser.add_argument("--strategy_diagnose_report", default="", help="strategy_diagnose_report.json path")
    parser.add_argument("--alpha_mechanism_probe_report", default="", help="alpha_mechanism_probe_report.json path")
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
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
