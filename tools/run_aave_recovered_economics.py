"""New delivery orchestrator using unchanged pre-price model and Decimal audit."""
import argparse
import json
import os
import time

import aave_market_recovery as inp
import aave_economic_model as model
import audit_aave_economic_screen as audit
from mvp_reference_inputs import strict_json


def execute():
    os.umask(0o077)
    inp.guard(); inp.preserve()
    c = strict_json(inp.old.CONTRACT.read_bytes())
    admission = strict_json((inp.ROOT/'market-audit.json').read_bytes())
    model.require(admission['status'] == 'FULL_FIXED_MARKET_ADMITTED' and
        admission['collection_sha256'] == inp.sha(inp.ROOT/'collection.json'), 'ADMISSION_BINDING')
    for root in (inp.ROOT, inp.old.ROOT):
        model.require(not (root/'economic-attempt.json').exists(), 'ONE_TOTAL_ECONOMIC_ATTEMPT')
    data, opportunities, stats = inp.market()
    inp.save(inp.ROOT/'economic-attempt.json', dict(attempt=1, started_utc=inp.now(),
        freeze_sha256=inp.sha(inp.ROOT/'freeze.json'), economic_contract_sha256=inp.sha(inp.old.CONTRACT),
        market_audit_sha256=inp.sha(inp.ROOT/'market-audit.json')))
    schedule = model.event_schedule(opportunities, data)
    model.require(len(schedule) == 492 and sum(v['candidate'] for v in schedule) == 275, 'FIXED_SCHEDULE_CHANGED')
    deadline = time.monotonic()+c['maximum_compute_seconds']
    paths = {}
    for arm in ('candidate', 'baseline'):
        for cost, multiplier in (('base', 1), ('stress', c['stress_multiplier'])):
            inp.guard()
            paths[arm+'_'+cost] = model.simulate(schedule, data, c, multiplier, arm, deadline)
    assessment = model.assess(paths, c)
    inp.guard()
    inp.save(inp.ROOT/'result.json', dict(freeze_sha256=inp.sha(inp.ROOT/'freeze.json'), assessment=assessment,
        economic_contract_sha256=inp.sha(inp.old.CONTRACT), market_statistics=stats, schedule=schedule,
        paths=paths, completed_utc=inp.now(), note='Reference development model, not account results or independent confirmation'))
    inp.preserve()
    print(json.dumps(assessment, sort_keys=True), flush=True)


def verify():
    inp.guard(); inp.preserve()
    c = strict_json(inp.old.CONTRACT.read_bytes())
    data, opportunities, _ = inp.market()
    result = strict_json((inp.ROOT/'result.json').read_bytes())
    attempt = strict_json((inp.ROOT/'economic-attempt.json').read_bytes())
    audit.ensure(result['freeze_sha256'] == attempt['freeze_sha256'] == inp.sha(inp.ROOT/'freeze.json') and
        result['economic_contract_sha256'] == attempt['economic_contract_sha256'] == inp.sha(inp.old.CONTRACT) and
        attempt['market_audit_sha256'] == inp.sha(inp.ROOT/'market-audit.json'), 'AUDIT_INPUT_BINDING')
    audit.ensure(not (inp.old.ROOT/'economic-attempt.json').exists(), 'AUDIT_OLD_ATTEMPT_UNEXPECTED')
    schedule = audit.exact_schedule(opportunities, data)
    audit.compare(result['schedule'], schedule)
    audit.ensure(set(result['paths']) == {'candidate_base', 'candidate_stress', 'baseline_base', 'baseline_stress'}, 'AUDIT_FOUR_PATHS')
    exact = {}
    for name, path in result['paths'].items():
        arm, cost = name.split('_')
        multiplier = 1 if cost == 'base' else c['stress_multiplier']
        audit.ensure(path['arm'] == arm and path['multiplier'] == multiplier, 'AUDIT_PATH_IDENTITY')
        exact[name] = audit.exact_path(schedule, data, c, multiplier, arm)
        audit.compare(path, exact[name])
    verdict, statistics = audit.decision(exact, c)
    audit.ensure(result['assessment']['decision'] == verdict, 'AUDIT_DECISION_MISMATCH')
    audit.compare(result['assessment'], statistics)
    audit.ensure(result['assessment']['economic_attempts'] == 1 and result['assessment']['qualification'] is False
        and result['assessment']['independent_confirmation_admitted'] is False, 'AUDIT_AUTHORITY')
    report = dict(status='INDEPENDENT_FROZEN_MODEL_DECIMAL_AUDIT_PASS', decision=verdict,
        result_sha256=inp.sha(inp.ROOT/'result.json'), protected_files=inp.preserve(),
        paths={k: dict(cash_lo=v['cash_lo'], cash_hi=v['cash_hi'], fills=v['fills'], complete=v['complete']) for k, v in exact.items()},
        economic_attempts=1, g4='NOT_ACHIEVED')
    inp.guard()
    inp.save(inp.ROOT/'economic-audit.json', report)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('execute', 'verify'))
    args = parser.parse_args()
    {'execute': execute, 'verify': verify}[args.command]()
