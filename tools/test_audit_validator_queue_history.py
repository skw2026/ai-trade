"""Synthetic input-audit checks; no market data or network."""
import copy
import unittest

from audit_validator_queue_history import compare_pre_pectra, summarize, validate_rows


def row(day="2024-01-01", entry=1, exit_queue=2):
    return {"date": day, "entry_queue": entry, "exit_queue": exit_queue}


class QueueHistoryAuditTest(unittest.TestCase):
    def test_gap_is_reported_not_filled(self):
        rows = [row(), row("2024-01-03")]
        before = copy.deepcopy(rows)
        self.assertEqual(summarize(rows)["missing_dates"], ["2024-01-02"])
        self.assertEqual(rows, before)

    def test_factor_32_does_not_mutate_snapshots(self):
        old, current = [row()], [row(entry=32, exit_queue=64)]
        before = copy.deepcopy((old, current))
        result = compare_pre_pectra(current, old)
        self.assertEqual(result["residual_differences_after_factor_32"], [])
        self.assertEqual(result["queue_raw_change_counts"], {"entry_queue": 1, "exit_queue": 1})
        self.assertEqual((old, current), before)

    def test_zero_is_not_divided(self):
        result = compare_pre_pectra([row(entry=0, exit_queue=0)], [row(entry=0, exit_queue=0)])
        self.assertEqual(result["residual_differences_after_factor_32"], [])

    def test_nonfactor_change_and_added_fields_are_reported(self):
        current = row(entry=31, exit_queue=64)
        current["extra"] = 1
        fields = [item["field"] for item in compare_pre_pectra([current], [row()])[
            "residual_differences_after_factor_32"]]
        self.assertEqual(fields, ["entry_queue", "extra"])

    def test_missing_current_day_not_silently_dropped(self):
        self.assertEqual(compare_pre_pectra([row("2024-01-02")], [row()])[
            "missing_in_current"], ["2024-01-01"])

    def test_post_pectra_factor_rejected(self):
        with self.assertRaises(ValueError):
            compare_pre_pectra([row("2025-05-07")], [row("2025-05-07")])

    def test_duplicate_and_unsorted_dates_rejected(self):
        for rows in ([row(), row()], [row("2024-01-02"), row()]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                validate_rows(rows)

    def test_invalid_values_rejected(self):
        for value in (None, True, -1, float("nan"), float("inf"), "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_rows([row(entry=value)])

    def test_inaccurate_interval_includes_both_endpoints(self):
        rows = [row("2025-05-06"), row("2025-05-07"), row("2025-05-21"), row("2025-05-22")]
        self.assertEqual(summarize(rows)["known_inaccurate_post_pectra_dates"],
                         ["2025-05-07", "2025-05-21"])


if __name__ == "__main__":
    unittest.main()
