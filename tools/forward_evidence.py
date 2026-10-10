#!/usr/bin/env python3
"""Bounded, credential-free receive-time evidence. No strategy or order API."""
import argparse
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import time

from collect_bybit_microstructure import OrderBook

SCHEMA = "forward_receive_evidence_v1"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
TOPICS = tuple(t for s in SYMBOLS for t in ("orderbook.50." + s, "publicTrade." + s))
URL = "wss://stream.bybit.com/v5/public/linear"
MAX_RAW_BYTES = 128 * 1024 * 1024
MAX_BATCH_BYTES = 2 * 1024**3
MIN_FREE_BYTES = 8 * 1024**3


def require(condition, code):
    if not condition:
        raise ValueError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, value, *, replace=False):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp") if replace else path
    require(not temporary.is_symlink() and not path.is_symlink(), "SYMLINK_OUTPUT")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if replace:
        temporary.replace(path)
    fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Replay:
    def __init__(self):
        self.count = 0
        self.first_wall = self.last_wall = self.first_mono = self.last_mono = 0
        self.max_gap = 0
        self.topics = {topic: 0 for topic in TOPICS}
        self.books = {symbol: OrderBook() for symbol in SYMBOLS}
        self.hash = hashlib.sha256()

    def accept(self, row):
        require(isinstance(row, dict) and set(row) == {
            "sequence", "receive_utc_ns", "receive_monotonic_ns", "wire"}, "ENVELOPE_FIELDS")
        require(type(row["sequence"]) is int and row["sequence"] == self.count + 1, "LOCAL_SEQUENCE")
        wall, mono = row["receive_utc_ns"], row["receive_monotonic_ns"]
        require(type(wall) is int and type(mono) is int and wall > 0 and mono > 0, "RECEIVE_CLOCK_TYPE")
        require(isinstance(row["wire"], str), "WIRE_TYPE")
        if self.count:
            require(mono > self.last_mono and wall >= self.last_wall, "RECEIVE_CLOCK_REVERSED")
            self.max_gap = max(self.max_gap, mono - self.last_mono)
            require(abs((wall - self.first_wall) - (mono - self.first_mono)) <= 2_000_000_000,
                    "RECEIVE_CLOCK_DISCONTINUITY")
        else:
            self.first_wall, self.first_mono = wall, mono
        payload = json.loads(row["wire"])
        require(isinstance(payload, dict) and payload.get("topic") in TOPICS, "UNKNOWN_TOPIC")
        topic = payload["topic"]
        symbol = topic.rsplit(".", 1)[1]
        self.topics[topic] += 1
        projection = {"sequence": row["sequence"], "topic": topic}
        if topic.startswith("orderbook."):
            require(isinstance(payload.get("data"), dict) and payload["data"].get("s") == symbol,
                    "BOOK_SYMBOL")
            self.books[symbol].apply(payload)
            metrics = self.books[symbol].metrics()
            require(metrics["best_bid"] > 0 and metrics["best_ask"] > metrics["best_bid"], "INVALID_QUOTE")
            projection["book"] = metrics
        else:
            trades = payload.get("data")
            require(isinstance(trades, list) and trades and all(
                isinstance(trade, dict) and trade.get("s") == symbol
                and type(trade.get("T")) is int for trade in trades), "TRADE_SHAPE")
            projection["trades"] = trades
        self.hash.update((canonical(projection) + "\n").encode())
        self.count += 1
        self.last_wall, self.last_mono = wall, mono

    def summary(self):
        require(self.count > 0 and all(self.topics.values()), "INCOMPLETE_CHANNEL_COVERAGE")
        require(self.max_gap <= 15_000_000_000, "RECEIVE_GAP_OVER_15_SECONDS")
        return {"messages": self.count, "topic_counts": self.topics,
                "first_receive_epoch_ms": self.first_wall // 1_000_000,
                "last_receive_epoch_ms": self.last_wall // 1_000_000,
                "elapsed_monotonic_ms": (self.last_mono - self.first_mono) // 1_000_000,
                "max_intermessage_gap_ms": self.max_gap / 1_000_000,
                "replay_sha256": self.hash.hexdigest()}


def replay(path):
    state, size = Replay(), 0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            size += len(line.encode())
            require(size <= MAX_RAW_BYTES, "SEGMENT_SIZE_LIMIT")
            state.accept(json.loads(line))
    return state.summary()


