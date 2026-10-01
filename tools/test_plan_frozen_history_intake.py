"""Invented flat-price fixtures only; no HTTP, accounts, or historical replay."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import plan_frozen_history_intake as p


def setUpModule():
    # The explicitly requested joint suite imports the old integration class
    # without going through its --binary CLI. Bind the exact previously tested
    # binary here, never an implicit ROOT/build fallback. Standalone tests keep
    # working with synthetic inputs and do not require private build artifacts.
    if 'test_mvp_reference_pipeline.ReferenceInputTest' in sys.argv:
        import test_mvp_reference_pipeline as old
        binary=p.ROOT/'.artifacts/offline-diagnostic-safety-20260930/build/trade_bot'
        expected='8cd6c00938b67f426e3558f4639a898946898c508cf4ec4e333b17ba5ed70724'
        if p.hashlib.sha256(binary.read_bytes()).hexdigest()!=expected:
            raise ValueError('EXPLICIT_JOINT_SUITE_BINARY_CHANGED')
        old.BINARY=binary


class PreparationTest(unittest.TestCase):
    def fixture(self,kind,last=False):
        req=[r for r in p.canonical_requests() if r['kind']==kind][-1 if last else 0]
        times=list(range(req['start'],req['end']+1,req['step']))[::-1]
        rows=([dict(symbol='BTCUSDT',fundingRateTimestamp=str(t),fundingRate='-0.0001') for t in times]
            if kind=='funding' else [[str(t),'100','101','99','100']+
                (['1','100'] if kind=='trade' else []) for t in times])
        return req,dict(retCode=0,result=dict(category='linear',symbol='BTCUSDT',list=rows))

    def validate(self,req,obj):
        return p.validate_page(json.dumps(obj).encode(),req,req['end']+1)

    def test_exact_dates_counts_and_no_admission(self):
        d=p.draft()
        self.assertEqual(d['planned_new_gets'],216)
        self.assertEqual(d['planned_page_counts'],dict(trade=105,mark=105,funding=6))
        self.assertEqual((d['expected_source_bars'],d['warmup_bars'],d['expected_evaluation_bars']),
                         (104832,288,104544))
        self.assertEqual(d['expected_funding_events'],1092)
        self.assertEqual(d['coefficients'],[0,-4])
        self.assertEqual(d['prior_probe_points_to_disclose'],6)
        for k in ('fetch_allowed','economic_execution_allowed','confirmation_admitted','qualification',
                  'old_compiler_compatible'):
            self.assertIs(d[k],False)

    def test_each_grid_contiguous_no_excluded_day(self):
        d=p.draft();first=1609372800000;end=1640822400000
        self.assertEqual(d['final_bar_start_ms'],end-300000)
        for kind,total in (('trade',104832),('mark',104832),('funding',1092)):
            cursor=first;count=0
            for r in (r for r in d['requests'] if r['kind']==kind):
                self.assertEqual(r['start'],cursor)
                self.assertLess(r['end'],end)
                self.assertEqual(r['start']%r['step'],0)
                self.assertTrue(r['url'].startswith('https://api.bybit.com/v5/market/'))
                cursor=r['end']+1
                count+=len(range(r['start'],cursor,r['step']))
            self.assertEqual((cursor,count),(end,total))

    def test_funding_is_checked_first(self):
        kinds=[r['kind'] for r in p.canonical_requests()]
        self.assertEqual(kinds[:6],['funding']*6)
        self.assertEqual(kinds[6:10],['trade','mark','trade','mark'])

    def test_complete_first_and_partial_last_pages(self):
        for kind in ('trade','mark','funding'):
            for last in (False,True):
                with self.subTest(kind=kind,last=last):
                    r,v=self.fixture(kind,last)
                    self.assertEqual(self.validate(r,v),len(v['result']['list']))

    def test_unknown_missing_duplicate_or_reordered_pages(self):
        records=p.canonical_requests()
        self.assertTrue(p.validate_request_set(records))
        variants=[[],records[:-1],records+[records[0]],list(reversed(records))]
        changed=copy.deepcopy(records);changed[0]['url']=changed[0]['url'].replace('api.bybit.com','example.com')
        variants.append(changed)
        for v in variants:
            with self.assertRaisesRegex(ValueError,'REQUEST_SET_CHANGED'):p.validate_request_set(v)

    def test_page_cannot_change_requested_domain(self):
        r,v=self.fixture('trade')
        for key,value in (('start',r['start']+300000),('end',r['end']+300000),('limit',999),
                          ('url',r['url']+'&symbol=ETHUSDT')):
            with self.assertRaisesRegex(ValueError,'REQUEST_NOT_IN_FIXED_PLAN'):
                self.validate(dict(r,**{key:value}),v)

    def test_missing_duplicate_reordered_and_extra_rows(self):
        for kind in ('trade','mark','funding'):
            r,v=self.fixture(kind,True)
            for how in ('missing','duplicate','reversed','extra'):
                w=copy.deepcopy(v);rows=w['result']['list']
                if how=='missing': rows.pop()
                elif how=='duplicate': rows[1]=rows[0]
                elif how=='reversed': rows.reverse()
                else: rows.append(rows[-1])
                with self.subTest(kind=kind,how=how),self.assertRaises(ValueError):self.validate(r,w)

    def test_api_errors_category_and_symbol(self):
        r,v=self.fixture('trade')
        for code in (10001,False,'0'):
            with self.assertRaisesRegex(ValueError,'API_ERROR'):self.validate(r,dict(v,retCode=code))
        for key,value in (('category','inverse'),('symbol','ETHUSDT')):
            w=copy.deepcopy(v);w['result'][key]=value
            with self.assertRaises(ValueError):self.validate(r,w)
        r,v=self.fixture('funding');v['result']['list'][0]['symbol']='ETHUSDT'
        with self.assertRaisesRegex(ValueError,'FUNDING_SYMBOL'):self.validate(r,v)

    def test_changed_funding_grid_and_nonfinite_rates(self):
        r,v=self.fixture('funding')
        w=copy.deepcopy(v);w['result']['list'][0]['fundingRateTimestamp']=str(r['start']+14400000)
        with self.assertRaisesRegex(ValueError,'PAGE_GRID'):self.validate(r,w)
        for rate in ('NaN','Infinity','-Infinity',True):
            w=copy.deepcopy(v);w['result']['list'][0]['fundingRate']=rate
            with self.assertRaises(ValueError):self.validate(r,w)

    def test_bad_ohlc_or_volume(self):
        r,v=self.fixture('trade')
        for index,value in ((2,'98'),(3,'102'),(1,'0'),(5,'-1'),(6,'NaN')):
            w=copy.deepcopy(v);w['result']['list'][0][index]=value
            with self.assertRaises(ValueError):self.validate(r,w)

    def test_unfinished_or_noninteger_receipt_clock(self):
        r,v=self.fixture('mark');raw=json.dumps(v).encode()
        for clock in (r['end'],float(r['end']+1),True):
            with self.assertRaisesRegex(ValueError,'UNFINISHED'):p.validate_page(raw,r,clock)

    def test_bytes_and_duplicate_json_keys(self):
        r,_=self.fixture('mark')
        for raw in ('{}',b' '*(2*1024*1024+1)):
            with self.assertRaisesRegex(ValueError,'RAW_PAGE_BYTES'):p.validate_page(raw,r,r['end']+1)
        with self.assertRaisesRegex(ValueError,'DUPLICATE_JSON'):
            p.validate_page(b'{"retCode":0,"retCode":0}',r,r['end']+1)

    def test_draft_never_constructs_transport(self):
        with mock.patch('collect_mvp_reference_history.opener',side_effect=AssertionError('NETWORK')):
            self.assertEqual(p.draft()['new_network_requests'],0)

    def test_emit_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);model=root/'model.json';evidence=root/'evidence.json';out=root/'plan.json'
            model.write_text(json.dumps(dict(coefficients=[0,-4])))
            evidence.write_text(json.dumps(dict(external_usage_status='UNKNOWN')))
            with mock.patch.multiple(p,MODEL=model,EVIDENCE=evidence,
                    MODEL_SHA=p.hashlib.sha256(model.read_bytes()).hexdigest(),
                    EVIDENCE_SHA=p.hashlib.sha256(evidence.read_bytes()).hexdigest()):
                p.emit(out);saved=out.read_bytes()
                with self.assertRaises(FileExistsError):p.emit(out)
                self.assertEqual(out.read_bytes(),saved)

    def test_changed_model_or_evidence_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);model=root/'model.json';evidence=root/'evidence.json'
            model.write_bytes(b'{}');evidence.write_bytes(b'{}')
            with mock.patch.multiple(p,MODEL=model,EVIDENCE=evidence):
                with self.assertRaisesRegex(ValueError,'MODEL_CHANGED'):p.emit(root/'no.json')
            with mock.patch.multiple(p,MODEL=model,EVIDENCE=evidence,
                    MODEL_SHA=p.hashlib.sha256(model.read_bytes()).hexdigest()):
                with self.assertRaisesRegex(ValueError,'EVIDENCE_CHANGED'):p.emit(root/'no.json')
            self.assertFalse((root/'no.json').exists())


if __name__=='__main__':unittest.main()
