#!/usr/bin/env python3
"""Offline-only preparation for the fixed 2021 candidate domain.

No collector, economic runner, provenance attestation or admission switch.
The CLI only emits an exclusive-write plan; an existing plan is never replaced.
"""
import argparse
import hashlib
import json
from pathlib import Path

from collect_mvp_reference_history import requests, validate_page as validate_rows
from mvp_reference_inputs import require, strict_json, window

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT/'.artifacts/offline-diagnostic-safety-20260930/frozen-model.json'
MODEL_SHA = 'af278f3b9612944d7bf5825ba01d910628e7604f96fa92785301464c58d9ed4c'
EVIDENCE = ROOT/'docs/reviews/2026-09-30-frozen-confirmation-feasibility.evidence.json'
EVIDENCE_SHA = '310654c681fe11171b201b97ac08c5d77130b7cd0bb05158f2ef907ad4d4a428'


def contract():
    return dict(symbol='BTCUSDT',interval_ms=300000,
        source_start_utc='2020-12-31T00:00:00Z',evaluation_start_utc='2021-01-01T00:00:00Z',
        end_exclusive_utc='2021-12-30T00:00:00Z',
        input=dict(public_host='api.bybit.com',funding_required_grid_ms=28800000,
            trade_endpoint='/v5/market/kline',mark_endpoint='/v5/market/mark-price-kline',
            funding_endpoint='/v5/market/funding/history'))


def canonical_requests():
    return requests(contract())


def validate_page(raw, request, received_at_ms):
    """Validate an exact planned page, including closure, bytes and input grid.

    This checks data integrity only. It cannot certify historical non-use.
    """
    require(request in canonical_requests(), 'REQUEST_NOT_IN_FIXED_PLAN')
    require(type(received_at_ms) is int and received_at_ms >= request['end']+1,
            'UNFINISHED_REQUEST_WINDOW')
    require(isinstance(raw,bytes) and len(raw)<=2*1024*1024,'RAW_PAGE_BYTES')
    return validate_rows(raw,request)


def validate_request_set(records):
    """No missing, duplicate, reordered or substituted request pages."""
    require(records==canonical_requests(),'REQUEST_SET_CHANGED')
    return True


def draft():
    c=contract()
    start,evaluation,end=window(c)
    records=canonical_requests()
    validate_request_set(records)
    return dict(schema='frozen_history_input_preparation_v1',status='PREPARED_NOT_ADMITTED',
        model_sha256=MODEL_SHA,coefficients=[0,-4],input_contract=c,
        requests=records,planned_new_gets=len(records),
        planned_page_counts={k:sum(r['kind']==k for r in records) for k in ('funding','trade','mark')},
        expected_source_bars=(end-start)//300000,expected_evaluation_bars=(end-evaluation)//300000,
        warmup_bars=(evaluation-start)//300000,expected_funding_events=(end-start)//28800000,
        final_bar_start_ms=end-300000,terminal_excluded_timestamp_ms=end,
        terminal_rule='Last included candle closes at the fixed end; no end-timestamp candle or funding event included. Future replay contract must explicitly bind settlement.',
        prior_probe_evidence_sha256=EVIDENCE_SHA,prior_probe_points_to_disclose=6,
        prior_probe_prices_used_for_selection=False,external_usage_status='UNKNOWN',
        fetch_allowed=False,economic_execution_allowed=False,confirmation_admitted=False,
        new_network_requests=0,new_economic_runs=0,qualification=False,
        blockers=['external_usage_fact_missing','whole_domain_coverage_not_verified',
                  'economic_contract_and_one_shot_claim_not_frozen'],
        old_compiler_compatible=False,
        compatibility_note='The old annual MVP compiler rejects end boundaries on a funding timestamp and accepts alternate contracts only as synthetic. Do not change its frozen contract or label real history synthetic to reuse it.')


def emit(output):
    require(hashlib.sha256(MODEL.read_bytes()).hexdigest()==MODEL_SHA,'MODEL_CHANGED')
    require(hashlib.sha256(EVIDENCE.read_bytes()).hexdigest()==EVIDENCE_SHA,'EVIDENCE_CHANGED')
    model,evidence=strict_json(MODEL.read_bytes()),strict_json(EVIDENCE.read_bytes())
    require(model['coefficients']==[0,-4] and evidence['external_usage_status']=='UNKNOWN',
            'REASSESS_FROM_NEW_EVIDENCE')
    value=draft()
    value['source_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (Path(__file__),ROOT/'tools/collect_mvp_reference_history.py',ROOT/'tools/mvp_reference_inputs.py')}
    with Path(output).open('x') as f:
        json.dump(value,f,indent=2,sort_keys=True,allow_nan=False)
        f.write('\n')
    print(json.dumps({k:value[k] for k in ('status','planned_new_gets','planned_page_counts',
        'expected_source_bars','expected_evaluation_bars','expected_funding_events',
        'fetch_allowed','economic_execution_allowed','new_network_requests')}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    emit(parser.parse_args().output)
