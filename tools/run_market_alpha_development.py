#!/usr/bin/env python3
"""One-command development-only market-alpha backfill and economic screen."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any, Dict, List, Sequence


SCHEMA_VERSION = "market_alpha_development_verification_v1"
FIXED_VARIANTS = (
    "continuous_return_rmse",
    "continuous_return_huber",
    "continuous_return_huber_side_calibrated",
    "continuous_return_path_huber",
    "ternary_action_rmse",
    "path_utility_huber",
)


def ensure_development_input(path: pathlib.Path) -> None:
    lowered = str(path).lower()
    if "development" not in lowered or any(
        token in lowered for token in ("selection", "holdout", "final_test")
    ):
        raise ValueError(
            "input must be explicitly named as development and must not reference "
            "selection/holdout/final_test"
        )


def last_anchor_date(path: pathlib.Path) -> dt.date:
    last = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if "timestamp" not in (reader.fieldnames or []):
            raise ValueError("OHLCV CSV is missing timestamp")
        for row in reader:
            last = int(row["timestamp"])
    if last is None:
        raise ValueError("OHLCV CSV is empty")
    return dt.datetime.fromtimestamp(last / 1000.0, dt.timezone.utc).date()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(command: Sequence[str]) -> None:
    print("[RUN] " + " ".join(command), flush=True)
    subprocess.run(list(command), check=True)


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON report is not an object: {path}")
    return payload


def summarize_probe(payload: Dict[str, Any]) -> Dict[str, Any]:
    variants = payload.get("variants")
    if payload.get("status") != "diagnostic_complete" or not isinstance(variants, list):
        raise ValueError("economic probe did not complete")
    summaries = []
    for item in variants:
        metrics = item.get("metrics_development_oos", {})
        summaries.append(
            {
                "variant": item.get("variant"),
                "passes_development_economic_screen": bool(
                    metrics.get("passes_development_economic_screen", False)
                ),
                "mean_model_net_edge_bps": metrics.get("mean_model_net_edge_bps"),
                "model_net_edge_lcb_bps": metrics.get("model_net_edge_lcb_bps"),
                "model_net_total_trades": metrics.get("model_net_total_trades"),
                "positive_model_net_edge_ratio_by_split": metrics.get(
                    "positive_model_net_edge_ratio_by_split"
                ),
            }
        )
    return {
        "feature_set": payload.get("data", {}).get("feature_set"),
        "report_status": payload.get("status"),
        "promotion_evidence": payload.get("promotion_evidence"),
        "multiple_hypothesis_count": payload.get("multiple_hypothesis_count"),
        "variants": summaries,
    }


def build_verification(
    *,
    anchor_path: pathlib.Path,
    market_report: Dict[str, Any],
    trade_report: Dict[str, Any],
    probe_reports: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    probe_summaries = [summarize_probe(payload) for payload in probe_reports]
    fully_verifiable = bool(
        market_report.get("status") == "PASS"
        and trade_report.get("status") == "PASS"
        and probe_summaries
        and all(item["report_status"] == "diagnostic_complete" for item in probe_summaries)
        and all(item["promotion_evidence"] is False for item in probe_summaries)
    )
    market_variants = [
        variant
        for report in probe_summaries
        if "market_alpha" in str(report["feature_set"])
        for variant in report["variants"]
    ]
    development_passed = any(
        item["passes_development_economic_screen"] for item in market_variants
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if fully_verifiable else "FAIL",
        "fully_verifiable": fully_verifiable,
        "research_domain": "development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "anchor": {"path": str(anchor_path), "sha256": sha256_file(anchor_path)},
        "data_gates": {
            "cross_market_cross_asset_history": market_report.get("status"),
            "bybit_trade_archive_sample": trade_report.get("status"),
        },
        "economic_screen": {
            "development_passed": development_passed,
            "feature_set_count": len(probe_summaries),
            "variant_result_count": sum(len(item["variants"]) for item in probe_summaries),
            "reports": probe_summaries,
        },
        "next_gate": (
            "independent_selection_required"
            if development_passed
            else "remain_in_development_and_reject_candidate"
        ),
        "untouched_final_holdout_required": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ohlcv-csv", required=True)
    parser.add_argument("--miner-report", required=True)
    parser.add_argument("--derivatives-csv", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--predict-horizon-bars", type=int, default=12)
    parser.add_argument("--bybit-trade-sample-days", type=int, default=1)
    parser.add_argument("--variants", default=",".join(FIXED_VARIANTS))
    parser.add_argument("--iterations", type=int, default=150)
    parser.add_argument("--round-trip-cost-bps", type=float, default=13.0)
    parser.add_argument("--path-take-profit-bps", type=float, default=32.0)
    parser.add_argument("--path-stop-loss-bps", type=float, default=20.0)
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    anchor = pathlib.Path(args.ohlcv_csv).resolve()
    miner = pathlib.Path(args.miner_report).resolve()
    derivatives = pathlib.Path(args.derivatives_csv).resolve() if args.derivatives_csv else None
    ensure_development_input(anchor)
    if args.research_domain != "development":
        raise ValueError("runner is development-only")
    if args.predict_horizon_bars <= 0 or args.bybit_trade_sample_days <= 0:
        raise ValueError("horizon and Bybit trade sample days must be positive")
    output_dir = pathlib.Path(args.output_dir).resolve()
    cache_dir = pathlib.Path(args.cache_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tools_dir = pathlib.Path(__file__).resolve().parent
    market_csv = output_dir / "research_development_market_alpha_5m.csv"
    market_report_path = output_dir / "market_alpha_history_report.json"
    trade_csv = output_dir / "research_development_bybit_trade_flow_sample_5m.csv"
    trade_report_path = output_dir / "bybit_trade_history_sample_report.json"

    run_command(
        [
            sys.executable,
            str(tools_dir / "fetch_market_alpha_history.py"),
            "--ohlcv-csv",
            str(anchor),
            "--output",
            str(market_csv),
            "--report",
            str(market_report_path),
            "--cache-dir",
            str(cache_dir / "binance"),
            "--research-domain",
            "development",
        ]
    )
    end_day = last_anchor_date(anchor)
    start_day = end_day - dt.timedelta(days=args.bybit_trade_sample_days - 1)
    run_command(
        [
            sys.executable,
            str(tools_dir / "fetch_bybit_trade_history.py"),
            "--ohlcv-csv",
            str(anchor),
            "--output",
            str(trade_csv),
            "--report",
            str(trade_report_path),
            "--cache-dir",
            str(cache_dir / "bybit"),
            "--start-date",
            start_day.isoformat(),
            "--end-date",
            end_day.isoformat(),
            "--research-domain",
            "development",
        ]
    )

    feature_sets = ["expanded_ohlcv_v1", "expanded_market_alpha_v1"]
    if derivatives is not None:
        ensure_development_input(derivatives)
        feature_sets.append("expanded_market_alpha_derivatives_v1")
    probe_payloads = []
    for feature_set in feature_sets:
        output = output_dir / f"economic_h{args.predict_horizon_bars}_{feature_set}.json"
        command: List[str] = [
            sys.executable,
            str(tools_dir / "economic_target_probe.py"),
            "--csv",
            str(anchor),
            "--miner_report",
            str(miner),
            "--output",
            str(output),
            "--predict_horizon_bars",
            str(args.predict_horizon_bars),
            "--feature_set",
            feature_set,
            "--market_alpha_csv",
            str(market_csv),
            "--calibration_mode",
            "nested_validation_quantile",
            "--variants",
            args.variants,
            "--iterations",
            str(args.iterations),
            "--label_round_trip_cost_bps",
            str(args.round_trip_cost_bps),
            "--path_take_profit_bps",
            str(args.path_take_profit_bps),
            "--path_stop_loss_bps",
            str(args.path_stop_loss_bps),
        ]
        if derivatives is not None:
            command.extend(["--derivatives_csv", str(derivatives)])
        run_command(command)
        probe_payloads.append(load_json(output))

    verification = build_verification(
        anchor_path=anchor,
        market_report=load_json(market_report_path),
        trade_report=load_json(trade_report_path),
        probe_reports=probe_payloads,
    )
    verification_path = output_dir / f"market_alpha_verification_h{args.predict_horizon_bars}.json"
    verification_path.write_text(
        json.dumps(verification, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(verification, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if verification["fully_verifiable"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
