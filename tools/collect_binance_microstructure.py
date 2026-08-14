#!/usr/bin/env python3
"""Collect/replay Binance USD-M SOLUSDT L20 and aggregate trades.

Public payloads are persisted without mutation. Replay emits deterministic
one-second exchange-time buckets for development-only information-set tests.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import hashlib
import json
import pathlib
import ssl
import tempfile
import time
from typing import Any, Dict, Mapping, Sequence, TextIO, Tuple

import collect_bybit_microstructure as common


SCHEMA_VERSION = "binance_sol_microstructure_v1"
SYMBOL = "SOLUSDT"
PUBLIC_URL = "wss://fstream.binance.com/public/ws/solusdt@depth20@100ms"
MARKET_URL = "wss://fstream.binance.com/market/ws/solusdt@aggTrade"
OUTPUT_FIELDS = ("timestamp", *common.TARGET_ROW_FIELDS)
ALIGNMENT_CONTRACT = {
    "method": "exchange_second_bucket_v1",
    "symbol": SYMBOL,
    "book_stream": "solusdt@depth20@100ms",
    "trade_stream": "solusdt@aggTrade",
    "book_semantics": "each_partial_depth_event_replaces_complete_top_20",
    "timestamp_semantics": "transaction_time_T_else_event_time_E_bucket_start",
    "finalization": "book_and_trade_streams_both_advanced_beyond_bucket",
    "future_fill_permitted": False,
    "backfill_permitted": False,
}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_text_auto(path: pathlib.Path) -> TextIO:
    return (
        gzip.open(path, "rt", encoding="utf-8")
        if path.suffix == ".gz"
        else path.open("r", encoding="utf-8")
    )


class BinanceMicrostructureAggregator:
    """Translate Binance payloads into the existing audited bucket engine."""

    def __init__(self, *, symbol: str = SYMBOL, bucket_ms: int = 1000) -> None:
        if symbol.upper() != SYMBOL:
            raise ValueError("only SOLUSDT is supported")
        self.engine = common.MicrostructureAggregator(
            symbol=SYMBOL, bucket_ms=bucket_ms
        )
        self.previous_bids: Dict[float, float] = {}
        self.previous_asks: Dict[float, float] = {}
        self.last_depth_update_id = -1

    @staticmethod
    def _unwrap(message: Mapping[str, Any]) -> Mapping[str, Any]:
        data = message.get("data")
        return data if "stream" in message and isinstance(data, Mapping) else message

    @staticmethod
    def _levels(raw: Any, side: str) -> Dict[float, float]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
            raise ValueError(f"partial depth {side} must contain 1..20 levels")
        result: Dict[float, float] = {}
        for item in raw:
            price, size = common.parse_level(item)
            if size <= 0.0 or price in result:
                raise ValueError(f"partial depth {side} is invalid")
            result[price] = size
        return result

    def _depth(self, payload: Mapping[str, Any]) -> bool:
        if str(payload.get("s") or "").upper() != SYMBOL:
            return False
        if payload.get("st") is not None and int(payload["st"]) != 1:
            return False
        timestamp = int(payload.get("T") or payload.get("E") or 0)
        update_id = int(payload.get("u") or 0)
        if timestamp <= 0 or update_id <= self.last_depth_update_id:
            raise ValueError("partial depth timestamp/update id is invalid")
        bids = self._levels(payload.get("b"), "bids")
        asks = self._levels(payload.get("a"), "asks")
        if max(bids) >= min(asks):
            raise ValueError("partial depth snapshot is crossed")
        if self.last_depth_update_id < 0:
            message_type = "snapshot"
            bid_updates = [[str(p), str(q)] for p, q in bids.items()]
            ask_updates = [[str(p), str(q)] for p, q in asks.items()]
        else:
            message_type = "delta"
            bid_updates = [
                [str(p), str(bids.get(p, 0.0))]
                for p in sorted(set(self.previous_bids) | set(bids), reverse=True)
                if self.previous_bids.get(p) != bids.get(p)
            ]
            ask_updates = [
                [str(p), str(asks.get(p, 0.0))]
                for p in sorted(set(self.previous_asks) | set(asks))
                if self.previous_asks.get(p) != asks.get(p)
            ]
        processed = self.engine.process(
            {
                "topic": f"orderbook.20.{SYMBOL}",
                "type": message_type,
                "ts": int(payload.get("E") or timestamp),
                "cts": timestamp,
                "data": {
                    "s": SYMBOL,
                    "b": bid_updates,
                    "a": ask_updates,
                    "u": update_id,
                    "seq": update_id,
                },
            }
        )
        self.previous_bids, self.previous_asks = bids, asks
        self.last_depth_update_id = update_id
        return processed

    def _trade(self, payload: Mapping[str, Any]) -> bool:
        if str(payload.get("s") or "").upper() != SYMBOL:
            return False
        if payload.get("st") is not None and int(payload["st"]) != 1:
            return False
        timestamp = int(payload.get("T") or 0)
        trade_id = str(payload.get("a") if payload.get("a") is not None else "")
        price, size, maker = (
            float(payload.get("p") or 0.0),
            float(payload.get("q") or 0.0),
            payload.get("m"),
        )
        if (
            timestamp <= 0
            or not trade_id
            or price <= 0.0
            or size <= 0.0
            or not isinstance(maker, bool)
        ):
            raise ValueError("aggregate trade values are invalid")
        return self.engine.process(
            {
                "topic": f"publicTrade.{SYMBOL}",
                "type": "snapshot",
                "ts": int(payload.get("E") or timestamp),
                "data": [
                    {
                        "T": timestamp,
                        "s": SYMBOL,
                        "S": "Sell" if maker else "Buy",
                        "v": size,
                        "p": price,
                        "i": trade_id,
                    }
                ],
            }
        )

    def process(self, message: Mapping[str, Any]) -> bool:
        payload = self._unwrap(message)
        event_type = str(payload.get("e") or "")
        if event_type == "depthUpdate":
            return self._depth(payload)
        if event_type == "aggTrade":
            return self._trade(payload)
        return False

    def rows(self) -> Sequence[Dict[str, float | int]]:
        return self.engine.rows()

    def finalized_through(self) -> int | None:
        return self.engine.finalized_through()


def replay_jsonl(
    path: pathlib.Path, *, symbol: str = SYMBOL, bucket_ms: int = 1000
) -> Tuple[list[Dict[str, Any]], int]:
    aggregator = BinanceMicrostructureAggregator(symbol=symbol, bucket_ms=bucket_ms)
    count = 0
    with open_text_auto(path) as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            count += 1
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("payload is not an object")
                aggregator.process(payload)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid raw message at line {line_number}: {exc}"
                ) from exc
    watermark = aggregator.finalized_through()
    rows = [
        dict(row)
        for row in aggregator.rows()
        if watermark is not None and int(row["timestamp"]) <= watermark
    ]
    if not rows:
        raise ValueError("capture produced no finalized feature rows")
    return rows, count


def write_feature_csv(
    path: pathlib.Path, rows: Sequence[Mapping[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False
    ) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row[name] for name in OUTPUT_FIELDS})
    temporary.replace(path)


async def capture_live(
    *, public_url: str, market_url: str, duration_sec: float, raw_output: pathlib.Path
) -> int:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("live mode requires research requirements") from exc
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    fallback = pathlib.Path("/etc/ssl/cert.pem")
    paths = ssl.get_default_verify_paths()
    context = (
        ssl.create_default_context(cafile=str(fallback))
        if paths.cafile is None and fallback.is_file()
        else ssl.create_default_context()
    )
    deadline = time.monotonic() + duration_sec
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def receive(url: str) -> None:
        try:
            async with websockets.connect(
                url,
                ssl=context,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=10,
                max_size=8 * 1024 * 1024,
            ) as socket:
                while time.monotonic() < deadline:
                    try:
                        raw = await asyncio.wait_for(
                            socket.recv(),
                            timeout=min(
                                5.0, max(0.01, deadline - time.monotonic())
                            ),
                        )
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(raw, str):
                        await queue.put(raw)
        finally:
            # Never leave the writer blocked if one public endpoint fails.
            await queue.put(None)

    tasks = [asyncio.create_task(receive(url)) for url in (public_url, market_url)]
    completed = count = 0
    with gzip.open(raw_output, "wt", encoding="utf-8") as handle:
        while completed < len(tasks):
            item = await queue.get()
            if item is None:
                completed += 1
            else:
                json.loads(item)
                handle.write(item.rstrip("\r\n") + "\n")
                count += 1
    await asyncio.gather(*tasks)
    if count == 0:
        raise RuntimeError("live capture received no market-data messages")
    return count


def build_capture_report(
    *,
    raw_path: pathlib.Path,
    feature_path: pathlib.Path,
    rows: Sequence[Mapping[str, Any]],
    raw_count: int,
    public_url: str,
    market_url: str,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "source": "binance_usdm_public_websocket",
        "symbol": SYMBOL,
        "urls": {"partial_depth": public_url, "aggregate_trade": market_url},
        "alignment_contract": ALIGNMENT_CONTRACT,
        "raw": {
            "path": str(raw_path),
            "sha256": sha256_file(raw_path),
            "message_count": int(raw_count),
        },
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
            "mean_spread_bps": sum(float(row["spread_bps"]) for row in rows) / len(rows),
        },
        "next_gate": "minimum_forward_capture_duration",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("live", "replay"))
    parser.add_argument("--raw", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--symbol", default=SYMBOL, choices=(SYMBOL,))
    parser.add_argument("--bucket-ms", type=int, default=1000, choices=(1000,))
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--public-url", default=PUBLIC_URL)
    parser.add_argument("--market-url", default=MARKET_URL)
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_path = pathlib.Path(args.raw)
    if args.mode == "live":
        if args.duration_sec <= 0.0:
            raise ValueError("duration-sec must be positive")
        asyncio.run(
            capture_live(
                public_url=args.public_url,
                market_url=args.market_url,
                duration_sec=args.duration_sec,
                raw_output=raw_path,
            )
        )
    rows, count = replay_jsonl(raw_path, symbol=args.symbol, bucket_ms=args.bucket_ms)
    feature_path = pathlib.Path(args.features)
    write_feature_csv(feature_path, rows)
    report = build_capture_report(
        raw_path=raw_path,
        feature_path=feature_path,
        rows=rows,
        raw_count=count,
        public_url=args.public_url,
        market_url=args.market_url,
    )
    report_path = pathlib.Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "PASS", "raw_messages": count, "feature_rows": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
