#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("build_trade_ledger.py")
    spec = importlib.util.spec_from_file_location("build_trade_ledger", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LEDGER = load_module()


def fill_line(
    timestamp: str,
    fill_id: str,
    direction: int,
    qty: float,
    price: float,
    fee: float,
    liquidity: str = "MAKER",
    local_qty_before: float = 0.0,
    local_qty_after: float | None = None,
    avg_entry_price_before: float = 0.0,
) -> str:
    if local_qty_after is None:
        local_qty_after = local_qty_before + direction * qty
    return (
        f"{timestamp} [INFO] FILL_APPLIED: fill_id={fill_id}, "
        f"client_order_id=order-{fill_id}, symbol=SOLUSDT, "
        f"direction={direction}, qty={qty}, price={price}, fee={fee}, "
        f"liquidity={liquidity}, order_state_before=sent, "
        f"local_qty_before={local_qty_before}, "
        f"avg_entry_price_before={avg_entry_price_before}, "
        f"local_qty_after={local_qty_after}"
    )


class BuildTradeLedgerTest(unittest.TestCase):
    def test_deduplicates_fill_id_and_reconstructs_net_pnl(self):
        lines = [
            fill_line("2026-06-02 14:22:18", "open", 1, 2.0, 100.0, 0.04),
            fill_line("2026-06-02 14:22:20", "open", 1, 2.0, 100.0, 0.04),
            fill_line(
                "2026-06-02 14:23:18",
                "close",
                -1,
                2.0,
                101.0,
                0.04,
                local_qty_before=2.0,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            report = LEDGER.build_report([path], run_id="run-1")

        self.assertEqual(report["schema_version"], "trade_ledger_v1")
        self.assertEqual(report["quality"]["source_fill_lines"], 3)
        self.assertEqual(report["quality"]["unique_fill_count"], 2)
        self.assertEqual(report["quality"]["duplicate_fill_count"], 1)
        self.assertEqual(report["summary"]["closed_lot_count"], 1)
        self.assertAlmostEqual(report["summary"]["realized_gross_pnl_usd"], 2.0)
        self.assertAlmostEqual(report["summary"]["realized_net_pnl_usd"], 1.92)
        self.assertFalse(report["accounting_scope"]["complete_net_pnl"])
        self.assertEqual(
            report["accounting_scope"]["funding"],
            "not_available_in_fill_events",
        )
        self.assertEqual(report["open_positions"], {})

    def test_reversal_closes_old_side_and_opens_new_side(self):
        lines = [
            fill_line("2026-06-02 14:22:18", "long", 1, 1.0, 100.0, 0.02),
            fill_line(
                "2026-06-02 14:23:18",
                "reverse",
                -1,
                2.0,
                99.0,
                0.04,
                local_qty_before=1.0,
                local_qty_after=-1.0,
                avg_entry_price_before=100.0,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            report = LEDGER.build_report([path])

        self.assertEqual(report["summary"]["closed_lot_count"], 1)
        self.assertAlmostEqual(report["summary"]["realized_net_pnl_usd"], -1.04)
        self.assertAlmostEqual(report["open_positions"]["SOLUSDT"]["qty"], -1.0)
        self.assertAlmostEqual(
            report["open_positions"]["SOLUSDT"]["unallocated_entry_fee_usd"],
            0.02,
        )

    def test_negative_maker_fee_is_preserved_as_rebate(self):
        lines = [
            fill_line("2026-06-02 14:22:18", "open", 1, 1.0, 100.0, -0.02),
            fill_line(
                "2026-06-02 14:23:18",
                "close",
                -1,
                1.0,
                101.0,
                -0.01,
                local_qty_before=1.0,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            report = LEDGER.build_report([path])

        self.assertAlmostEqual(report["summary"]["fee_usd"], -0.03)
        self.assertAlmostEqual(
            report["summary"]["realized_trade_pnl_ex_funding_usd"],
            1.03,
        )
        self.assertEqual(
            report["accounting_scope"]["fee_sign_convention"],
            "positive_cost_negative_rebate",
        )

    def test_conflicting_duplicate_is_reported(self):
        lines = [
            fill_line("2026-06-02 14:22:18", "same", 1, 1.0, 100.0, 0.02),
            fill_line("2026-06-02 14:22:19", "same", 1, 1.0, 101.0, 0.02),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            report = LEDGER.build_report([path])

        self.assertEqual(report["quality"]["conflicting_duplicate_count"], 1)

    def test_malformed_fill_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text(
                "2026-06-02 14:22:18 [INFO] FILL_APPLIED: "
                "fill_id=bad, symbol=SOLUSDT, direction=1, qty=oops, "
                "price=100, fee=0.02",
                encoding="utf-8",
            )
            report = LEDGER.build_report([path])

        self.assertEqual(report["quality"]["source_fill_lines"], 1)
        self.assertEqual(report["quality"]["unique_fill_count"], 0)
        self.assertEqual(report["quality"]["malformed_fill_count"], 1)

    def test_same_second_fills_keep_source_event_order(self):
        lines = [
            fill_line("2026-06-02 14:22:18", "z-open", 1, 1.0, 100.0, 0.0),
            fill_line(
                "2026-06-02 14:22:18",
                "a-close",
                -1,
                1.0,
                101.0,
                0.0,
                local_qty_before=1.0,
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text("\n".join(lines), encoding="utf-8")
            report = LEDGER.build_report([path])
        self.assertEqual([fill["fill_id"] for fill in report["fills"]], ["z-open", "a-close"])
        self.assertAlmostEqual(report["summary"]["realized_gross_pnl_usd"], 1.0)

    def test_missing_timestamp_is_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text(fill_line("", "bad-time", 1, 1.0, 100.0, 0.0), encoding="utf-8")
            report = LEDGER.build_report([path])
        self.assertEqual(report["quality"]["malformed_fill_count"], 1)

    def test_inherited_position_without_entry_price_is_unverifiable(self):
        line = fill_line(
            "2026-06-02 14:22:18",
            "close-existing",
            -1,
            1.0,
            101.0,
            0.01,
            local_qty_before=1.0,
            local_qty_after=0.0,
            avg_entry_price_before=0.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text(line, encoding="utf-8")
            report = LEDGER.build_report([path])
        self.assertFalse(report["quality"]["initial_position_state_verifiable"])
        self.assertFalse(report["accounting_scope"]["realized_pnl_verifiable"])

    def test_inherited_position_with_price_still_has_unverifiable_net_fee(self):
        line = fill_line(
            "2026-06-02 14:22:18",
            "close-existing",
            -1,
            1.0,
            101.0,
            0.01,
            local_qty_before=1.0,
            local_qty_after=0.0,
            avg_entry_price_before=100.0,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "runtime.log"
            path.write_text(line, encoding="utf-8")
            report = LEDGER.build_report([path])

        self.assertTrue(report["quality"]["initial_position_state_verifiable"])
        self.assertFalse(report["quality"]["pre_window_entry_fees_verifiable"])
        self.assertTrue(report["accounting_scope"]["realized_pnl_verifiable"])
        self.assertFalse(
            report["accounting_scope"]["realized_trade_net_pnl_verifiable"]
        )


if __name__ == "__main__":
    unittest.main()
