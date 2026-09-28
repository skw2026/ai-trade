#!/usr/bin/env python3
"""Read-only sample-capacity diagnosis, not a second economic experiment.

No network, changed market dates, post-signal returns, fees, bootstrap or gate run.
The all-restored view is hypothetical and never restores the actual sample mask.
"""
from decimal import Decimal
import json
from pathlib import Path

import free_option_flow_data as f
from screen_regional_session import need, sha, strict, ms, save_new

OUT = f.RUN/'capacity-diagnosis'


def protected():
    paths = [p for p in f.RUN.rglob('*') if p.is_file() and OUT not in p.parents]
    paths += [f.ROOT/'.artifacts'/p for p in f.GATES]
    paths += [f.ROOT/f.PLAN]
    spec = strict((f.RUN/'economy-freeze.json').read_bytes())
    paths += [f.ROOT/p for p in spec['source_sha256']]
    paths += [f.ROOT/'docs/reviews'/name for name in (
        '2026-09-28-free-option-flow-result.md','2026-09-28-free-option-flow.evidence.json')]
    return {str(p.relative_to(f.ROOT)):sha(p.read_bytes()) for p in sorted(set(paths))}


def main():
    before = protected()
    need(strict(f.GATE.read_bytes())['status'] == 'READY', 'CLOSED_STAGE_IDENTITY')
    need((f.RUN/'experiment-start.json').is_file() and (f.RUN/'delivery.json').is_file(), 'CLOSED_BATCH_REQUIRED')
    collection = strict((f.RUN/'collection.json').read_bytes())
    baseline = strict((f.RUN/'result.json').read_bytes())
    need(baseline['reason'] == 'INSUFFICIENT_SAMPLE' and baseline['economic_experiments'] == 1,
         'BOUND_DIAGNOSIS_ONLY')
    need(all(sha((f.RUN/p).read_bytes()) == h for p,h in collection['input_sha256'].items()), 'INPUT_CHANGED')
    need(f.old_hashes() == strict((f.RUN/'freeze.json').read_bytes())['old_gates'], 'OLD_GATES_CHANGED')
    original,restored = dict(valid=0,A=0,B=0,C=0),dict(valid=0,A=0,B=0,C=0)
    details = []
    for quality in collection['quality']:
        day = quality['date']
        opt = f.options(f.read_page(day+'-options'),day,True)
        calls = opt['calls']
        eligible = bool(opt['quality'] and calls)
        group = None
        if eligible:
            # Only these two pre-entry prices are selected; never calculate a holding-period return.
            times = (ms(day+'T07:30:00Z'), ms(day+'T07:55:00Z'))
            rows = strict(f.read_page(day+'-trade').read_bytes())['result']['list']
            prices = {int(r[0]):Decimal(r[1]) for r in rows if int(r[0]) in times}
            need(set(prices) == set(times), 'PRE_SIGNAL_PRICE_MISSING')
            imbalance = sum((Decimal(r['amount'])*(1 if r['side']=='sell' else -1) for r in calls),Decimal(0))
            down = prices[times[1]] < prices[times[0]]
            group = 'A' if down and imbalance > 0 else 'B' if down else 'C_ONLY'
            restored['valid'] += 1
            restored['A' if group=='A' else 'C'] += 1
            restored['B'] += group=='B'
            if not quality['incident_overlap']:
                original['valid'] += 1
                original['A' if group=='A' else 'C'] += 1
                original['B'] += group=='B'
        if quality['incident_overlap']:
            details.append(dict(date=day,has_call_volume=bool(calls),hypothetical_group=group))
    need(original['valid'] == baseline['valid_days'] and
         {k:original[k] for k in ('A','B','C')} == baseline['counts'], 'BASELINE_COUNT_MISMATCH')
    thresholds = dict(valid=30,A=8,B=8,C=8)
    report = dict(scope='SAMPLE_CAPACITY_DIAGNOSIS_NOT_RESEARCH_REOPENING',
        actual_accepted_counts_unchanged=original,
        hypothetical_all_incident_dates_restored_counts=restored,
        minimum_counts=thresholds,
        remaining_best_case_shortfall={k:max(0,thresholds[k]-restored[k]) for k in thresholds},
        restoring_all_incident_dates_would_satisfy_sample_floors=all(restored[k]>=v for k,v in thresholds.items()),
        uncertain_date_details=details,
        public_gets_added=0,economic_experiments_added=0,returns_computed=False,
        original_date_mask_changed=False,incident_dates_corrected=False,candidate_qualified=False,
        protected_identity_sha256=before)
    need(protected() == before, 'PROTECTED_EVIDENCE_CHANGED')
    OUT.mkdir(exist_ok=False)
    save_new(OUT/'result.json',report)
    need(protected() == before, 'POST_DIAGNOSIS_EVIDENCE_CHANGED')
    print(json.dumps({k:v for k,v in report.items() if k!='protected_identity_sha256'},indent=2))
    print(json.dumps(dict(protected_files_unchanged=len(before),gates_unchanged=len(f.GATES)+1,
                          result_sha256=sha((OUT/'result.json').read_bytes()))))


if __name__ == '__main__':
    main()
