"""The one pre-frozen development attempt; no training or live actuator."""
import argparse
import json
import os
import time

import aave_market_input as inp
import aave_economic_model as model


def execute():
    os.umask(0o077)
    c = inp.guard()
    inp.preserve()
    model.require((inp.ROOT/'market-audit.json').is_file(), 'MARKET_AUDIT_REQUIRED')
    data, opportunities, stats = inp.market()
    model.require(not (inp.ROOT/'economic-attempt.json').exists(), 'ECONOMIC_ATTEMPT_ALREADY_CONSUMED')
    inp.save(inp.ROOT/'economic-attempt.json', dict(attempt=1, started_utc=inp.now(),
        freeze_sha256=inp.sha(inp.ROOT/'freeze.json'), market_audit_sha256=inp.sha(inp.ROOT/'market-audit.json')))
    schedule = model.event_schedule(opportunities, data)
    model.require(len(schedule) == 492 and sum(v['candidate'] for v in schedule) == 275, 'FIXED_SCHEDULE_CHANGED')
    deadline = time.monotonic()+c['maximum_compute_seconds']
    paths = {}
    for arm in ('candidate', 'baseline'):
        for cost, multiplier in (('base', 1), ('stress', c['stress_multiplier'])):
            inp.guard()
            paths[arm+'_'+cost] = model.simulate(schedule, data, c, multiplier, arm, deadline)
    assessment = model.assess(paths, c)
    result = dict(freeze_sha256=inp.sha(inp.ROOT/'freeze.json'), assessment=assessment,
        market_statistics=stats, schedule=schedule, paths=paths, completed_utc=inp.now(),
        note='All model fills and risk envelopes are reference assumptions, not actual account results')
    inp.guard()
    inp.save(inp.ROOT/'result.json', result)
    inp.preserve()
    print(json.dumps(assessment, sort_keys=True), flush=True)


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    execute()
