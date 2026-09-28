#!/usr/bin/env python3
"""Audit the stored capability probes; no network or economic calculation."""
from decimal import Decimal
import json
import review_eth_supply as r
from screen_regional_session import need, sha, strict, save_new, ms, utc


def catalog(rows):
    out={}
    for asset in rows:
        for metric in asset['metrics']:
            key=(asset['asset'],metric['metric'])
            need(key not in out,'DUPLICATE_CATALOG_PAIR')
            out[key]=metric['frequencies']
    return out


def sample(row,day):
    need(row['asset']=='eth' and ms(row['time'])==ms(day+'T00:00:00Z'),'SAMPLE_IDENTITY')
    for key in ('IssTotNtv','SplyCur','AssetCompletionTime','AssetEODCompletionTime'):
        value=Decimal(row[key])
        need(value.is_finite() and value>=0,'INVALID_SAMPLE_VALUE')
    completed=int(row['AssetEODCompletionTime'])*1000
    end=ms(day+'T00:00:00Z')+86400000
    return dict(date=day,period_end_utc=utc(end),recorded_completion_utc=utc(completed),
                seconds_after_period_end=(completed-end)//1000,
                status_fields_present=sorted(k for k in row if '-status' in k),
                present_fields=sorted(row),first_publication_proven=False)


def main():
    r.running()
    r.frozen()
    community=catalog(r.read('community')['data'])
    full=catalog(r.read('full')['data'])
    definitions={x['metric']:x for x in r.read('definitions')['data']}
    keys=[('eth',x) for x in ('IssTotNtv','SplyCur','SplyBurntNtv','AssetEODCompletionTime')]
    keys += [('eth_cl',x) for x in ('IssContNtv','IssTotNtv','PenaltyNtv','SlashedNtv')]
    mapping=[]
    for asset,metric in keys:
        free=[f for f in community.get((asset,metric),[]) if f['frequency']=='1d']
        all_daily=[f for f in full.get((asset,metric),[]) if f['frequency']=='1d']
        mapping.append(dict(asset=asset,metric=metric,free_daily=free,full_catalog_daily=all_daily,
                            definition_unit=definitions[metric]['unit']))
    need(('eth','SplyBurntNtv') not in community and ('eth','SplyBurntNtv') in full,'BURN_CATALOG_CONCLUSION_CHANGED')
    need(all(asset!='eth_cl' for asset,metric in community),'CONSENSUS_FREE_CONCLUSION_CHANGED')
    samples=[]
    for day in r.DATES:
        rows=r.read('sample-'+day)['data']
        need(len(rows)==1,'EXACT_ONE_DAY_SAMPLE')
        samples.append(sample(rows[0],day))
    need(any(x['seconds_after_period_end']>2*86400 for x in samples),'TIMING_OBSERVATION_CHANGED')
    receipts={p.stem:strict(p.read_bytes()) for p in (r.RUN/'receipts').glob('*.json')}
    need(len(receipts)==len(list((r.RUN/'attempts').glob('*.json')))==6,'PROBE_BUDGET_IDENTITY')
    need(all(v['status']==200 and v['error'] is None for v in receipts.values()),'PROBE_INTEGRITY')
    need(all(sha((r.RUN/v['raw']).read_bytes())==v['sha256'] for v in receipts.values()),'RAW_HASH')
    report=dict(status='PARTIAL_INPUTS',decision='DO_NOT_START_ORIGINAL_BURN_ATTRIBUTION_EXPERIMENT',
        scope='ETH_SUPPLY_INPUT_REVIEW_ONLY',mapping=mapping,samples=samples,
        source_read_units=34,recorded_anonymous_api_gets=6,total_public_read_units=40,
        source_code_open_units_included_in_34=6,combined_api_and_source_open_units=12,
        economic_experiments=0,market_price_rows=0,full_history_downloaded=False,
        synthetic_tests=10,formal_technical_failures=0,preserved_old_gate_sha256=r.identities(),
        raw_receipts=receipts,
        plan_sha256=sha((r.ROOT/r.PLAN).read_bytes()),
        free_issuance_and_total_supply_available=True,
        original_fee_burn_attribution_ready=False,
        net_supply_proxy_contract_design_possible=True,
        proxy_is_not_pure_fee_burn=True,point_in_time_vintage_proven=False,
        completion_delay_cause='UNKNOWN: first processing, recomputation or correction not distinguished',
        candidate_qualified=False,experiment_authorized=False,
        alternative_ultrasound='Official source inspected only: rounded daily timestamps, historical fill, and pending-deposit handling; no live historical response acceptance',
        commercial_use_permission='NOT_ESTABLISHED: Community documents specify noncommercial license',
        next='Close this input review; do not wait. A net-supply proxy would need an explicitly different frozen contract and bounded experiment scope.')
    save_new(r.RUN/'result.json',report)
    # Public evidence omits raw provider values; only schemas/availability/timestamps/hashes.
    public=dict(report)
    public['raw_receipts']={k:{f:v[f] for f in ('url','status','sha256','bytes','at')} for k,v in receipts.items()}
    save_new(r.ROOT/'docs/reviews/2026-09-28-eth-supply-input.evidence.json',public)
    print(json.dumps({k:v for k,v in report.items() if k not in ('raw_receipts','preserved_old_gate_sha256')},indent=2))


if __name__=='__main__':
    main()
