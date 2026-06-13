#!/usr/bin/env python3
"""Compare recent closed-loop reports by convergence layer.

The goal is not to replace detailed report reading. It gives a stable first
question for retrospectives: which layer keeps blocking convergence?
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
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


def as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def first_existing(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def find_report_paths(root: Path, pattern: str, limit: int) -> list[Path]:
    paths = sorted(
        [path for path in root.glob(pattern) if path.is_file()],
        key=lambda path: path.stat().st_mtime,
    )
    if limit > 0:
        paths = paths[-limit:]
    return paths


def extract_confirmed_trend(strategy: dict[str, Any]) -> dict[str, Any]:
    aggregate = strategy.get("aggregate", {}) if isinstance(strategy, dict) else {}
    if not isinstance(aggregate, dict):
        aggregate = {}
    confirmed = aggregate.get("confirmed_trend", {})
    if not isinstance(confirmed, dict):
        confirmed = {}
    return {
        "sample_count": as_int(confirmed.get("sample_count")),
        "mean_net_forward_bps": as_float(confirmed.get("mean_net_forward_bps")),
        "positive_net_ratio": as_float(confirmed.get("positive_net_ratio")),
        "mean_path_mfe_bps": as_float(confirmed.get("mean_path_mfe_bps")),
    }


def infer_blocking_layer(
    *,
    explicit_layer: Any,
    replay_status: Any,
    replay_readiness_status: Any,
    replay_skip_reason: Any,
    strategy_diagnose_status: Any,
    trading_blockers: list[Any],
    live_fills: int,
) -> str:
    explicit = str(explicit_layer or "").strip()
    if explicit:
        return explicit
    skip_reason = str(replay_skip_reason or "").strip().lower()
    replay_status_text = str(replay_status or "").strip().lower()
    replay_readiness = str(replay_readiness_status or "").strip().upper()
    strategy_status = str(strategy_diagnose_status or "").strip().upper()
    blockers = {str(item).strip().upper() for item in trading_blockers}
    if skip_reason == "command_failed":
        return "replay_execution"
    if replay_status_text == "fail" or replay_readiness == "FAIL":
        return "replay_execution"
    if strategy_status in {"FAIL", "ACTION_REQUIRED"}:
        return "strategy_raw_edge"
    if any(item.startswith("NOT_CONVERGED_EXIT_CAPTURE") for item in blockers):
        return "replay_execution"
    if "NOT_CONVERGED_NO_LIVE_FILLS" in blockers and live_fills <= 0:
        return "live_canary"
    if blockers:
        return "trading_convergence"
    return "none"


def infer_next_action(blocking_layer: str, replay_skip_reason: Any) -> str:
    skip_reason = str(replay_skip_reason or "").strip().lower()
    if skip_reason == "command_failed":
        return "inspect_replay_failure_diagnostics_and_fix_replay_command"
    if blocking_layer == "replay_execution":
        return "fix_replay_validation_or_execution_economics_before_waiting_for_live"
    if blocking_layer == "strategy_raw_edge":
        return "redesign_alpha_label_or_exit_objective_before_widening_live_gates"
    if blocking_layer == "live_canary":
        return "do_not_wait_blindly; rerun_live_only_after_replay_passes_or_market_context_has_target_samples"
    if blocking_layer == "model_walkforward":
        return "fix_training_or_walkforward_before_live_replay_tuning"
    if blocking_layer == "data_feature_quality":
        return "fix_data_pipeline_data_quality_or_live_replay_feature_parity"
    return "review_closed_loop_report"


def infer_primary_reason(
    *,
    blocking_layer: str,
    replay_status: Any,
    replay_readiness_status: Any,
    replay_skip_reason: Any,
    strategy_diagnose_status: Any,
    trading_blockers: list[Any],
    live_fills: int,
) -> str:
    skip_reason = str(replay_skip_reason or "").strip().lower()
    replay_status_text = str(replay_status or "").strip().lower()
    replay_readiness = str(replay_readiness_status or "").strip().upper()
    strategy_status = str(strategy_diagnose_status or "").strip().upper()
    blockers = [str(item).strip().upper() for item in trading_blockers]
    if skip_reason == "command_failed":
        return "replay_validation command_failed"
    if replay_status_text == "fail" or replay_readiness == "FAIL":
        return "replay_validation failed_or_not_ready"
    if strategy_status in {"FAIL", "ACTION_REQUIRED"}:
        return f"strategy_diagnose {strategy_status.lower()}"
    exit_blockers = [
        item for item in blockers if item.startswith("NOT_CONVERGED_EXIT_CAPTURE")
    ]
    if exit_blockers:
        return ",".join(exit_blockers)
    if blocking_layer == "live_canary" and live_fills <= 0:
        return "no live fills after earlier gates"
    if blockers:
        return ",".join(blockers[:3])
    return "no blocking reason detected"


def summarize_report(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    sections = payload.get("sections", {})
    if not isinstance(sections, dict):
        sections = {}
    replay = sections.get("replay_validation", {})
    runtime = sections.get("runtime", {})
    strategy = sections.get("strategy_diagnose", {})
    trading = sections.get("trading_convergence", {})
    next_action = payload.get("next_action_plan", {})
    layers = payload.get("convergence_layers", {})
    if not isinstance(replay, dict):
        replay = {}
    if not isinstance(runtime, dict):
        runtime = {}
    if not isinstance(strategy, dict):
        strategy = {}
    if not isinstance(trading, dict):
        trading = {}
    if not isinstance(next_action, dict):
        next_action = {}
    if not isinstance(layers, dict):
        layers = {}

    runtime_metrics = runtime.get("metrics", {})
    if not isinstance(runtime_metrics, dict):
        runtime_metrics = {}
    replay_summary = first_existing(
        replay.get("aggregate_summary"),
        replay.get("summary"),
        {},
    )
    if not isinstance(replay_summary, dict):
        replay_summary = {}
    replay_command_failure = layers.get("replay_command_failure", {})
    if not isinstance(replay_command_failure, dict):
        replay_command_failure = {}
    replay_skip_reason = first_existing(
        replay.get("skip_reason"),
        (replay.get("selection") or {}).get("stop_reason")
        if isinstance(replay.get("selection"), dict)
        else None,
    )
    live_fills = max(
        as_int(runtime_metrics.get("funnel_fills_runtime_count")),
        as_int(runtime_metrics.get("trend_candidate_probe_fill_count")),
    )
    trading_blockers = trading.get("blockers", [])
    if not isinstance(trading_blockers, list):
        trading_blockers = []
    first_blocking_layer = infer_blocking_layer(
        explicit_layer=first_existing(
            next_action.get("first_blocking_layer"),
            layers.get("first_blocking_layer"),
        ),
        replay_status=replay.get("status"),
        replay_readiness_status=payload.get("replay_readiness_status"),
        replay_skip_reason=replay_skip_reason,
        strategy_diagnose_status=payload.get("strategy_diagnose_status"),
        trading_blockers=trading_blockers,
        live_fills=live_fills,
    )
    primary_next_action = first_existing(
        next_action.get("primary_next_action"),
        layers.get("primary_next_action"),
        infer_next_action(first_blocking_layer, replay_skip_reason),
    )
    primary_reason = first_existing(
        next_action.get("primary_reason"),
        layers.get("primary_reason"),
        infer_primary_reason(
            blocking_layer=first_blocking_layer,
            replay_status=replay.get("status"),
            replay_readiness_status=payload.get("replay_readiness_status"),
            replay_skip_reason=replay_skip_reason,
            strategy_diagnose_status=payload.get("strategy_diagnose_status"),
            trading_blockers=trading_blockers,
            live_fills=live_fills,
        ),
    )
    replay_command_failed = bool(replay_command_failure.get("present")) or (
        str(replay_skip_reason or "").strip().lower() == "command_failed"
    )

    return {
        "path": str(path),
        "run_id": payload.get("run_id"),
        "generated_at_utc": payload.get("generated_at_utc"),
        "overall_status": payload.get("overall_status"),
        "first_blocking_layer": first_blocking_layer,
        "primary_next_action": primary_next_action,
        "primary_reason": primary_reason,
        "runtime_validation_class": payload.get("runtime_validation_class"),
        "runtime_market_context_status": runtime.get("market_context_status"),
        "live_fills": live_fills,
        "replay_readiness_status": payload.get("replay_readiness_status"),
        "replay_status": replay.get("status"),
        "replay_skip_reason": replay_skip_reason,
        "replay_total_fills": as_int(replay_summary.get("total_fills")),
        "replay_command_failed": replay_command_failed,
        "replay_command_failure_has_diagnostics": bool(
            replay_command_failure.get("has_failure_diagnostics")
        ),
        "strategy_diagnose_status": payload.get("strategy_diagnose_status"),
        "confirmed_trend": extract_confirmed_trend(strategy),
        "trading_convergence_status": payload.get("trading_convergence_status"),
        "trading_blockers": trading_blockers,
    }


def build_comparison(paths: list[Path]) -> dict[str, Any]:
    reports = [summarize_report(path) for path in paths]
    layer_counts = Counter(
        str(item.get("first_blocking_layer") or "unknown") for item in reports
    )
    action_counts = Counter(
        str(item.get("primary_next_action") or "unknown") for item in reports
    )
    blocker_counts: Counter[str] = Counter()
    for item in reports:
        for blocker in item.get("trading_blockers", []):
            blocker_counts[str(blocker)] += 1
    latest = reports[-1] if reports else {}
    return {
        "schema_version": "closed_loop_report_comparison_v1",
        "report_count": len(reports),
        "latest": latest,
        "counts": {
            "overall_status": dict(Counter(str(item.get("overall_status")) for item in reports)),
            "first_blocking_layer": dict(layer_counts),
            "primary_next_action": dict(action_counts),
            "trading_blockers": dict(blocker_counts),
            "replay_command_failed": sum(1 for item in reports if item.get("replay_command_failed")),
            "replay_command_failure_without_diagnostics": sum(
                1
                for item in reports
                if item.get("replay_command_failed")
                and not item.get("replay_command_failure_has_diagnostics")
            ),
        },
        "rows": reports,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare recent closed-loop reports by convergence layer."
    )
    parser.add_argument(
        "reports",
        nargs="*",
        help="Explicit closed_loop_report.json paths. If omitted, --reports-root/--glob is used.",
    )
    parser.add_argument(
        "--reports-root",
        default="data/reports/closed_loop",
        help="Root used when explicit report paths are omitted.",
    )
    parser.add_argument(
        "--glob",
        default="*/closed_loop_report.json",
        help="Glob under --reports-root when explicit report paths are omitted.",
    )
    parser.add_argument("--limit", type=int, default=10, help="Latest report count.")
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.reports:
        paths = [Path(item) for item in args.reports]
        if args.limit > 0:
            paths = paths[-args.limit :]
    else:
        paths = find_report_paths(Path(args.reports_root), args.glob, args.limit)
    comparison = build_comparison(paths)
    text = json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
