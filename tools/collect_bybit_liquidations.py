#!/usr/bin/env python3
"""Collect/replay the public Bybit SOLUSDT all-liquidation stream.

The raw topic payload is persisted unchanged.  Replay creates sparse,
exchange-time one-second event buckets; absence of a row never proves absence
of liquidation unless a separately checksum-bound connection interval covers
the complete bucket.
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


SCHEMA_VERSION = "bybit_sol_all_liquidation_v1"
ARTIFACT_PATH_CONTRACT = "capture_root_relative_v1"
SYMBOL = "SOLUSDT"
TOPIC = f"allLiquidation.{SYMBOL}"
PUBLIC_URL = "wss://stream.bybit.com/v5/public/linear"
OUTPUT_FIELDS = (
    "timestamp",
    "long_liquidation_count",
    "long_liquidation_qty",
    "long_liquidation_notional",
    "short_liquidation_count",
    "short_liquidation_qty",
    "short_liquidation_notional",
)
ALIGNMENT_CONTRACT = {
    "method": "exchange_second_sparse_event_bucket_v1",
    "topic": TOPIC,
    "timestamp_semantics": "liquidation_updated_time_T_bucket_start",
    "side_semantics": {"Buy": "long_position_liquidated", "Sell": "short_position_liquidated"},
    "zero_semantics": "zero_only_when_complete_connection_interval_covers_bucket",
    "decision_lag_seconds": 1,
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
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("r", encoding="utf-8")


def _event_values(event: Mapping[str, Any]) -> Tuple[int, str, float, float]:
    try:
        timestamp = int(event.get("T") or 0)
        symbol = str(event.get("s") or "").upper()
        side = str(event.get("S") or "")
        quantity = float(event.get("v") or 0.0)
        price = float(event.get("p") or 0.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid liquidation values") from exc
    if timestamp <= 0 or symbol != SYMBOL or side not in {"Buy", "Sell"} or quantity <= 0.0 or price <= 0.0:
        raise ValueError("invalid liquidation values")
    return timestamp, side, quantity, price


def replay_jsonl(path: pathlib.Path) -> Tuple[list[Dict[str, Any]], int]:
    buckets: Dict[int, Dict[str, Any]] = {}
    raw_count = 0
    with open_text_auto(path) as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            raw_count += 1
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict) or payload.get("topic") != TOPIC:
                    raise ValueError("invalid liquidation topic")
                data = payload.get("data")
                if not isinstance(data, list):
                    raise ValueError("invalid liquidation data")
                for event in data:
                    if not isinstance(event, dict):
                        raise ValueError("invalid liquidation event")
                    timestamp, side, quantity, price = _event_values(event)
                    bucket = timestamp // 1000 * 1000
                    row = buckets.setdefault(
                        bucket,
                        {
                            "timestamp": bucket,
                            "long_liquidation_count": 0,
                            "long_liquidation_qty": 0.0,
                            "long_liquidation_notional": 0.0,
                            "short_liquidation_count": 0,
                            "short_liquidation_qty": 0.0,
                            "short_liquidation_notional": 0.0,
                        },
                    )
                    prefix = "long" if side == "Buy" else "short"
                    row[f"{prefix}_liquidation_count"] += 1
                    row[f"{prefix}_liquidation_qty"] += quantity
                    row[f"{prefix}_liquidation_notional"] += quantity * price
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid liquidation message at line {line_number}: {exc}") from exc
    return [buckets[key] for key in sorted(buckets)], raw_count


def write_feature_csv(path: pathlib.Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] for field in OUTPUT_FIELDS})
    temporary.replace(path)


async def capture_live(*, public_url: str, duration_sec: float, raw_output: pathlib.Path) -> Tuple[int, int, int]:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("live mode requires research requirements") from exc
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    fallback = pathlib.Path("/etc/ssl/cert.pem")
    paths = ssl.get_default_verify_paths()
    context = ssl.create_default_context(cafile=str(fallback)) if paths.cafile is None and fallback.is_file() else ssl.create_default_context()
    count = 0
    with gzip.open(raw_output, "wt", encoding="utf-8") as handle:
        async with websockets.connect(
            public_url,
            ssl=context,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
            max_size=8 * 1024 * 1024,
        ) as socket:
            await socket.send(json.dumps({"op": "subscribe", "args": [TOPIC]}, separators=(",", ":")))
            subscribed = False
            subscription_deadline = time.monotonic() + 15.0
            while not subscribed:
                remaining = subscription_deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("liquidation subscription acknowledgement timed out")
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                if not isinstance(raw, str):
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("op") == "subscribe":
                    if payload.get("success") is not True:
                        raise RuntimeError("liquidation subscription was rejected")
                    subscribed = True
            started_epoch_ms = int(time.time() * 1000)
            deadline = time.monotonic() + duration_sec
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=min(5.0, max(0.01, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    continue
                if not isinstance(raw, str):
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("topic") == TOPIC:
                    handle.write(raw.rstrip("\r\n") + "\n")
                    count += 1
            completed_epoch_ms = int(time.time() * 1000)
    if completed_epoch_ms - started_epoch_ms < max(1, int(duration_sec * 1000) - 1500):
        raise RuntimeError("liquidation connection interval ended early")
    return count, started_epoch_ms, completed_epoch_ms


def build_capture_report(
    *, capture_root: pathlib.Path, raw_path: pathlib.Path, feature_path: pathlib.Path,
    rows: Sequence[Mapping[str, Any]],
    raw_count: int, capture_started_epoch_ms: int, capture_completed_epoch_ms: int,
    public_url: str,
) -> Dict[str, Any]:
    if capture_started_epoch_ms <= 0 or capture_completed_epoch_ms <= capture_started_epoch_ms:
        raise ValueError("invalid connected capture interval")
    long_count = sum(int(row["long_liquidation_count"]) for row in rows)
    short_count = sum(int(row["short_liquidation_count"]) for row in rows)
    root = capture_root.resolve()
    recorded_paths: Dict[str, str] = {}
    for kind, artifact in (("raw", raw_path), ("features", feature_path)):
        resolved = artifact.resolve()
        expected_parent = (root / kind / SYMBOL).resolve()
        if resolved.parent != expected_parent:
            raise ValueError("artifact path escapes capture root")
        recorded_paths[kind] = resolved.relative_to(root).as_posix()
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_path_contract": ARTIFACT_PATH_CONTRACT,
        "status": "PASS",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "promotion_authority": False,
        "demo_activation_authorized": False,
        "live_activation_authorized": False,
        "source": "bybit_public_websocket_v5_all_liquidation",
        "symbol": SYMBOL,
        "url": public_url,
        "alignment_contract": ALIGNMENT_CONTRACT,
        "coverage": {
            "capture_started_epoch_ms": int(capture_started_epoch_ms),
            "capture_completed_epoch_ms": int(capture_completed_epoch_ms),
            "duration_ms": int(capture_completed_epoch_ms - capture_started_epoch_ms),
            "connection_continuous": True,
        },
        "raw": {"path": recorded_paths["raw"], "sha256": sha256_file(raw_path), "message_count": int(raw_count)},
        "features": {
            "path": recorded_paths["features"], "sha256": sha256_file(feature_path), "row_count": len(rows),
            "first_timestamp": int(rows[0]["timestamp"]) if rows else None,
            "last_timestamp": int(rows[-1]["timestamp"]) if rows else None,
        },
        "quality": {
            "liquidation_event_count": long_count + short_count,
            "long_liquidation_count": long_count,
            "short_liquidation_count": short_count,
            "event_bucket_count": len(rows),
        },
        "next_gate": "minimum_forward_connection_coverage",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("live", "replay"))
    parser.add_argument("--raw", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--duration-sec", type=float, default=30.0)
    parser.add_argument("--public-url", default=PUBLIC_URL)
    parser.add_argument("--capture-started-epoch-ms", type=int, default=0)
    parser.add_argument("--capture-completed-epoch-ms", type=int, default=0)
    parser.add_argument("--research-domain", default="development", choices=("development",))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_path = pathlib.Path(args.raw)
    started, completed = args.capture_started_epoch_ms, args.capture_completed_epoch_ms
    if args.mode == "live":
        if args.duration_sec <= 0.0:
            raise ValueError("duration-sec must be positive")
        _, started, completed = asyncio.run(capture_live(public_url=args.public_url, duration_sec=args.duration_sec, raw_output=raw_path))
    elif started <= 0 or completed <= started:
        raise ValueError("replay mode requires the original connected capture interval")
    rows, count = replay_jsonl(raw_path)
    feature_path = pathlib.Path(args.features)
    write_feature_csv(feature_path, rows)
    report = build_capture_report(
        capture_root=pathlib.Path(args.capture_root), raw_path=raw_path,
        feature_path=feature_path, rows=rows, raw_count=count,
        capture_started_epoch_ms=started, capture_completed_epoch_ms=completed,
        public_url=args.public_url,
    )
    report_path = pathlib.Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "raw_messages": count, "event_buckets": len(rows), "liquidations": report["quality"]["liquidation_event_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
