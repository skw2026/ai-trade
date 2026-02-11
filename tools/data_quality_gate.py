#!/usr/bin/env python3
"""
研究数据质量门禁（D2）。

目的：
1. 在 Miner/Integrator 训练前，先对 OHLCV CSV 做硬性质量检查；
2. 将检查结果写为 JSON，可被闭环汇总报告直接消费；
3. 门禁失败时返回非 0，阻断后续训练流程。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import pathlib
import sys
from typing import Any, Dict, List, Optional


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("non-finite")
    return value


def parse_timestamp(row: Dict[str, str]) -> Optional[int]:
    # 允许 timestamp / ts / time 三种字段名。
    for key in ("timestamp", "ts", "time"):
        if key in row and row[key] != "":
            return int(float(row[key]))
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="研究数据质量门禁")
    parser.add_argument("--csv", required=True, help="输入 OHLCV CSV")
    parser.add_argument("--output", required=True, help="输出 JSON 报告路径")
    parser.add_argument("--min_rows", type=int, default=2000, help="最小样本条数")
    parser.add_argument("--max_nan_ratio", type=float, default=0.0, help="最大 NaN/解析失败比例")
    parser.add_argument(
        "--max_duplicate_ts_ratio",
        type=float,
        default=0.0,
        help="最大重复时间戳比例",
    )
    parser.add_argument(
        "--max_zero_volume_ratio",
        type=float,
        default=1.0,
        help="最大零成交量比例（1.0 代表不限制）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = pathlib.Path(args.csv)
    out_path = pathlib.Path(args.output)

    fail_reasons: List[str] = []
    warn_reasons: List[str] = []

    if not csv_path.is_file():
        payload = {
            "generated_at_utc": now_utc_iso(),
            "source_csv": str(csv_path),
            "status": "FAIL",
            "gate_pass": False,
            "fail_reasons": [f"输入文件不存在: {csv_path}"],
            "warn_reasons": [],
            "summary": {},
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ERROR] DQ_FAIL: {payload['fail_reasons'][0]}")
        return 1

    required = {"open", "high", "low", "close", "volume"}
    row_count = 0
    parse_error_count = 0
    non_positive_price_count = 0
    negative_volume_count = 0
    zero_volume_count = 0
    duplicate_ts_count = 0
    ts_monotonic_break_count = 0
    has_timestamp = False
    seen_ts = set()
    prev_ts: Optional[int] = None

    try:
        with csv_path.open("r", newline="", encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            headers = {h.lower() for h in (reader.fieldnames or [])}
            if not required.issubset(headers):
                missing = sorted(list(required - headers))
                fail_reasons.append(f"CSV 缺少必需列: {','.join(missing)}")
            has_timestamp = any(h in headers for h in ("timestamp", "ts", "time"))
            if not has_timestamp:
                warn_reasons.append("CSV 不包含 timestamp/ts/time，跳过时间序一致性检查")

            for row in reader:
                row_count += 1
                try:
                    open_ = parse_float(str(row["open"]))
                    high = parse_float(str(row["high"]))
                    low = parse_float(str(row["low"]))
                    close = parse_float(str(row["close"]))
                    volume = parse_float(str(row["volume"]))
                except Exception:
                    parse_error_count += 1
                    continue

                if open_ <= 0.0 or high <= 0.0 or low <= 0.0 or close <= 0.0:
                    non_positive_price_count += 1
                if volume < 0.0:
                    negative_volume_count += 1
                if volume == 0.0:
                    zero_volume_count += 1

                if has_timestamp:
                    try:
                        ts = parse_timestamp(row)
                    except Exception:
                        parse_error_count += 1
                        continue
                    if ts is None:
                        parse_error_count += 1
                        continue
                    if ts in seen_ts:
                        duplicate_ts_count += 1
                    else:
                        seen_ts.add(ts)
                    if prev_ts is not None and ts <= prev_ts:
                        ts_monotonic_break_count += 1
                    prev_ts = ts
    except Exception as exc:
        fail_reasons.append(f"CSV 读取失败: {exc}")

    if row_count < args.min_rows:
        fail_reasons.append(f"样本不足: rows={row_count} < min_rows={args.min_rows}")
    if row_count > 0:
        nan_ratio = parse_error_count / float(row_count)
        duplicate_ts_ratio = duplicate_ts_count / float(row_count)
        zero_volume_ratio = zero_volume_count / float(row_count)
    else:
        nan_ratio = 1.0
        duplicate_ts_ratio = 1.0
        zero_volume_ratio = 1.0

    if nan_ratio > args.max_nan_ratio:
        fail_reasons.append(
            f"解析失败比例超限: nan_ratio={nan_ratio:.6f} > max_nan_ratio={args.max_nan_ratio:.6f}"
        )
    if has_timestamp and duplicate_ts_ratio > args.max_duplicate_ts_ratio:
        fail_reasons.append(
            "重复时间戳比例超限: "
            f"duplicate_ts_ratio={duplicate_ts_ratio:.6f} > max_duplicate_ts_ratio={args.max_duplicate_ts_ratio:.6f}"
        )
    if has_timestamp and ts_monotonic_break_count > 0:
        fail_reasons.append(f"时间戳非严格递增: breaks={ts_monotonic_break_count}")
    if non_positive_price_count > 0:
        fail_reasons.append(f"存在非正价格: count={non_positive_price_count}")
    if negative_volume_count > 0:
        fail_reasons.append(f"存在负成交量: count={negative_volume_count}")
    if zero_volume_ratio > args.max_zero_volume_ratio:
        fail_reasons.append(
            "零成交量比例超限: "
            f"zero_volume_ratio={zero_volume_ratio:.6f} > max_zero_volume_ratio={args.max_zero_volume_ratio:.6f}"
        )

    gate_pass = len(fail_reasons) == 0
    payload: Dict[str, Any] = {
        "generated_at_utc": now_utc_iso(),
        "source_csv": str(csv_path),
        "status": "PASS" if gate_pass else "FAIL",
        "gate_pass": gate_pass,
        "fail_reasons": fail_reasons,
        "warn_reasons": warn_reasons,
        "summary": {
            "rows": row_count,
            "has_timestamp": has_timestamp,
            "parse_error_count": parse_error_count,
            "nan_ratio": nan_ratio,
            "duplicate_ts_count": duplicate_ts_count,
            "duplicate_ts_ratio": duplicate_ts_ratio,
            "ts_monotonic_break_count": ts_monotonic_break_count,
            "non_positive_price_count": non_positive_price_count,
            "negative_volume_count": negative_volume_count,
            "zero_volume_count": zero_volume_count,
            "zero_volume_ratio": zero_volume_ratio,
            "thresholds": {
                "min_rows": args.min_rows,
                "max_nan_ratio": args.max_nan_ratio,
                "max_duplicate_ts_ratio": args.max_duplicate_ts_ratio,
                "max_zero_volume_ratio": args.max_zero_volume_ratio,
            },
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[INFO] DQ_STATUS: {payload['status']}")
    print(
        "[INFO] DQ_SUMMARY: "
        f"rows={row_count}, nan_ratio={nan_ratio:.6f}, "
        f"duplicate_ts_ratio={duplicate_ts_ratio:.6f}, "
        f"zero_volume_ratio={zero_volume_ratio:.6f}"
    )
    if fail_reasons:
        for item in fail_reasons:
            print(f"[ERROR] DQ_FAIL_REASON: {item}")
    if warn_reasons:
        for item in warn_reasons:
            print(f"[WARN] DQ_WARN_REASON: {item}")

    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
