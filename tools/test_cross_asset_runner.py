import json
from pathlib import Path
import tempfile
import unittest

import run_cross_asset as runner
import audit_cross_asset as audit
import test_cross_asset_model as fixtures


class Runner(unittest.TestCase):
    def test_economic_attempt_cannot_be_repeated(self):
        with tempfile.TemporaryDirectory() as d:
            runner.claim_attempt(d, {'attempt': 1})
            before = (Path(d)/'economic-attempt.json').read_bytes()
            with self.assertRaises(FileExistsError):
                runner.claim_attempt(d, {'attempt': 2})
            self.assertEqual((Path(d)/'economic-attempt.json').read_bytes(), before)

    def test_independent_negative_selection(self):
        make = fixtures.Selection().fixture
        p = {7: make(9900), 14: make(9950), 28: make(9900)}
        self.assertEqual(audit.selection(p, 14)['chosen'], 14)
        with self.assertRaises(ValueError):
            audit.selection(p, 7)

    def test_independent_tie_and_all_closed(self):
        make = fixtures.Selection().fixture
        self.assertEqual(audit.selection({7: make(9900), 28: make(9900)}, 7)['chosen'], 7)
        self.assertIsNone(audit.selection({7: make(9900, 'RISK')}, None)['chosen'])

    def test_source_catalog_is_explicit_and_present(self):
        self.assertEqual(len(runner.SOURCES), len(set(runner.SOURCES)))
        self.assertTrue(all(Path(p).is_file() and not p.startswith('.artifacts/') for p in runner.SOURCES))

    def test_bounded_resume_does_not_repeat_existing_paths(self):
        saved = ['prefix-7', 'prefix-14', 'prefix-28', 'normal-7', 'normal-14', 'delay-7']
        self.assertEqual(runner.missing_tasks(14, saved), ['delay-14'])
        self.assertEqual(runner.missing_tasks(14, saved+['delay-14']), [])
        with self.assertRaises(ValueError):
            runner.missing_tasks(14, saved+['normal-21'])


if __name__ == '__main__':
    unittest.main()
