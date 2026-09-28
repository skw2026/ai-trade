#!/usr/bin/env python3
"""Sanitized closeout from the sole stored experiment; no new returns or network."""
import json
from pathlib import Path

import attention_capacity as a
import screen_attention as s
from screen_regional_session import need, sha, strict, save_new, utc


def main():
    a.running()
    s.bound()
    result = strict((a.RUN/'result.json').read_bytes())
    audit = strict((a.RUN/'audit.json').read_bytes())
    capacity = strict((a.RUN/'capacity.json').read_bytes())
    need(audit['result_sha256'] == sha((a.RUN/'result.json').read_bytes()),'AUDIT_RESULT_IDENTITY')
    need(result['status']=='REJECT' and result['reason']=='REFERENCE_DRAWDOWN','EXPECTED_FROZEN_EXIT')
    need(result['statistics'] is None and result['economic_experiments']==1,'NO_SECOND_EXPERIMENT')
    completed = result['rows']
    metrics = ('raw_gross','slippage','fee','funding','net','price_control_net','long_control_net')
    means = {key:sum(row[key] for row in completed)/len(completed) for key in metrics}
    need(abs(means['raw_gross']-means['slippage']-means['fee']-means['funding']-means['net'])<1e-12,
         'ATTRIBUTION_IDENTITY')
    receipts = [strict(p.read_bytes()) for p in sorted((a.RUN/'receipts').glob('*.json'))]
    need(len(receipts)==len(list((a.RUN/'attempts').glob('*.json')))==99,'REQUEST_RECEIPTS')
    need(all(r['status']==200 and r['error'] is None for r in receipts),'REQUEST_FAILURE')
    need(all(sha((a.RUN/'raw'/r['raw']).read_bytes())==r['sha256'] for r in receipts),'RECEIPT_RAW_IDENTITY')
    states = strict(a.GATE.read_bytes())
    need(not any(h.get('exit_code',0) != 0 for h in states['history']),'UNRESOLVED_TECHNICAL_FAILURE')
    report = dict(stage='capacity-first-20260928',status=result['status'],reason=result['reason'],
        selected_candidates=0,candidate_qualified=False,candidate_state='NO_QUALIFIED_CANDIDATE',
        capacity={k:capacity[k] for k in ('valid_days','cells','halves','eligible','thresholds')},
        excluded_days=len(capacity['excluded']),input_rows={kind:len(rows) for kind,rows in
          a.inputs({'views','trade','mark','funding'}).items()},
        completed_positions=len(completed), completed_prefix_mean_unit_returns=means,
        attribution_scope='Descriptive completed prefix only, not full-history or causal profit attribution',
        last_completed_nav=result['last_reference_nav'],prior_peak=result['peak'],
        stop=dict(result['stop'],at_utc=utc(result['stop']['at']),entry_utc=utc(result['stop']['entry'])),
        no_later_outcomes_computed=True,bootstrap_executed=False,
        public_reads=122,documentation_read_units=23,actual_public_gets=99,all_actual_gets_200=True,
        economic_experiments=1,new_synthetic_tests=27,technical_failures=0,
        audit={k:v for k,v in audit.items() if k!='stop'},
        preserved_old_gate_sha256=a.identities(),
        record_sha256={name:sha((a.RUN/name).read_bytes()) for name in (
            'freeze.json','capacity.json','economy-freeze.json','experiment-start.json','result.json','audit.json')},
        contract_sha256=sha((a.ROOT/a.CONTRACT).read_bytes()),
        limitations=['Reconstructed pageviews, not proven historical first-publication vintages',
                     'Reference fee/slippage assumptions, not account execution',
                     'Hourly mark extrema identify a definite breach, not an executable stop fill',
                     'Rejects this frozen rule/risk contract, not all attention mechanisms'],
        r2_status='NOT_SELECTED_BEFORE_EXPERIMENT_NO_ECONOMIC_VERDICT',
        continuation='Batch closed. No tuning, sample expansion, second experiment, training, trading or deployment.')
    save_new(a.RUN/'closeout.json',report)
    save_new(a.ROOT/'docs/reviews/2026-09-28-attention-capacity.evidence.json',report)
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
