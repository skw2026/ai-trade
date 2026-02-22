#!/usr/bin/env python3
"""
Data acceleration pipeline orchestrator.

Steps:
1) archive download
2) incremental update
3) gap fill
4) feature store build
5) walk-forward backtest
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class StepResult:
    name: str
    enabled: bool
    command: List[str]
    return_code: int | None = None
    status: str = "skipped"
    started_at_utc: str = ""
    finished_at_utc: str = ""
    error: str = ""


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if text.lower() in {"true", "yes", "on"}:
        return True
    if text.lower() in {"false", "no", "off"}:
        return False
    if text.lower() in {"null", "none"}:
        return None
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def load_yaml(path: pathlib.Path) -> Dict[str, Any]:
    """
    Minimal YAML parser for this project config format:
    - mapping only
    - scalar values
    - 2-space indentation
    """
    root: Dict[str, Any] = {}
    stack: List[tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.lstrip()
        if stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(stripped)
        line = stripped
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: Dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = parse_scalar(value)
    return root


def deep_get(root: Dict[str, Any], keys: List[str], default: Any) -> Any:
    current: Any = root
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value_l = value.strip().lower()
        if value_l in {"1", "true", "yes", "on"}:
            return True
        if value_l in {"0", "false", "no", "off"}:
            return False
    return default


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_command(step: StepResult, dry_run: bool) -> StepResult:
    step.started_at_utc = now_utc_iso()
    if not step.enabled:
        step.status = "skipped"
        step.finished_at_utc = now_utc_iso()
        return step

    print(f"[PIPELINE] {step.name}: {' '.join(shlex.quote(x) for x in step.command)}")
    if dry_run:
        step.status = "planned"
        step.return_code = 0
        step.finished_at_utc = now_utc_iso()
        return step

    completed = subprocess.run(step.command, capture_output=True, text=True)
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    step.return_code = completed.returncode
    if completed.returncode == 0:
        step.status = "ok"
    else:
        step.status = "fail"
        step.error = f"return_code={completed.returncode}"
    step.finished_at_utc = now_utc_iso()
    return step


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run data acceleration pipeline")
    parser.add_argument("--config", default="config/data_pipeline.yaml")
    parser.add_argument("--run-dir", default="")
    parser.add_argument("--ohlcv-out", default="")
    parser.add_argument("--feature-out", default="")
    parser.add_argument("--backtest-report", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = pathlib.Path(args.config)
    config = load_yaml(config_path)

    run_dir = pathlib.Path(args.run_dir) if args.run_dir else pathlib.Path(
        f"data/reports/closed_loop/data_pipeline/{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)

    ohlcv_path = pathlib.Path(
        args.ohlcv_out
        or deep_get(config, ["paths", "ohlcv_csv"], "data/research/ohlcv_5m.csv")
    )
    feature_path = pathlib.Path(
        args.feature_out
        or deep_get(config, ["paths", "feature_csv"], str(run_dir / "feature_store_5m.csv"))
    )
    backtest_path = pathlib.Path(
        args.backtest_report
        or deep_get(config, ["paths", "backtest_report"], str(run_dir / "walkforward_report.json"))
    )

    symbol = str(deep_get(config, ["common", "symbol"], "BTCUSDT")).upper()
    interval_min = str(deep_get(config, ["common", "interval_minutes"], 5))
    category = str(deep_get(config, ["common", "category"], "linear"))

    py = sys.executable
    steps: List[StepResult] = []

    archive_enabled = as_bool(deep_get(config, ["archive", "enabled"], True), True)
    archive_cmd = [
        py,
        "tools/fetch_binance_archive.py",
        "--symbol",
        symbol,
        "--interval",
        str(deep_get(config, ["archive", "interval"], "5m")),
        "--market",
        str(deep_get(config, ["archive", "market"], "futures_um")),
        "--days",
        str(as_int(deep_get(config, ["archive", "days"], 365), 365)),
        "--base-url",
        str(deep_get(config, ["archive", "base_url"], "https://data.binance.vision")),
        "--output",
        str(ohlcv_path),
        "--report",
        str(run_dir / "archive_report.json"),
    ]
    start_date = deep_get(config, ["archive", "start_date"], "")
    end_date = deep_get(config, ["archive", "end_date"], "")
    if isinstance(start_date, str) and start_date.strip():
        archive_cmd += ["--start-date", start_date.strip()]
    if isinstance(end_date, str) and end_date.strip():
        archive_cmd += ["--end-date", end_date.strip()]
    steps.append(StepResult(name="archive_download", enabled=archive_enabled, command=archive_cmd))

    incremental_enabled = as_bool(deep_get(config, ["incremental", "enabled"], True), True)
    steps.append(
        StepResult(
            name="incremental_update",
            enabled=incremental_enabled,
            command=[
                py,
                "tools/stream_market_ws.py",
                "--symbol",
                symbol,
                "--interval",
                str(deep_get(config, ["incremental", "interval_minutes"], interval_min)),
                "--category",
                str(deep_get(config, ["incremental", "category"], category)),
                "--bars",
                str(as_int(deep_get(config, ["incremental", "bars"], 240), 240)),
                "--iterations",
                str(as_int(deep_get(config, ["incremental", "iterations"], 1), 1)),
                "--sleep-sec",
                str(as_float(deep_get(config, ["incremental", "sleep_sec"], 5.0), 5.0)),
                "--output",
                str(ohlcv_path),
                "--report",
                str(run_dir / "incremental_report.json"),
            ],
        )
    )

    gap_fill_enabled = as_bool(deep_get(config, ["gap_fill", "enabled"], True), True)
    gap_cmd = [
        py,
        "tools/gap_fill_klines.py",
        "--input",
        str(ohlcv_path),
        "--output",
        str(ohlcv_path),
        "--symbol",
        symbol,
        "--interval",
        str(deep_get(config, ["gap_fill", "interval_minutes"], interval_min)),
        "--category",
        str(deep_get(config, ["gap_fill", "category"], category)),
        "--max-ranges",
        str(as_int(deep_get(config, ["gap_fill", "max_ranges"], 200), 200)),
        "--report",
        str(run_dir / "gap_fill_report.json"),
    ]
    if as_bool(deep_get(config, ["gap_fill", "strict"], False), False):
        gap_cmd.append("--strict")
    steps.append(StepResult(name="gap_fill", enabled=gap_fill_enabled, command=gap_cmd))

    feature_enabled = as_bool(deep_get(config, ["feature_store", "enabled"], True), True)
    feature_cmd = [
        py,
        "tools/build_feature_store.py",
        "--input",
        str(ohlcv_path),
        "--output",
        str(feature_path),
        "--forward-bars",
        str(as_int(deep_get(config, ["feature_store", "forward_bars"], 12), 12)),
        "--report",
        str(run_dir / "feature_store_report.json"),
    ]
    if as_bool(deep_get(config, ["feature_store", "keep_na"], False), False):
        feature_cmd.append("--keep-na")
    steps.append(StepResult(name="feature_store", enabled=feature_enabled, command=feature_cmd))

    walk_enabled = as_bool(deep_get(config, ["walkforward", "enabled"], True), True)
    steps.append(
        StepResult(
            name="walkforward_backtest",
            enabled=walk_enabled,
            command=[
                py,
                "tools/backtest_walkforward.py",
                "--features",
                str(feature_path),
                "--output",
                str(backtest_path),
                "--train-window",
                str(as_int(deep_get(config, ["walkforward", "train_window"], 2400), 2400)),
                "--test-window",
                str(as_int(deep_get(config, ["walkforward", "test_window"], 480), 480)),
                "--step-window",
                str(as_int(deep_get(config, ["walkforward", "step_window"], 480), 480)),
                "--fee-bps",
                str(as_float(deep_get(config, ["walkforward", "fee_bps"], 6.0), 6.0)),
                "--slippage-bps",
                str(as_float(deep_get(config, ["walkforward", "slippage_bps"], 1.5), 1.5)),
                "--signal-threshold",
                str(
                    as_float(deep_get(config, ["walkforward", "signal_threshold"], 0.0002), 0.0002)
                ),
                "--max-leverage",
                str(as_float(deep_get(config, ["walkforward", "max_leverage"], 1.5), 1.5)),
                "--pred-scale",
                str(as_float(deep_get(config, ["walkforward", "pred_scale"], 0.002), 0.002)),
                "--interval-minutes",
                str(as_int(deep_get(config, ["walkforward", "interval_minutes"], 5), 5)),
            ],
        )
    )

    status = "PASS"
    for step in steps:
        result = run_command(step, dry_run=bool(args.dry_run))
        if result.status == "fail":
            status = "FAIL"
            break

    report = {
        "status": status if not args.dry_run else "PLANNED",
        "config": str(config_path),
        "run_dir": str(run_dir),
        "outputs": {
            "ohlcv_csv": str(ohlcv_path),
            "feature_csv": str(feature_path),
            "backtest_report": str(backtest_path),
        },
        "steps": [
            {
                "name": item.name,
                "enabled": item.enabled,
                "status": item.status,
                "command": item.command,
                "return_code": item.return_code,
                "started_at_utc": item.started_at_utc,
                "finished_at_utc": item.finished_at_utc,
                "error": item.error,
            }
            for item in steps
        ],
    }
    report_path = run_dir / "data_pipeline_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[PIPELINE] report: {report_path}")
    print(json.dumps({"status": report["status"], "run_dir": str(run_dir)}, ensure_ascii=False))
    if status == "FAIL":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
