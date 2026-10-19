"""Synthetic-only integration; no archival market rows or economic attempt."""
import math
import unittest

import btc_old_output_model as base
import open_market_learning as learning
import open_market_execution as execution
import audit_open_market_learning as audit
import audit_open_market_execution as ledger


class PipelineTests(unittest.TestCase):
    def test_frozen_fit_to_predictions_to_full_decimal_calendar(self):
        origin = base.millis('2020-01-01')
        rows = []
        for i in range(100*288):
            def price(k):
                return 10000*math.exp(.0000005*k+.005*math.sin(k/100))
            op, cl = price(i-1), price(i)
            hi, lo = max(op, cl)*1.0002, min(op, cl)*.9998
            rows.append(dict(timestamp=origin+i*base.STEP, open=op, price=cl, high=hi, low=lo,
                mark_open=op, mark_close=cl, mark_high=hi, mark_low=lo,
                volume=100+10*math.sin(i/17), funding_rate_per_interval=.0001 if i % 96 == 0 else 0.))
        start, cutoff, end = origin+16*base.DAY, origin+80*base.DAY, origin+100*base.DAY
        fs = learning.features(rows, start, end)
        labels = learning.samples(rows, fs, start, cutoff)
        audit.audit_features_and_labels(rows, {t: f for t, f in fs.items() if t < cutoff},
                                        labels, start, cutoff)
        for arm in learning.ARMS:
            model = learning.fit(labels, arm, 1.)
            audit.audit_fit(labels, model)
            evaluation = {t: f for t, f in fs.items() if t >= cutoff+base.DAY}
            predictions = [learning.predict(model, f, t) for t, f in evaluation.items()]
            audit.audit_predictions(model, evaluation, predictions)
            decisions = {p['t']: p['direction'] for p in predictions}
            result = execution.simulate(rows, evaluation, decisions, cutoff+base.DAY, end)
            ledger.audit(result, rows, evaluation, decisions)
            self.assertEqual(result['bars'], 19*288)
            self.assertTrue(learning.complete(result))


if __name__ == '__main__':
    unittest.main()