async def capture(path, duration, connector=None):
    if connector is None:
        import websockets
        connector = websockets.connect
    state, size = Replay(), 0
    started = time.monotonic()
    deadline = started + duration
    with gzip.open(path, "xt", encoding="utf-8") as handle:
        async with connector(URL, ssl=ssl.create_default_context(), open_timeout=15,
                             ping_interval=20, ping_timeout=20, close_timeout=5,
                             max_size=8 * 1024 * 1024) as socket:
            await socket.send(canonical({"op": "subscribe", "args": list(TOPICS)}))
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    wire = await asyncio.wait_for(socket.recv(), min(15.0, remaining))
                except asyncio.TimeoutError:
                    if remaining > 15.0:
                        raise ValueError("STREAM_IDLE_TIMEOUT") from None
                    break
                wall, mono = time.time_ns(), time.monotonic_ns()
                require(isinstance(wire, str), "NON_TEXT_FRAME")
                payload = json.loads(wire)
                require(isinstance(payload, dict), "NON_OBJECT_FRAME")
                require(payload.get("success") is not False, "SUBSCRIPTION_REJECTED")
                if not payload.get("topic"):
                    continue
                row = {"sequence": state.count + 1, "receive_utc_ns": wall,
                       "receive_monotonic_ns": mono, "wire": wire}
                line = canonical(row) + "\n"
                size += len(line.encode())
                require(size <= MAX_RAW_BYTES, "SEGMENT_SIZE_LIMIT")
                # Persist before processing; preserve the rejected observation for diagnosis.
                handle.write(line)
                state.accept(row)
                if state.count % 1000 == 0:
                    require(shutil.disk_usage(path.parent).free >= MIN_FREE_BYTES, "DISK_RESERVE")
    with Path(path).open("rb") as handle:
        os.fsync(handle.fileno())
    result = state.summary()
    require(result["elapsed_monotonic_ms"] >= (duration - 20) * 1000, "SHORT_CAPTURE")
    return result


def verify_segment(directory):
    directory = Path(directory)
    require(not directory.is_symlink(), "SYMLINK_SEGMENT")
    receipt = json.loads((directory / "receipt.json").read_text())
    raw = directory / "receive.jsonl.gz"
    require(not raw.is_symlink() and raw.is_file(), "RAW_FILE_REQUIRED")
    require(receipt.get("schema_version") == SCHEMA and receipt.get("status") == "SEALED", "UNSEALED_SEGMENT")
    require(digest(raw) == receipt["raw_sha256"], "RAW_HASH_MISMATCH")
    reconstructed = replay(raw)
    require(reconstructed == receipt["observation"], "REPLAY_MISMATCH")
    return receipt


def run(root, *, segments=97):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "OUTPUT_DIRECTORY_REQUIRED")
    require(not (root / "started.json").exists(), "BATCH_ALREADY_STARTED")
    require(1 <= segments <= 97, "SEGMENT_LIMIT")
    write_json(root / "started.json", {"schema_version": SCHEMA, "started_epoch_ms": time.time_ns() // 1_000_000,
               "maximum_seconds": 86400, "maximum_bytes": MAX_BATCH_BYTES, "maximum_segments": segments,
               "source_sha256": digest(__file__), "book_source_sha256": digest(Path(__file__).with_name("collect_bybit_microstructure.py")),
               "research_domain": "prospective_development", "economic_evidence": False,
               "account_access": False, "order_submission": False})
    deadline = time.monotonic() + 86400
    completed, total = 0, 0
    try:
        for index in range(segments):
            duration = 65 if index == 0 else 900
            if time.monotonic() + duration > deadline or total + MAX_RAW_BYTES > MAX_BATCH_BYTES:
                break
            require(shutil.disk_usage(root).free >= MIN_FREE_BYTES, "DISK_RESERVE")
            directory = root / ("segment-%03d" % index)
            directory.mkdir(mode=0o700)
            raw = directory / "receive.partial.jsonl.gz"
            write_json(root / "health.json", {"status": "CAPTURING", "completed_segments": completed,
                       "active_segment": index, "updated_epoch_ms": time.time_ns() // 1_000_000}, replace=True)
            observed = asyncio.run(capture(raw, duration))
            require(replay(raw) == observed, "LIVE_REPLAY_MISMATCH")
            final = directory / "receive.jsonl.gz"
            raw.rename(final)
            receipt = {"schema_version": SCHEMA, "status": "SEALED", "segment_index": index,
                       "raw_sha256": digest(final), "raw_bytes": final.stat().st_size,
                       "observation": observed, "previous_receipt_sha256":
                       digest(root / ("segment-%03d" % (index - 1)) / "receipt.json") if index else None,
                       "source_sha256": digest(__file__), "economic_evidence": False}
            write_json(directory / "receipt.json", receipt)
            require(verify_segment(directory) == receipt, "SEALED_REPLAY_MISMATCH")
            completed += 1
            total += final.stat().st_size
            write_json(root / "health.json", {"status": "SEGMENT_SEALED", "completed_segments": completed,
                       "raw_bytes": total, "last_receipt_sha256": digest(directory / "receipt.json"),
                       "last_observation": observed, "updated_epoch_ms": time.time_ns() // 1_000_000}, replace=True)
        write_json(root / "health.json", {"status": "BOUNDED_COLLECTION_COMPLETE", "completed_segments": completed,
                   "raw_bytes": total, "updated_epoch_ms": time.time_ns() // 1_000_000}, replace=True)
        return 0
    except Exception as exc:
        # Keep raw/partial files. No automatic reconnect, restart, cleanup or retry.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).isupper() else type(exc).__name__
        failure = {"status": "STOPPED_ERROR", "reason_code": reason,
                   "completed_segments": completed, "updated_epoch_ms": time.time_ns() // 1_000_000}
        write_json(root / "failure.json", failure)
        write_json(root / "health.json", failure, replace=True)
        return 2


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "verify"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--segments", type=int, default=97)
    args = parser.parse_args()
    if args.mode == "run":
        return run(args.root, segments=args.segments)
    receipt = verify_segment(args.root)
    print(canonical(receipt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
