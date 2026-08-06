#!/usr/bin/env python3
"""Collect or replay Bybit L50 order-book/public-trade alpha data.

Live capture writes the exchange payloads verbatim to JSONL.gz.  Replay uses
exchange timestamps only and produces deterministic one-second causal feature
bars.  These features remain forward-development evidence until a sufficiently
long capture passes the independent research gates.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import json
import math
import pathlib
import ssl
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, TextIO, Tuple


SCHEMA_VERSION = "bybit_microstructure_v1"
DEFAULT_URL = "wss://stream.bybit.com/v5/public/linear"


def parse_level(raw: Any) -> Tuple[float, float]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError("order-book level must be [price,size]")
    price, size = float(raw[0]), float(raw[1])
    if price <= 0.0 or size < 0.0 or not math.isfinite(price + size):
        raise ValueError("invalid order-book price/size")
    return price, size


class OrderBook:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.initialized = False
        self.last_update_id = -1
        self.last_sequence = -1

    @staticmethod
    def _apply_levels(destination: Dict[float, float], rows: Iterable[Any]) -> None:
        for raw in rows:
            price, size = parse_level(raw)
            if size == 0.0:
                destination.pop(price, None)
            else:
                destination[price] = size

    def apply(self, message: Mapping[str, Any]) -> int:
        data = message.get("data")
        if not isinstance(data, dict):
            raise ValueError("order-book message is missing data object")
        update_id = int(data.get("u", -1))
        sequence = int(data.get("seq", -1))
        message_type = str(message.get("type", ""))
        reset = message_type == "snapshot" or update_id == 1
        if reset:
            self.bids.clear()
            self.asks.clear()
            self.initialized = True
            self.last_update_id = -1
            self.last_sequence = -1
        elif not self.initialized:
            raise ValueError("order-book delta received before snapshot")
        if not reset and update_id <= self.last_update_id:
            raise ValueError("order-book update id is not strictly increasing")
        if not reset and sequence < self.last_sequence:
            raise ValueError("order-book cross sequence regressed")
        self._apply_levels(self.bids, data.get("b", []))
        self._apply_levels(self.asks, data.get("a", []))
        if not self.bids or not self.asks:
            raise ValueError("order-book has an empty side")
        if max(self.bids) >= min(self.asks):
            raise ValueError("order-book is crossed")
        self.last_update_id = update_id
        self.last_sequence = sequence
        timestamp = int(message.get("cts") or message.get("ts") or 0)
        if timestamp <= 0:
            raise ValueError("order-book exchange timestamp is missing")
        return timestamp

    @staticmethod
    def _imbalance(bids: Sequence[Tuple[float, float]], asks: Sequence[Tuple[float, float]]) -> float:
        bid_size = sum(size for _, size in bids)
        ask_size = sum(size for _, size in asks)
        total = bid_size + ask_size
        return (bid_size - ask_size) / total if total > 0.0 else 0.0

    def metrics(self) -> Dict[str, float]:
        if not self.initialized or not self.bids or not self.asks:
            raise ValueError("order-book snapshot is unavailable")
        bids = sorted(self.bids.items(), reverse=True)
        asks = sorted(self.asks.items())
        best_bid, bid_size = bids[0]
        best_ask, ask_size = asks[0]
        mid = (best_bid + best_ask) / 2.0
        top_total = bid_size + ask_size
        microprice = (
            (best_ask * bid_size + best_bid * ask_size) / top_total
            if top_total > 0.0
            else mid
        )
        depth_1 = top_total
        depth_20 = sum(size for _, size in bids[:20]) + sum(size for _, size in asks[:20])
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_bps": (best_ask - best_bid) / mid * 10000.0,
            "microprice": microprice,
            "book_imbalance_l1": self._imbalance(bids[:1], asks[:1]),
            "book_imbalance_l5": self._imbalance(bids[:5], asks[:5]),
            "book_imbalance_l20": self._imbalance(bids[:20], asks[:20]),
            "depth_slope": math.log(max(depth_20, 1e-12) / max(depth_1, 1e-12)),
        }


@dataclass
class FeatureBucket:
    timestamp_ms: int
    metrics: Dict[str, float] | None = None
    book_update_count: int = 0
    trade_count: int = 0
    buy_quote_volume: float = 0.0
    sell_quote_volume: float = 0.0


FEATURE_FIELDS = (
    "best_bid",
    "best_ask",
    "mid",
    "spread_bps",
    "microprice",
    "book_imbalance_l1",
    "book_imbalance_l5",
    "book_imbalance_l20",
    "depth_slope",
)


class MicrostructureAggregator:
    def __init__(self, *, symbol: str, bucket_ms: int = 1000) -> None:
        if bucket_ms <= 0:
            raise ValueError("bucket_ms must be positive")
        self.symbol = symbol.upper()
        self.bucket_ms = bucket_ms
        self.book = OrderBook()
        self.buckets: Dict[int, FeatureBucket] = {}
        self.trade_ids: set[str] = set()
        self.trade_id_order: deque[str] = deque()
        self.trade_id_capacity = 200000

    def _remember_trade_id(self, trade_id: str) -> bool:
        if trade_id in self.trade_ids:
            return False
        self.trade_ids.add(trade_id)
        self.trade_id_order.append(trade_id)
        if len(self.trade_id_order) > self.trade_id_capacity:
            self.trade_ids.discard(self.trade_id_order.popleft())
        return True

    def _bucket(self, timestamp_ms: int) -> FeatureBucket:
        key = timestamp_ms - timestamp_ms % self.bucket_ms
        return self.buckets.setdefault(key, FeatureBucket(timestamp_ms=key))

    def process(self, message: Mapping[str, Any]) -> bool:
        topic = str(message.get("topic", ""))
        if topic.startswith("orderbook."):
            if not topic.endswith(f".{self.symbol}"):
                return False
            timestamp = self.book.apply(message)
            bucket = self._bucket(timestamp)
            bucket.metrics = self.book.metrics()
            bucket.book_update_count += 1
            return True
        if topic == f"publicTrade.{self.symbol}":
            rows = message.get("data")
            if not isinstance(rows, list):
                raise ValueError("publicTrade data must be an array")
            processed = False
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("publicTrade row must be an object")
                trade_id = str(row.get("i", ""))
                if not trade_id:
                    raise ValueError("publicTrade trade id is missing")
                if not self._remember_trade_id(trade_id):
                    continue
                timestamp = int(row.get("T", 0))
                side = str(row.get("S", ""))
                size = float(row.get("v", 0.0))
                price = float(row.get("p", 0.0))
                if timestamp <= 0 or side not in {"Buy", "Sell"} or size <= 0.0 or price <= 0.0:
                    raise ValueError("invalid publicTrade values")
                bucket = self._bucket(timestamp)
                if self.book.initialized:
                    bucket.metrics = self.book.metrics()
                bucket.trade_count += 1
                if side == "Buy":
                    bucket.buy_quote_volume += size * price
                else:
                    bucket.sell_quote_volume += size * price
                processed = True
            return processed
        return False

    def rows(self) -> List[Dict[str, float | int]]:
        output: List[Dict[str, float | int]] = []
        carried_metrics: Dict[str, float] | None = None
        for timestamp in sorted(self.buckets):
            bucket = self.buckets[timestamp]
            if bucket.metrics is not None:
                carried_metrics = bucket.metrics
            if carried_metrics is None:
                continue
            total_quote = bucket.buy_quote_volume + bucket.sell_quote_volume
            row: Dict[str, float | int] = {"timestamp": timestamp}
            row.update(carried_metrics)
            row.update(
                {
                    "book_update_count": bucket.book_update_count,
                    "trade_count": bucket.trade_count,
                    "buy_quote_volume": bucket.buy_quote_volume,
                    "sell_quote_volume": bucket.sell_quote_volume,
                    "trade_imbalance": (
                        (bucket.buy_quote_volume - bucket.sell_quote_volume) / total_quote
                        if total_quote > 0.0
                        else 0.0
                    ),
                }
            )
            output.append(row)
        return output


def open_text_auto(path: pathlib.Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def replay_jsonl(path: pathlib.Path, *, symbol: str, bucket_ms: int) -> Tuple[List[Dict[str, Any]], int]:
    aggregator = MicrostructureAggregator(symbol=symbol, bucket_ms=bucket_ms)
    raw_count = 0
    with open_text_auto(path) as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw_count += 1
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("payload is not an object")
                aggregator.process(payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid raw message at line {line_number}: {exc}") from exc
    rows = aggregator.rows()
    if not rows:
        raise ValueError("capture produced no microstructure feature rows")
    return rows, raw_count


def write_feature_csv(path: pathlib.Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "timestamp",
        *FEATURE_FIELDS,
        "book_update_count",
        "trade_count",
        "buy_quote_volume",
        "sell_quote_volume",
        "trade_imbalance",
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    temporary.replace(path)


async def capture_live(
    *, url: str, symbol: str, depth: int, duration_sec: float, raw_output: pathlib.Path
) -> int:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - depends on research image
        raise RuntimeError("live mode requires tools/requirements-research.txt") from exc
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    deadline = time.monotonic() + duration_sec
    paths = ssl.get_default_verify_paths()
    fallback = pathlib.Path("/etc/ssl/cert.pem")
    ssl_context = (
        ssl.create_default_context(cafile=str(fallback))
        if paths.cafile is None and fallback.is_file()
        else ssl.create_default_context()
    )
    with gzip.open(raw_output, "wt", encoding="utf-8") as handle:
        async with websockets.connect(
            url,
            ssl=ssl_context,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
            max_size=8 * 1024 * 1024,
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "op": "subscribe",
                        "args": [
                            f"orderbook.{depth}.{symbol}",
                            f"publicTrade.{symbol}",
                        ],
                    },
                    separators=(",", ":"),
                )
            )
            while time.monotonic() < deadline:
                remaining = max(0.01, deadline - time.monotonic())
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=min(5.0, remaining))
                except asyncio.TimeoutError:
                    continue
                if not isinstance(raw, str):
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("topic"):
                    handle.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
                    count += 1
    if count == 0:
        raise RuntimeError("live capture received no market-data messages")
    return count


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("live", "replay"))
    parser.add_argument("--raw", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--depth", type=int, default=50, choices=(50,))
    parser.add_argument("--bucket-ms", type=int, default=1000)
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.research_domain != "development":
        raise ValueError("microstructure collection is development-only")
    if args.symbol.strip().upper() != "SOLUSDT":
        raise ValueError("only the audited SOLUSDT contract is supported")
    raw_path = pathlib.Path(args.raw)
    if args.mode == "live":
        if args.duration_sec <= 0.0:
            raise ValueError("duration-sec must be positive")
        asyncio.run(
            capture_live(
                url=args.url,
                symbol="SOLUSDT",
                depth=args.depth,
                duration_sec=args.duration_sec,
                raw_output=raw_path,
            )
        )
    rows, raw_count = replay_jsonl(
        raw_path, symbol="SOLUSDT", bucket_ms=args.bucket_ms
    )
    feature_path = pathlib.Path(args.features)
    write_feature_csv(feature_path, rows)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "source": "bybit_public_websocket_v5",
        "url": args.url,
        "topics": ["orderbook.50.SOLUSDT", "publicTrade.SOLUSDT"],
        "timestamp_semantics": "exchange cts/T; one-second bucket includes only events at or before bucket end",
        "raw": {"path": str(raw_path), "sha256": sha256_file(raw_path), "message_count": raw_count},
        "features": {
            "path": str(feature_path),
            "sha256": sha256_file(feature_path),
            "row_count": len(rows),
            "first_timestamp": int(rows[0]["timestamp"]),
            "last_timestamp": int(rows[-1]["timestamp"]),
        },
        "quality": {
            "book_update_count": sum(int(row["book_update_count"]) for row in rows),
            "trade_count": sum(int(row["trade_count"]) for row in rows),
            "trade_bucket_count": sum(int(row["trade_count"]) > 0 for row in rows),
            "mean_spread_bps": sum(float(row["spread_bps"]) for row in rows)
            / len(rows),
        },
        "gates_remaining": [
            "minimum_forward_capture_duration",
            "offline_online_feature_parity",
            "development_economic_screen",
            "independent_selection",
            "untouched_final_holdout",
        ],
    }
    report_path = pathlib.Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "PASS", "raw_messages": raw_count, "feature_rows": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
