"""Exclusive-batch orchestration checks with synthetic I/O, never market data."""
import contextlib
import datetime as dt
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import run_open_market_selective as runner


class SelectiveRunnerTests(unittest.TestCase):
    def invoke(self, calibration_status, *, bad_audit=False, duplicate_attempt=False, expired=False):
        import open_market_selective as selective
        import audit_open_market_selective as independent
        import audit_open_market_learning as input_audit
        import open_market_execution as execution
        import audit_open_market_execution as ledger
        import open_market_selective_verdict as verdict
        calls, saved = [], {}
        stamp = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)
        calibration = dict(status=calibration_status, fit_count=0 if
            calibration_status == 'CALIBRATION_CAPACITY_INSUFFICIENT' else 1)
        decision = dict(status='NET_OR_RISK_UNSUPPORTED', g4=False,
            qualified_candidates=0, independent_confirmation=False,
            online_learning_proven=False, demo_opening_lock_unchanged=True)

        def read(path):
            name = Path(path).name
            if name == 'freeze.json':
                return dict(files={})
            if name == 'scope.json':
                return dict(deadline_utc=(stamp+dt.timedelta(hours=-1 if expired else 1)).isoformat())
            if name == 'select-PV-0-model.json':
                return dict(arm='PV', ridge=.1, training_rows=166, label_cutoff=1625097300000)
            if name.endswith('features.json'):
                year = name[:4]
                left, right = (('2021-01-16', '2021-12-30') if year == '2021' else
                               ('2025-01-16', '2026-01-01'))
                return {str(t): {} for t in range(runner.data.millis(left), runner.data.millis(right), runner.data.DAY)}
            return []

        def save(path, value):
            key = Path(path).name
            if key in saved:
                raise FileExistsError(key)
            saved[key] = value

        def simulate(rows, fs, decisions, start, end, latency):
            calls.append((start, end, latency, decisions))
            b = {key: 0. for key in ('net', 'gross', 'fees', 'slippage', 'funding', 'drawdown',
                                     'close_drawdown', 'failed_entries', 'quantity')}
            b.update(stopped=None, insufficient=None, cycles=[], active_weeks=[], events=[])
            return dict(books=[dict(b), dict(b)])

        if duplicate_attempt:
            saved['economic-attempt.json'] = dict(original=True)
        with tempfile.TemporaryDirectory() as folder, contextlib.ExitStack() as stack:
            for name, replacement in (
                    ('ROOT', Path(folder)), ('active', lambda: None), ('preserve', lambda: 7),
                    ('now', lambda: stamp), ('read', read), ('sha', lambda p: 'synthetic'), ('save', save)):
                stack.enter_context(mock.patch.object(runner, name, replacement))
            stack.enter_context(mock.patch.object(input_audit, 'audit_fit', return_value={}))
            stack.enter_context(mock.patch.object(input_audit, 'audit_predictions', return_value={}))
            fit = stack.enter_context(mock.patch.object(selective, 'fit', return_value=calibration))
            audit = stack.enter_context(mock.patch.object(independent, 'audit_fit',
                side_effect=ValueError('synthetic audit rejection') if bad_audit else None, return_value={}))
            stack.enter_context(mock.patch.object(independent, 'audit_apply', return_value={}))
            stack.enter_context(mock.patch.object(runner.learning, 'predict',
                side_effect=lambda m, f, t: dict(t=t, direction=1)))
            stack.enter_context(mock.patch.object(selective, 'apply',
                side_effect=lambda m, ps: [dict(t=p['t'], direction=0) for p in ps]))
            stack.enter_context(mock.patch.object(runner.data, 'load_rows', return_value=[]))
            simulation = stack.enter_context(mock.patch.object(execution, 'simulate', side_effect=simulate))
            ledger_check = stack.enter_context(mock.patch.object(ledger, 'audit', return_value={}))
            stack.enter_context(mock.patch.object(verdict, 'classify', return_value=decision))
            stack.enter_context(mock.patch.object(verdict, 'audit_verdict', return_value={}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            error = None
            try:
                runner.execute()
            except (ValueError, FileExistsError) as exc:
                error = exc
            return saved, calls, error, (fit.call_count, audit.call_count, simulation.call_count, ledger_check.call_count)

    def test_capacity_exit_never_enters_account_evaluation(self):
        saved, calls, error, counts = self.invoke('CALIBRATION_CAPACITY_INSUFFICIENT')
        self.assertIsNone(error)
        self.assertEqual(calls, [])
        self.assertEqual(counts, (1, 1, 0, 0))
        self.assertEqual(saved['result.json']['calibration_fits'], 0)
        self.assertEqual(saved['result.json']['decision']['economic_status'], 'NOT_EVALUATED')

    def test_degenerate_fit_has_one_fit_and_zero_paths(self):
        saved, _, error, _ = self.invoke('NO_SELECTIVE_POLICY')
        self.assertIsNone(error)
        self.assertEqual(saved['result.json']['paired_paths'], 0)
        self.assertEqual(saved['result.json']['decimal_books'], 0)
        self.assertEqual(saved['result.json']['calibration_fits'], 1)

    def test_eligible_branch_runs_all_five_matching_paths_and_ten_books(self):
        saved, calls, error, counts = self.invoke('ELIGIBLE_SELECTIVE_POLICY')
        self.assertIsNone(error)
        self.assertEqual(counts, (1, 1, 5, 5))
        result = saved['result.json']
        self.assertEqual(result['paired_paths'], 5)
        self.assertEqual(result['decimal_books'], 10)
        self.assertEqual(set(result['paths']), {'2021-control', '2021-veto', '2025-control', '2025-veto', '2025-delay'})
        self.assertEqual([c[2] for c in calls], [1, 1, 1, 1, 2])
        self.assertEqual(calls[3][3], calls[4][3])
        self.assertEqual([len(c[3]) for c in calls], [89, 89, 350, 350, 350])
        self.assertTrue(all(v == 0 for v in calls[1][3].values()))
        self.assertFalse(result['decision']['g4'])

    def test_independent_fit_rejection_stops_before_any_path_or_result(self):
        saved, calls, error, counts = self.invoke('ELIGIBLE_SELECTIVE_POLICY', bad_audit=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(calls, [])
        self.assertNotIn('result.json', saved)
        self.assertEqual(counts, (1, 1, 0, 0))

    def test_duplicate_attempt_never_fits_or_overwrites_original(self):
        saved, _, error, counts = self.invoke('ELIGIBLE_SELECTIVE_POLICY', duplicate_attempt=True)
        self.assertIsInstance(error, FileExistsError)
        self.assertEqual(counts, (0, 0, 0, 0))
        self.assertEqual(saved['economic-attempt.json'], dict(original=True))

    def test_expired_budget_never_consumes_attempt_or_fits(self):
        saved, _, error, counts = self.invoke('ELIGIBLE_SELECTIVE_POLICY', expired=True)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(counts, (0, 0, 0, 0))
        self.assertNotIn('economic-attempt.json', saved)


if __name__ == '__main__':
    unittest.main()
