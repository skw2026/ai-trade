from collections import Counter
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import aave_market_input as inp


def config():
    return json.loads(inp.CONTRACT.read_text())


def fixture(kind='trade'):
    request = dict(kind=kind, start=60000, end=179999, limit=1000 if kind != 'funding' else 200)
    rows = [[str(t), '100', '101', '99', '100']+(['1', '100'] if kind == 'trade' else []) for t in (120000, 60000)]
    if kind == 'funding':
        rows = [dict(symbol='ETHUSDT', fundingRateTimestamp='120001', fundingRate='0.001')]
    result = dict(retCode=0, time=9999999, result=dict(category='linear', symbol='ETHUSDT', list=rows))
    return request, result


class MarketContractTests(unittest.TestCase):
    def test_fixed_full_domain_and_controls(self):
        c = config(); reqs = inp.requests(c)
        self.assertEqual(len(reqs), 1422)
        self.assertEqual(Counter(v['kind'] for v in reqs), {'funding': 366, 'trade': 528, 'mark': 528})
        self.assertEqual([v['kind'] for v in reqs[:3]], ['funding', 'trade', 'mark'])
        for kind in ('trade', 'mark', 'funding'):
            subset = sorted((v for v in reqs if v['kind'] == kind), key=lambda v: v['start'])
            self.assertEqual(subset[-1]['end']+1, c['evaluation_end_ms'])
            self.assertTrue(all(a['end']+1 == b['start'] for a, b in zip(subset, subset[1:])))
        self.assertEqual(c['prior_active_seconds_reserved']+c['remaining_active_seconds'], 28800)

    def test_valid_trade_mark_and_irregular_funding(self):
        for kind in ('trade', 'mark', 'funding'):
            req, obj = fixture(kind)
            self.assertTrue(inp.validate(json.dumps(obj).encode(), req))

    def test_missing_duplicate_and_extra_minute_rejected(self):
        req, obj = fixture()
        for rows in (obj['result']['list'][:1], obj['result']['list']*2,
                     obj['result']['list']+[['180000', '100', '101', '99', '100', '1', '100']]):
            changed = copy.deepcopy(obj); changed['result']['list'] = rows
            with self.assertRaises(ValueError): inp.validate(json.dumps(changed).encode(), req)

    def test_wrong_market_ohlc_nonfinite_or_api_failure(self):
        req, obj = fixture()
        examples = []
        for key, value in (('symbol', 'BTCUSDT'), ('category', 'spot')):
            changed = copy.deepcopy(obj); changed['result'][key] = value; examples.append(changed)
        changed = copy.deepcopy(obj); changed['retCode'] = True; examples.append(changed)
        changed = copy.deepcopy(obj); changed['result']['list'][0][2] = '98'; examples.append(changed)
        changed = copy.deepcopy(obj); changed['result']['list'][0][1] = 'NaN'; examples.append(changed)
        for value in examples:
            with self.assertRaises(ValueError): inp.validate(json.dumps(value).encode(), req)

    def test_empty_and_full_funding_pages_reject(self):
        req, obj = fixture('funding')
        for count in (0, 200):
            obj['result']['list'] = [dict(symbol='ETHUSDT', fundingRateTimestamp=str(120000-i), fundingRate='0') for i in range(count)]
            with self.assertRaisesRegex(ValueError, 'EMPTY_OR_CENSORED'): inp.validate(json.dumps(obj).encode(), req)

    def test_transport_failure_is_archived_without_retry(self):
        c = config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root/'attempts').mkdir()
            requests = inp.requests(c)
            (root/'freeze.json').write_text(json.dumps(dict(requests=requests)))
            (root/'validation-state.json').write_text(json.dumps(dict(status='RUNNING', active={})))
            response = subprocess.CompletedProcess([], 28, b'\n000', b'network timeout')
            with patch.object(inp, 'ROOT', root), patch.object(inp, 'guard', return_value=c), \
                 patch.object(inp, 'preserve', return_value=1), patch.object(inp.time, 'sleep'), \
                 patch.object(inp.subprocess, 'run', return_value=response) as run:
                with self.assertRaisesRegex(ValueError, 'TRANSPORT'): inp.collect(3)
                self.assertEqual(run.call_count, 1)
                record = json.loads((root/'attempts/0000/record.json').read_text())
                self.assertEqual(record['status'], 'FAILED')
                self.assertEqual(record['curl_returncode'], 28)
                with self.assertRaisesRegex(ValueError, 'REVIEW'): inp.collect(3)
                self.assertEqual(run.call_count, 1)
                argv = run.call_args.args[0]
                self.assertEqual(argv[argv.index('--retry')+1], '0')
                self.assertNotIn('--insecure', argv)
                self.assertNotIn('--location', argv)


if __name__ == '__main__': unittest.main()
