"""Independently rebuild the causal S2 schedule; no returns or network."""
import datetime as dt
import json
from pathlib import Path

import eth_queue_development_inputs as q
from mvp_reference_inputs import strict_json
from run_bounded_learning import need, sha


def audit():
    c = strict_json(q.CONTRACT.read_bytes())
    f = strict_json((q.RUN/'freeze.json').read_bytes())
    need(sha(q.CONTRACT) == f['contract_sha256'] and sha(q.PLAN) == f['plan_sha256'], 'CONTRACT_IDENTITY')
    protected = q.preserve()
    snapshots = []
    for req in f['queue_requests']:
        rec = strict_json((q.RUN/'pages'/(req['key']+'.json')).read_bytes())
        path = q.ROOT/rec['reuse'] if rec.get('reuse') else q.RUN/'raw'/rec['raw']
        need(sha(path) == rec['sha256'], 'SNAPSHOT_IDENTITY')
        rows = strict_json(path.read_bytes())
        snapshots.append((req['available_ms'], req['key'], rows[-1]))
    snapshots.sort()
    # A separate linear as-of join instead of the producer's bisect join.
    def lookup(clock):
        seen = [s for s in snapshots if s[0] <= clock-60000]
        if not seen:
            return None
        available, key, row = seen[-1]
        day = dt.date.fromisoformat(row['date'])
        age = (dt.datetime.fromtimestamp(clock/1000, dt.timezone.utc).date()-day).days
        if not 0 <= age <= 2:
            return None
        return dict(sha=key, available_ms=available, date=row['date'], exit_queue=row['exit_queue'])
    result = strict_json((q.RUN/'schedule.json').read_bytes())
    grid = list(range(q.ms(c['decision_start_utc']), q.ms(c['last_entry_utc'])+1, q.DAY))
    need([r['entry_ms'] for r in result['rows']] == grid, 'DAILY_GRID')
    useful = active = 0
    counts = [0, 0]
    groups = []
    for t, row in zip(grid, result['rows']):
        left, right = lookup(t-7*q.DAY), lookup(t)
        need(row['current'] == right and row['past'] == left and row['exit_ms'] == t+q.DAY, 'ASOF_JOIN')
        valid = left is not None and right is not None
        trade = valid and right['exit_queue'] > left['exit_queue']
        need(row['usable'] is valid and row['active'] is trade, 'CAUSAL_SIGNAL')
        useful += valid
        active += trade
        if trade:
            counts[int(t >= q.ms(c['split_utc']))] += 1
            if not groups or t >= groups[-1]['exit_ms']+7*q.DAY:
                groups.append(dict(id=len(groups), entry_ms=t, exit_ms=t+q.DAY, days=0))
            groups[-1]['exit_ms'] = t+q.DAY
            groups[-1]['days'] += 1
            need(row['group'] == len(groups)-1, 'GROUP_ASSIGNMENT')
        else:
            need(row['group'] is None, 'FLAT_GROUP')
    need(result['groups'] == groups, 'EVENT_GROUPS')
    eligible = useful/len(grid) >= .9 and active >= 30 and len(groups) >= 5 and min(counts) >= 10
    need(result['capacity_admitted'] is eligible and not result['independent_confirmation_admitted'], 'CAPACITY_VERDICT')
    need((result['usable_days'], result['signal_days'], result['subperiod_signal_days'], result['separated_groups']) ==
         (useful, active, counts, len(groups)), 'CAPACITY_COUNTS')
    print(json.dumps(dict(independent_asof_join='MATCH', versions=len(snapshots), days=len(grid),
        signal_days=active, separated_groups=len(groups), capacity_admitted=eligible, preserved=protected,
        independent_confirmation_admitted=False)))


if __name__ == '__main__': audit()
