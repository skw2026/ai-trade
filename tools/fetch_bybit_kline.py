#!/usr/bin/env python3
"""
Bybit V5 历史 K 线抓取工具（离线研究数据准备）。

目标：
1. 从 Bybit 公共接口分页拉取历史 K 线；
2. 输出为 Miner/Integrator 可直接消费的 CSV：
   timestamp,open,high,low,close,volume

说明：
- 默认按 `--bars` 回溯抓取（以 `--end_ms` 为终点，默认当前时间）；
- 也可显式指定 `--start_ms` + `--end_ms`；
- `timestamp` 使用毫秒时间戳（UTC）。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Candle:
    """单根 K 线数据（与 CSV 字段一一对应）。"""

    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def log_info(message: str) -> None:
    print(f"[INFO] {message}")


def log_warn(message: str) -> None:
    print(f"[WARN] {message}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取 Bybit K 线并生成 Miner/Integrator 训练 CSV"
    )
    parser.add_argument("--symbol", default="BTCUSDT", help="交易对，例如 BTCUSDT")
    parser.add_argument(
        "--interval",
        default="5",
        help="K 线周期：1/3/5/15/30/60/120/240/360/720/D/W/M",
    )
    parser.add_argument(
        "--category",
        default="linear",
        choices=["linear", "inverse", "spot"],
        help="Bybit 产品类型",
    )
    parser.add_argument(
        "--bars",
        type=int,
        default=3000,
        help="目标拉取根数（当未显式指定 start_ms 时生效，默认 3000）",
    )
    parser.add_argument(
        "--start_ms",
        type=int,
        default=None,
        help="起始毫秒时间戳（可选，若提供则按区间拉取）",
    )
    parser.add_argument(
        "--end_ms",
        type=int,
        default=None,
        help="结束毫秒时间戳（可选，默认当前时间）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="单次请求上限（Bybit 最大 1000）",
    )
    parser.add_argument(
        "--max_pages",
        type=int,
        default=500,
        help="最大分页次数（防止异常条件下死循环）",
    )
    parser.add_argument(
        "--sleep_ms",
        type=int,
        default=120,
        help="分页间隔毫秒，降低触发限频概率",
    )
    parser.add_argument(
        "--timeout_sec", type=float, default=15.0, help="HTTP 超时时间（秒）"
    )
    parser.add_argument(
        "--base_url",
        default="https://api.bybit.com",
        help="Bybit API 基地址",
    )
    parser.add_argument(
        "--output",
        default="./data/research/ohlcv_5m.csv",
        help="输出 CSV 路径",
    )
    return parser.parse_args()


def normalize_interval(raw: str) -> str:
    value = raw.strip().upper()
    if value in {"D", "W", "M"}:
        return value
    if not value.isdigit():
        raise ValueError(f"非法 interval: {raw}")
    minute = int(value)
    if minute <= 0:
        raise ValueError(f"非法 interval: {raw}")
    return str(minute)


def interval_to_ms(interval: str) -> int:
    if interval.isdigit():
        return int(interval) * 60 * 1000
    mapping = {
        "D": 24 * 60 * 60 * 1000,
        "W": 7 * 24 * 60 * 60 * 1000,
        # 月线长度不固定，这里仅用于分页回溯的“步长近似”。
        "M": 30 * 24 * 60 * 60 * 1000,
    }
    if interval not in mapping:
        raise ValueError(f"不支持的 interval: {interval}")
    return mapping[interval]


def request_kline_page(
    *,
    base_url: str,
    category: str,
    symbol: str,
    interval: str,
    end_ms: int,
    limit: int,
    timeout_sec: float,
) -> List[Candle]:
    params = {
        "category": category,
        "symbol": symbol,
        "interval": interval,
        "end": str(end_ms),
        "limit": str(limit),
    }
    endpoint = f"{base_url.rstrip('/')}/v5/market/kline?{urlencode(params)}"
    request = Request(
        endpoint,
        headers={"User-Agent": "ai-trade-kline-fetcher/1.0"},
    )
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"HTTP 错误: status={exc.code}, body={body}") from exc
    except URLError as exc:
        raise RuntimeError(f"网络错误: {exc}") from exc

    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"响应不是合法 JSON: {payload[:256]}") from exc

    ret_code = int(decoded.get("retCode", -1))
    if ret_code != 0:
        raise RuntimeError(
            f"Bybit API 异常: retCode={ret_code}, retMsg={decoded.get('retMsg', '')}"
        )

    rows = decoded.get("result", {}).get("list", [])
    candles: List[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            candles.append(
                Candle(
                    timestamp_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
            )
        except (TypeError, ValueError):
            continue

    candles.sort(key=lambda item: item.timestamp_ms)
    return candles


def write_csv(path: pathlib.Path, candles: List[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for candle in candles:
            writer.writerow(
                [
                    candle.timestamp_ms,
                    f"{candle.open:.8f}",
                    f"{candle.high:.8f}",
                    f"{candle.low:.8f}",
                    f"{candle.close:.8f}",
                    f"{candle.volume:.8f}",
                ]
            )


def read_existing_candles(path: pathlib.Path) -> List[Candle]:
    if not path.exists():
        return []
    candles = []
    try:
        with path.open("r", encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                try:
                    candles.append(Candle(
                        timestamp_ms=int(row["timestamp"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    ))
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        log_warn(f"读取现有 CSV 失败: {e}")
        return []
    return candles


def main() -> int:
    args = parse_args()
    interval = normalize_interval(args.interval)
    interval_ms = interval_to_ms(interval)
    limit = max(1, min(1000, int(args.limit)))

    end_ms = args.end_ms if args.end_ms is not None else int(time.time() * 1000)
    if args.start_ms is not None and args.start_ms >= end_ms:
        raise ValueError("start_ms 必须小于 end_ms")
    if args.start_ms is None and args.bars <= 0:
        raise ValueError("未指定 start_ms 时，bars 必须 > 0")

    output_path = pathlib.Path(args.output)
    existing_candles: List[Candle] = []

    # 确定起始时间：显式指定 > 增量更新 > 默认回溯
    if args.start_ms is not None:
        target_start_ms = args.start_ms
    elif output_path.exists():
        existing_candles = read_existing_candles(output_path)
        if existing_candles:
            existing_candles.sort(key=lambda x: x.timestamp_ms)
            last_ts = existing_candles[-1].timestamp_ms
            target_start_ms = last_ts + interval_ms
            log_info(f"INCREMENTAL: 发现现有数据 {len(existing_candles)} 条，最后时间戳 {last_ts}，将从 {target_start_ms} 开始抓取")
        else:
            target_start_ms = end_ms - max(0, args.bars - 1) * interval_ms
    else:
        target_start_ms = end_ms - max(0, args.bars - 1) * interval_ms

    log_info(
        "FETCH_START: "
        f"symbol={args.symbol}, category={args.category}, interval={interval}, "
        f"end_ms={end_ms}, target_start_ms={target_start_ms}, bars={args.bars}"
    )

    if target_start_ms > end_ms:
        log_info("INCREMENTAL: 数据已是最新，无需更新")
        return 0

    candles_by_ts: Dict[int, Candle] = {}
    page_count = 0
    cursor_end_ms = end_ms

    while page_count < max(1, args.max_pages):
        if args.start_ms is not None and cursor_end_ms < target_start_ms:
            break
        if args.start_ms is None and args.bars > 0 and len(candles_by_ts) >= args.bars:
            break

        if args.start_ms is None and args.bars > 0:
            remaining = args.bars - len(candles_by_ts)
            page_limit = min(limit, max(1, remaining))
        else:
            page_limit = limit

        batch = request_kline_page(
            base_url=args.base_url,
            category=args.category,
            symbol=args.symbol,
            interval=interval,
            end_ms=cursor_end_ms,
            limit=page_limit,
            timeout_sec=float(args.timeout_sec),
        )
        page_count += 1

        if not batch:
            log_warn(
                f"分页返回空数据，提前结束: page={page_count}, cursor_end_ms={cursor_end_ms}"
            )
            break

        for candle in batch:
            candles_by_ts[candle.timestamp_ms] = candle

        earliest = batch[0].timestamp_ms
        latest = batch[-1].timestamp_ms
        log_info(
            "FETCH_PAGE: "
            f"page={page_count}, got={len(batch)}, "
            f"range=[{earliest},{latest}], total_unique={len(candles_by_ts)}"
        )

        # 下一页从“当前最早 K 线之前一根”开始，避免重复页卡住。
        next_cursor = earliest - interval_ms
        if next_cursor >= cursor_end_ms:
            next_cursor = cursor_end_ms - interval_ms
        cursor_end_ms = next_cursor

        if args.sleep_ms > 0:
            time.sleep(max(0.0, args.sleep_ms / 1000.0))

    if not candles_by_ts:
        raise RuntimeError("未获取到任何 K 线数据，请检查 symbol/interval/category。")

    candles = sorted(candles_by_ts.values(), key=lambda item: item.timestamp_ms)
    candles = [
        candle
        for candle in candles
        if target_start_ms <= candle.timestamp_ms <= end_ms
    ]
    if args.bars > 0 and len(candles) > args.bars:
        candles = candles[-args.bars :]

    if not candles:
        raise RuntimeError("过滤后无有效 K 线，请调整 start/end/bars 参数。")

    gap_count = 0
    for idx in range(1, len(candles)):
        gap_ms = candles[idx].timestamp_ms - candles[idx - 1].timestamp_ms
        if gap_ms > int(math.ceil(interval_ms * 1.5)):
            gap_count += 1

    # 合并现有数据与新数据（去重）
    if existing_candles:
        merged_map = {c.timestamp_ms: c for c in existing_candles}
        for c in candles:
            merged_map[c.timestamp_ms] = c
        candles = sorted(merged_map.values(), key=lambda x: x.timestamp_ms)

    write_csv(output_path, candles)
    log_info(
        "FETCH_DONE: "
        f"output={output_path}, bars={len(candles)}, pages={page_count}, "
        f"first_ts={candles[0].timestamp_ms}, last_ts={candles[-1].timestamp_ms}, "
        f"gap_count={gap_count}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
