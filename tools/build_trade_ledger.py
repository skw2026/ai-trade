#!/usr/bin/env python3
"""Build a canonical, fill-id deduplicated trade ledger from runtime logs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


FILL_MARKER = "FILL_APPLIED:"
TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
FIELD_RE = re.compile(r"(?:^|,\s*)([A-Za-z0-9_]+)=([^,}]*)")
EPSILON = 1e-12


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_fields(line: str) -> dict[str, str]:
    payload = line.split(FILL_MARKER, 1)[1].strip()
    return {
        match.group(1): match.group(2).strip()
        for match in FIELD_RE.finditer(payload)
    }


def parse_float(fields: dict[str, str], name: str) -> float:
    try:
        value = float(fields.get(name, ""))
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
    if not math.isfinite(value):
        raise ValueError(f"non-finite {name}")
    return value


def parse_timestamp(line: str) -> str:
    match = TIMESTAMP_RE.search(line)
    if not match:
        raise ValueError("missing timestamp")
    parsed = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_optional_float(fields: dict[str, str], name: str) -> float | None:
    raw = fields.get(name, "").strip()
    if not raw:
        return None
    return parse_float(fields, name)


def canonical_fill(
    fields: dict[str, str],
    line: str,
    source: Path,
    source_index: int,
    source_line: int,
) -> dict[str, Any]:
    fill_id = fields.get("fill_id", "").strip()
    symbol = fields.get("symbol", "").strip().upper()
    client_order_id = fields.get("client_order_id", "").strip()
    direction = int(parse_float(fields, "direction"))
    qty = parse_float(fields, "qty")
    price = parse_float(fields, "price")
    # Positive fee is a cost; negative fee is a maker rebate.
    fee = parse_float(fields, "fee")
    if not fill_id:
        raise ValueError("missing fill_id")
    if not symbol:
        raise ValueError("missing symbol")
    if direction not in {-1, 1}:
        raise ValueError("direction must be -1 or 1")
    if qty <= 0.0 or price <= 0.0:
        raise ValueError("qty and price must be positive")
    liquidity = fields.get("liquidity", "UNKNOWN").strip().upper() or "UNKNOWN"
    return {
        "fill_id": fill_id,
        "client_order_id": client_order_id,
        "timestamp_utc": parse_timestamp(line),
        "symbol": symbol,
        "direction": direction,
        "qty": qty,
        "price": price,
        "fee_usd": fee,
        "liquidity": liquidity,
        "source_log": str(source),
        "source_index": source_index,
        "source_line": source_line,
        "local_qty_before": parse_optional_float(fields, "local_qty_before"),
        "avg_entry_price_before": parse_optional_float(
            fields, "avg_entry_price_before"
        ),
        "local_qty_after": parse_optional_float(fields, "local_qty_after"),
    }


def fill_fingerprint(fill: dict[str, Any]) -> tuple[Any, ...]:
    return (
        fill["client_order_id"],
        fill["symbol"],
        fill["direction"],
        fill["qty"],
        fill["price"],
        fill["fee_usd"],
        fill["liquidity"],
    )


def read_fills(paths: Iterable[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fills_by_id: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    conflict_count = 0
    malformed_count = 0
    source_lines = 0
    sources: list[dict[str, Any]] = []

    for source_index, path in enumerate(paths):
        if not path.is_file():
            raise FileNotFoundError(path)
        sources.append(
            {
                "path": str(path),
                "sha256": file_sha256(path),
                "size_bytes": path.stat().st_size,
            }
        )
        for source_line, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            if FILL_MARKER not in line:
                continue
            source_lines += 1
            try:
                fill = canonical_fill(
                    parse_fields(line),
                    line,
                    path,
                    source_index,
                    source_line,
                )
            except (TypeError, ValueError):
                malformed_count += 1
                continue
            existing = fills_by_id.get(fill["fill_id"])
            if existing is None:
                fills_by_id[fill["fill_id"]] = fill
                continue
            duplicate_count += 1
            if fill_fingerprint(existing) != fill_fingerprint(fill):
                conflict_count += 1

    fills = list(fills_by_id.values())
    quality = {
        "source_fill_lines": source_lines,
        "unique_fill_count": len(fills),
        "duplicate_fill_count": duplicate_count,
        "conflicting_duplicate_count": conflict_count,
        "malformed_fill_count": malformed_count,
        "sources": sources,
    }
    return fills, quality


@dataclass
class PositionState:
    qty: float = 0.0
    avg_price: float = 0.0
    entry_fee_usd: float = 0.0
    opened_at_utc: str = ""
    opening_fill_id: str = ""


def build_closed_lots(
    fills: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, PositionState], dict[str, Any]]:
    states: dict[str, PositionState] = {}
    closed_lots: list[dict[str, Any]] = []
    first_fill_seen: set[str] = set()
    unresolved_symbols: set[str] = set()
    inherited_position_symbols: set[str] = set()
    reconciliation_mismatches: list[dict[str, Any]] = []

    for fill in fills:
        symbol = str(fill["symbol"])
        state = states.setdefault(symbol, PositionState())
        if symbol not in first_fill_seen:
            first_fill_seen.add(symbol)
            initial_qty = fill.get("local_qty_before")
            initial_avg_price = fill.get("avg_entry_price_before")
            if initial_qty is None:
                unresolved_symbols.add(symbol)
            elif abs(float(initial_qty)) > EPSILON:
                if initial_avg_price is None or float(initial_avg_price) <= 0.0:
                    unresolved_symbols.add(symbol)
                else:
                    inherited_position_symbols.add(symbol)
                    state.qty = float(initial_qty)
                    state.avg_price = float(initial_avg_price)
                    state.opened_at_utc = "before_evaluation_window"
                    state.opening_fill_id = "pre_window_position"

        if symbol in unresolved_symbols:
            continue

        logged_before = fill.get("local_qty_before")
        if logged_before is not None and abs(float(logged_before) - state.qty) > 1e-8:
            reconciliation_mismatches.append(
                {
                    "fill_id": fill["fill_id"],
                    "symbol": symbol,
                    "phase": "before",
                    "ledger_qty": rounded(state.qty),
                    "logged_qty": rounded(float(logged_before)),
                }
            )
        signed_qty = float(fill["direction"]) * float(fill["qty"])
        fill_qty_remaining = abs(signed_qty)
        fill_fee_remaining = float(fill["fee_usd"])

        same_side = (
            abs(state.qty) <= EPSILON
            or state.qty * signed_qty > 0.0
        )
        if same_side:
            previous_abs_qty = abs(state.qty)
            new_abs_qty = previous_abs_qty + fill_qty_remaining
            state.avg_price = (
                previous_abs_qty * state.avg_price
                + fill_qty_remaining * float(fill["price"])
            ) / new_abs_qty
            state.qty += signed_qty
            state.entry_fee_usd += fill_fee_remaining
            if previous_abs_qty <= EPSILON:
                state.opened_at_utc = str(fill["timestamp_utc"])
                state.opening_fill_id = str(fill["fill_id"])
            logged_after = fill.get("local_qty_after")
            if (
                logged_after is not None
                and abs(float(logged_after) - state.qty) > 1e-8
            ):
                reconciliation_mismatches.append(
                    {
                        "fill_id": fill["fill_id"],
                        "symbol": symbol,
                        "phase": "after",
                        "ledger_qty": rounded(state.qty),
                        "logged_qty": rounded(float(logged_after)),
                    }
                )
            continue

        position_abs_before = abs(state.qty)
        close_qty = min(position_abs_before, fill_qty_remaining)
        close_fraction_of_fill = close_qty / fill_qty_remaining
        exit_fee = fill_fee_remaining * close_fraction_of_fill
        entry_fee = state.entry_fee_usd * (close_qty / position_abs_before)
        side = 1 if state.qty > 0.0 else -1
        gross_pnl = close_qty * (float(fill["price"]) - state.avg_price) * side
        lot_key = (
            f"{symbol}|{state.opening_fill_id}|{fill['fill_id']}|"
            f"{len(closed_lots)}"
        )
        closed_lots.append(
            {
                "lot_id": hashlib.sha256(lot_key.encode("utf-8")).hexdigest()[:24],
                "symbol": symbol,
                "side": "LONG" if side > 0 else "SHORT",
                "opened_at_utc": state.opened_at_utc,
                "closed_at_utc": fill["timestamp_utc"],
                "opening_fill_id": state.opening_fill_id,
                "closing_fill_id": fill["fill_id"],
                "qty": close_qty,
                "entry_price": state.avg_price,
                "exit_price": fill["price"],
                "entry_fee_usd": entry_fee,
                "exit_fee_usd": exit_fee,
                "gross_pnl_usd": gross_pnl,
                "net_pnl_usd": gross_pnl - entry_fee - exit_fee,
                "exit_liquidity": fill["liquidity"],
            }
        )

        state.entry_fee_usd -= entry_fee
        fill_fee_remaining -= exit_fee
        fill_qty_remaining -= close_qty
        state.qty += side * -close_qty
        if abs(state.qty) <= EPSILON:
            state.qty = 0.0
            state.avg_price = 0.0
            state.entry_fee_usd = 0.0
            state.opened_at_utc = ""
            state.opening_fill_id = ""

        if fill_qty_remaining > EPSILON:
            state.qty = float(fill["direction"]) * fill_qty_remaining
            state.avg_price = float(fill["price"])
            state.entry_fee_usd = fill_fee_remaining
            state.opened_at_utc = str(fill["timestamp_utc"])
            state.opening_fill_id = str(fill["fill_id"])

        logged_after = fill.get("local_qty_after")
        if logged_after is not None and abs(float(logged_after) - state.qty) > 1e-8:
            reconciliation_mismatches.append(
                {
                    "fill_id": fill["fill_id"],
                    "symbol": symbol,
                    "phase": "after",
                    "ledger_qty": rounded(state.qty),
                    "logged_qty": rounded(float(logged_after)),
                }
            )

    verification = {
        "initial_position_state_verifiable": not unresolved_symbols,
        "unresolved_initial_position_symbols": sorted(unresolved_symbols),
        "inherited_position_symbols": sorted(inherited_position_symbols),
        "pre_window_entry_fees_verifiable": not inherited_position_symbols,
        "position_reconciliation_mismatch_count": len(reconciliation_mismatches),
        "position_reconciliation_mismatches": reconciliation_mismatches,
        "event_order": "source_file_argument_order_then_source_line",
    }
    return closed_lots, states, verification


def rounded(value: float) -> float:
    return round(float(value), 12)


def build_report(paths: list[Path], run_id: str = "") -> dict[str, Any]:
    fills, quality = read_fills(paths)
    closed_lots, states, verification = build_closed_lots(fills)
    quality.update(verification)
    gross = sum(float(item["gross_pnl_usd"]) for item in closed_lots)
    fees = sum(float(fill["fee_usd"]) for fill in fills)
    closed_fees = sum(
        float(item["entry_fee_usd"]) + float(item["exit_fee_usd"])
        for item in closed_lots
    )
    net = sum(float(item["net_pnl_usd"]) for item in closed_lots)
    maker_count = sum(fill["liquidity"] == "MAKER" for fill in fills)
    positive_lots = sum(float(item["net_pnl_usd"]) > 0.0 for item in closed_lots)
    return {
        "schema_version": "trade_ledger_v1",
        "run_id": run_id,
        "generated_at_utc": now_utc_iso(),
        "dedupe_key": "fill_id",
        "accounting_scope": {
            "realized_pnl_source": "fill_price_position_reconstruction",
            "fees": "included_from_fill_events",
            "fee_sign_convention": "positive_cost_negative_rebate",
            "slippage": "implicit_in_fill_price_without_arrival_price_attribution",
            "funding": "not_available_in_fill_events",
            "complete_net_pnl": False,
            "realized_pnl_verifiable": bool(
                verification["initial_position_state_verifiable"]
                and verification["position_reconciliation_mismatch_count"] == 0
            ),
            "realized_trade_net_pnl_verifiable": bool(
                verification["initial_position_state_verifiable"]
                and verification["pre_window_entry_fees_verifiable"]
                and verification["position_reconciliation_mismatch_count"] == 0
            ),
            "pre_window_entry_fees": "unavailable_for_inherited_positions",
        },
        "quality": quality,
        "summary": {
            "fill_count": len(fills),
            "closed_lot_count": len(closed_lots),
            "maker_fill_count": maker_count,
            "taker_fill_count": sum(fill["liquidity"] == "TAKER" for fill in fills),
            "unknown_liquidity_fill_count": len(fills) - maker_count
            - sum(fill["liquidity"] == "TAKER" for fill in fills),
            "fee_usd": rounded(fees),
            "closed_fee_usd": rounded(closed_fees),
            "realized_gross_pnl_usd": rounded(gross),
            "realized_trade_pnl_ex_funding_usd": rounded(net),
            "realized_net_pnl_usd": rounded(net),
            "positive_closed_lot_ratio": (
                rounded(positive_lots / len(closed_lots)) if closed_lots else None
            ),
        },
        "open_positions": {
            symbol: {
                "qty": rounded(state.qty),
                "avg_price": rounded(state.avg_price),
                "unallocated_entry_fee_usd": rounded(state.entry_fee_usd),
                "opened_at_utc": state.opened_at_utc,
                "opening_fill_id": state.opening_fill_id,
            }
            for symbol, state in sorted(states.items())
            if abs(state.qty) > EPSILON
        },
        "fills": fills,
        "closed_lots": closed_lots,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a canonical trade ledger from FILL_APPLIED log events."
    )
    parser.add_argument("--log", action="append", required=True, help="Runtime log path")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--run-id", default="", help="Closed-loop run identifier")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = build_report([Path(item) for item in args.log], run_id=args.run_id)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "TRADE_LEDGER: "
        f"fills={report['summary']['fill_count']}, "
        f"closed_lots={report['summary']['closed_lot_count']}, "
        f"net_usd={report['summary']['realized_net_pnl_usd']}, "
        f"output={output}"
    )
    quality = report["quality"]
    return 1 if (
        quality["conflicting_duplicate_count"] > 0
        or quality["malformed_fill_count"] > 0
        or not quality["initial_position_state_verifiable"]
        or quality["position_reconciliation_mismatch_count"] > 0
    ) else 0


if __name__ == "__main__":
    raise SystemExit(main())
