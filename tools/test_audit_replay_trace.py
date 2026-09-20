#!/usr/bin/env python3
import pathlib
import tempfile
import unittest

import audit_replay_trace as audit


def log(state="sent", order="o1", fill="f1"):
    values = {key: "0" for key in audit.FILL_FIELDS}
    values.update(fill_id=fill, client_order_id=order, symbol="BTCUSDT",
                  direction="1", qty="1", price="100", fee="0.05",
                  liquidity="taker", order_state_before=state,
                  order_state_after="filled", account_already_reflected="false")
    return "[time] FILL_APPLIED: " + ", ".join(
        f"{key}={value}" for key, value in sorted(values.items())) + (
        "\nREPLAY_TERMINAL_SETTLEMENT_DONE: position_count=0, "
        "realized_net_usd=-0.05, fees_usd=0.05, funding_paid_usd=0\n")


class ReplayTraceTest(unittest.TestCase):
    def test_generated_ids_and_timestamps_only_are_normalized(self):
        self.assertEqual(audit.normalize(log()),
                         audit.normalize(log(order="other", fill="other").replace("[time]", "[later]")))

    def test_state_difference_fails_full_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [pathlib.Path(directory) / str(i) for i in range(2)]
            paths[0].write_text(log("new"))
            paths[1].write_text(log("sent"))
            self.assertEqual(audit.audit(paths)["status"], "FAIL")

    def test_all_emitted_fields_remain_compared(self):
        for old, new in (("fee=0.05", "fee=0.06"), ("qty=1", "qty=2"),
                         ("avg_entry_price_before=0", "avg_entry_price_before=1")):
            self.assertNotEqual(audit.normalize(log()), audit.normalize(log().replace(old, new)))

    def test_identity_relationships_are_preserved(self):
        first = log().splitlines()[0]
        terminal = log().splitlines()[1]
        same_order = first + "\n" + first.replace("fill_id=f1", "fill_id=f2") + "\n" + terminal
        other_order = same_order.replace("client_order_id=o1", "client_order_id=o2", 1)
        self.assertNotEqual(audit.normalize(same_order), audit.normalize(other_order))

    def test_incomplete_unknown_and_nonfinite_fields_fail(self):
        for malformed in (log().replace("order_state_before=sent, ", ""),
                          log().replace("fee=0.05", "fee=nan"),
                          log().replace("fee=0.05", "fee=0.05, extra=1"),
                          log().replace("fee=0.05", "fee=0.05, fee=0.05")):
            with self.assertRaises(ValueError):
                audit.normalize(malformed)

    def test_missing_or_failed_or_nonflat_terminal_fails(self):
        for malformed in ("", log().splitlines()[0], log() + log(),
                          "\n".join(reversed(log().splitlines())),
                          log().replace("position_count=0", "position_count=1"),
                          log() + "REPLAY_TERMINAL_SETTLEMENT_FAILED: reason=timeout"):
            with self.assertRaises(ValueError):
                audit.normalize(malformed)

    def test_reusing_one_log_is_not_two_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "run.log"
            path.write_text(log())
            with self.assertRaises(ValueError):
                audit.audit([path])
            with self.assertRaises(ValueError):
                audit.audit([path, path])


if __name__ == "__main__":
    unittest.main()
