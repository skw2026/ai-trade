"""Synthetic policy-design checks only: no strategy runner or trading authority."""
import argparse
import datetime as dt
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess


ROOT = Path('.artifacts/product-research-policy-20261015')
PRIOR = Path('.artifacts/eth-sol-program-20261015')
POLICY = Path('docs/research_product_policy.json')
DOC = Path('docs/RESEARCH_PRODUCT_POLICY.md')
REVIEW = Path('docs/reviews/2026-10-15-product-research-policy-result.md')
ROUND_TABLE = Path('docs/reviews/2026-10-15-project-multi-agent-roundtable.md')
SOURCES = (Path(__file__).resolve(), Path('tools/test_research_policy_design.py'), POLICY, DOC)
DEADLINE = dt.datetime(2026, 10, 15, 6, 19, 43, tzinfo=dt.timezone.utc)
REQUIRED_SCOPE = {
    'named_falsifiable_mechanism', 'difference_from_closed_routes',
    'complete_cost_and_exit_model', 'development_and_independent_input_identity',
    'frozen_learning_algorithm_and_reference', 'same_research_and_delivery_semantics',
    'performance_alert_trigger_and_recovery_protocol', 'minimum_economic_effect_and_units',
    'dependence_and_tail_assumptions', 'familywise_selection_and_monitoring_policy',
    'false_admission_limit_and_minimum_power', 'event_capacity_and_final_endpoint',
    'total_resource_budget_and_negative_exit',
}


def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def finite(value):
    need(type(value) in (int, float) and math.isfinite(value), 'FINITE_NUMBER_REQUIRED')


def flags(*values):
    need(all(type(v) is bool for v in values), 'EXPLICIT_BOOLEAN_REQUIRED')


def development_case(baseline_net, *, input_valid, scope_complete, budget_available, closed):
    """Test semantics, not an input attestation or permission to execute research."""
    finite(baseline_net)
    flags(input_valid, scope_complete, budget_available, closed)
    if closed:
        return 'CASE_CLOSED_ROUTE_REMAINS_CLOSED'
    if not (input_valid and scope_complete and budget_available):
        return 'CASE_NOT_ADMITTED'
    return 'CASE_BOUNDED_DEVELOPMENT_ALLOWED'


def economic_case(base, stress, reference_base, reference_stress, *, learned, fills,
                  evidence_complete, identity_matches, hard_risk, closed):
    """Supplied evidence flags are synthetic fixtures, never real certification."""
    for value in (base, stress, reference_base, reference_stress):
        finite(value)
    flags(learned, evidence_complete, identity_matches, hard_risk, closed)
    need(type(fills) is int and fills >= 0, 'FILL_COUNT')
    if closed:
        return 'CASE_CLOSED_ROUTE_REMAINS_CLOSED'
    if hard_risk:
        return 'CASE_REJECT_HARD_RISK'
    if not (evidence_complete and identity_matches):
        return 'CASE_INSUFFICIENT'
    if fills == 0 or base <= 0 or stress <= 0:
        return 'CASE_REJECT_NET_OR_ACTIVITY'
    if learned and (base <= reference_base or stress <= reference_stress):
        return 'CASE_REJECT_LEARNING_INCREMENT'
    return 'CASE_ECONOMIC_SUPPORT_NOT_DEPLOYMENT_PERMISSION'


def performance_case(*, operator_lock, existing_latch, hard_risk, evidence_known, alert):
    """The new-design action table only; cannot actuate or clear any live state."""
    flags(operator_lock, existing_latch, hard_risk, evidence_known, alert)
    # A known hard violation remains visible even when inputs or other guards fail.
    if hard_risk:
        return 'STOP_PATH_AND_UNWIND'
    if operator_lock or existing_latch:
        return 'RETAIN_EXISTING_REDUCE_ONLY_OR_LATCH'
    if not evidence_known:
        return 'BLOCK_NEW_RISK_AND_RECONCILE'
    if alert:
        return 'FREEZE_UPDATES_AND_REVIEW'
    return 'NO_NEW_ACTION_NO_UNLOCK_PERMISSION'


def masses(n, p):
    need(type(n) is int and 1 <= n <= 200, 'BOUNDED_SAMPLE_COUNT')
    p = Fraction(p)
    need(0 <= p <= 1, 'PROBABILITY_RANGE')
    return [math.comb(n, k) * p**k * (1-p)**(n-k) for k in range(n+1)]


def independent_masses(n, p):
    """Convolution recurrence independent of the binomial-coefficient formula."""
    p = Fraction(p)
    row = [Fraction(1)]
    for _ in range(n):
        new = [Fraction(0)] * (len(row)+1)
        for k, value in enumerate(row):
            new[k] += value*(1-p)
            new[k+1] += value*p
        row = new
    return row


def critical_wins(n, alpha=Fraction(1, 20)):
    alpha = Fraction(alpha)
    need(0 < alpha < 1, 'ALPHA_RANGE')
    row = masses(n, Fraction(1, 2))
    tail = sum(row)
    for k in range(n+1):
        if tail <= alpha:
            return k
        tail -= row[k]
    return n+1  # At very short horizons no non-randomized rejection is possible.


def read(path):
    def reject(value):
        raise ValueError('NONFINITE_JSON')
    def pairs(values):
        result = {}
        for key, value in values:
            need(key not in result, 'DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), parse_constant=reject, object_pairs_hook=pairs)


def validate_policy(p):
    need(p['schema'] == 'research_product_policy_v1' and
         p['scope'] == 'NEW_RESEARCH_DESIGN_ONLY', 'POLICY_SCOPE')
    for key in ('runtime_applied', 'market_research_authorized', 'reopen_closed_routes'):
        need(p[key] is False, 'NO_RUNTIME_OR_RESEARCH_AUTHORITY')
    need(p['stages'] == ['FROZEN_DEPLOYABLE_STRATEGY', 'INCREMENTAL_ONLINE_LEARNING'], 'STAGE_ORDER')
    d = p['development']
    need(d == dict(positive_baseline_required=False, negative_training_examples_allowed=True,
                  complete_scope_and_budget_required=True, hard_stopped_path_may_resume=False,
                  closed_candidate_may_resume=False) and all(type(v) is bool for v in d.values()), 'DEVELOPMENT_BOUNDARY')
    q = p['qualification']
    for key in ('candidate_base_and_stress_net_positive', 'learned_candidate_must_improve_frozen_reference',
                'independent_evidence_required', 'same_delivery_semantics_required',
                'actual_execution_required_before_deployment_qualification'):
        need(q[key] is True, 'QUALIFICATION_BOUNDARY')
    need(q['no_trade_is_success'] is False and q['default_confirmation_weeks'] is None and
         q['automatic_online_updates_required_for_first_stage'] is False, 'NO_FORCED_WAIT_OR_LEARNING')
    r = p['risk']
    need(r['research_drawdown_stop'] == r['per_position_liquidation_distance_floor'] == '0.08' and
         r['product_drawdown_actions'] == ['0.08', '0.12', '0.20'], 'HARD_RISK_CHANGED')
    for key in ('risk_fields_learnable', 'operator_lock_may_clear', 'existing_safety_latch_may_clear',
                'new_design_alert_auto_unfreezes'):
        need(r[key] is False, 'LOCK_OR_RISK_IMMUTABILITY')
    need(r['new_design_performance_alert'] == 'FREEZE_UPDATES_AND_REVIEW' and
         r['hard_risk_action'] == 'STOP_PATH_AND_UNWIND' and
         r['data_or_execution_unknown_action'] == 'BLOCK_NEW_RISK_AND_RECONCILE' and
         r['live_rules'] == 'UNCHANGED_UNTIL_SEPARATE_QUALIFIED_RELEASE', 'ACTION_SCOPE')
    need(set(p['next_market_scope_required']) == REQUIRED_SCOPE and
         len(p['next_market_scope_required']) == len(REQUIRED_SCOPE), 'COMPLETE_NEXT_SCOPE')
    c = p['synthetic_calibration']
    need(c['scope'] == 'STATISTICAL_COMPONENT_ONLY_NOT_MARKET_OR_FULL_STRATEGY_POWER' and
         c['sample_counts'] == [20, 50, 100, 200] and
         c['win_probabilities'] == ['0.4', '0.5', '0.6', '0.75'] and
         c['null_win_probability'] == '0.5' and c['one_sided_alpha'] == '0.05' and
         c['illustrative_power_target'] == '0.8' and
         c['selected_rule'] == 'FIXED_ENDPOINT_EXACT_BINOMIAL_NO_INTERIM_SUCCESS' and
         c['project_effect_size_estimate'] is None and c['project_confirmation_duration'] is None,
         'SYNTHETIC_NOT_MARKET_POWER')
    need(p['this_delivery'] == dict(maximum_engineering_hours=2, market_requests=0,
         economic_experiments=0, account_operations=0, deployments=0,
         automatic_new_research_budget=False), 'DELIVERY_SCOPE')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


def guard():
    need(dt.datetime.now(dt.timezone.utc) < DEADLINE, 'DELIVERY_BUDGET_EXHAUSTED')
    need(read(ROOT/'validation-state.json')['status'] == 'RUNNING', 'DESIGN_GATE_NOT_RUNNING')


def preserve():
    f = read(ROOT/'freeze.json')
    need(all(sha(p) == h for p, h in f['protected'].items()), 'OLD_EVIDENCE_CHANGED')
    need(all(sha(p) == h for p, h in f['sources'].items()), 'DESIGN_SOURCE_CHANGED')
    return len(f['protected'])


def freeze():
    guard()
    protected = dict(read(PRIOR/'freeze.json')['protected'])
    for name in ('freeze.json', 'model-freeze.json', 'baseline-result.json', 'baseline-audit.json',
                 'closed.json', 'publication.json', 'validation-state.json'):
        p = PRIOR/name
        protected[str(p)] = sha(p)
    for p, h in read(PRIOR/'model-freeze.json')['sources'].items():
        need(sha(p) == h, 'OLD_MODEL_CHANGED')
        protected[p] = h
    for p in Path('.artifacts').rglob('validation-state.json'):
        if p != ROOT/'validation-state.json':
            protected[str(p)] = sha(p)
    for p in (Path('.artifacts/validation-gate/state.json'), ROUND_TABLE,
              Path('docs/需求描述.md'), Path('docs/闭环收敛契约.md')):
        protected[str(p)] = sha(p)
    for name in ('aave_authenticated_batch.py', 'aave_authenticated_input.py',
                 'aave_complete_boundaries.py', 'test_aave_authenticated_batch.py',
                 'test_aave_authenticated_input.py', 'test_aave_complete_boundaries.py'):
        p = Path('tools')/name
        protected[str(p)] = sha(p)
    need(all(sha(p) == h for p, h in protected.items()), 'PRIOR_EVIDENCE_CHANGED')
    save(ROOT/'freeze.json', dict(protected=protected,
         sources={str(p): sha(p) for p in SOURCES},
         started_utc='2026-10-15T04:19:43+00:00', deadline_utc=DEADLINE.isoformat(),
         authorization='User accepted direction B; design only, no market research or live changes'))
    print(json.dumps(dict(status='DESIGN_SCOPE_FROZEN', protected_files=preserve())))


def calibrate():
    guard(); preserve()
    p = read(POLICY); validate_policy(p)
    c = p['synthetic_calibration']; rows = []
    for n in c['sample_counts']:
        k = critical_wins(n, Fraction(c['one_sided_alpha']))
        power = {}
        for prob in c['win_probabilities']:
            row = masses(n, prob)
            need(row == independent_masses(n, prob), 'INDEPENDENT_BINOMIAL_AUDIT')
            value = sum(row[k:], Fraction(0))
            power[prob] = dict(exact=str(value), probability=float(value))
        need(Fraction(power['0.5']['exact']) <= Fraction(c['one_sided_alpha']), 'NULL_FALSE_ADMISSION')
        need(Fraction(power['0.4']['exact']) <= Fraction(power['0.5']['exact']), 'NEGATIVE_CONTROL')
        if n == 50:
            need(Fraction(power['0.75']['exact']) >= Fraction(c['illustrative_power_target']), 'POSITIVE_CONTROL')
        rows.append(dict(blocks=n, critical_wins=k, admission_probabilities=power))
    result = dict(status='DESIGN_VALIDATED_ONLY', policy_sha256=sha(POLICY),
         protected_files=preserve(), independent_exact_probability_audit=True,
         calibration_scope=c['scope'], calibration=rows, market_experiments=0,
         candidate_count=0, g4='NOT_ACHIEVED', project_power=None,
         project_confirmation_duration=None, runtime_applied=False, deployment_permission=False)
    save(ROOT/'calibration.json', result)
    print(json.dumps(result, sort_keys=True))


def delivery():
    guard(); preserve(); validate_policy(read(POLICY))
    result = read(ROOT/'calibration.json')
    need(result['status'] == 'DESIGN_VALIDATED_ONLY' and result['policy_sha256'] == sha(POLICY), 'CALIBRATION_IDENTITY')
    for p in (DOC, REVIEW, ROUND_TABLE, Path('README.md'), Path('docs/CURRENT_STATE.md'), Path('AGENTS.md')):
        content = p.read_text()
        need(content.endswith('\n'), 'FINAL_NEWLINE')
        if p in (DOC, REVIEW, ROUND_TABLE):
            need(all(line == line.rstrip() for line in content.splitlines()), 'REPORT_WHITESPACE')
            for link in re.findall(r'\]\(([^)]+)\)', content):
                if not link.startswith(('http:', 'https:')):
                    need((p.parent/link.split('#')[0]).exists(), 'LOCAL_LINK_MISSING')
    for p in (Path('README.md'), Path('docs/CURRENT_STATE.md'), Path('AGENTS.md')):
        need('RESEARCH_PRODUCT_POLICY.md' in p.read_text(), 'AUTHORITY_ENTRY_MISSING')
    subprocess.run(['git', 'diff', '--check'], check=True)
    print(json.dumps(dict(status='DESIGN_DELIVERY_CHECKED', protected_files=preserve(), economic_admission=False)))


def publication(commit):
    guard(); preserve()
    need(re.fullmatch('[0-9a-f]{40}', commit) is not None, 'FULL_SHA_REQUIRED')
    def git(*args):
        return subprocess.check_output(['git', *args], text=True, timeout=30).strip()
    need(git('branch', '--show-current') == 'main' and git('rev-parse', 'HEAD') == commit and
         '[skip ci]' in git('show', '-s', '--format=%B', commit), 'RESEARCH_MAIN_IDENTITY')
    def api(endpoint, *fields):
        cmd = ['gh', 'api', '--method', 'GET', 'repos/skw2026/ai-trade/'+endpoint]
        for field in fields:
            cmd += ['-f', field]
        return json.loads(subprocess.check_output(cmd, text=True, timeout=45))
    branch = api('branches/main')
    runs = api('actions/runs', 'head_sha='+commit, 'per_page=100')
    deployments = api('deployments', 'sha='+commit, 'per_page=100')
    need(branch['commit']['sha'] == commit, 'REMOTE_MAIN_DIFFERS')
    need(runs['total_count'] == 0 and runs['workflow_runs'] == [] and deployments == [], 'UNEXPECTED_AUTOMATION')
    result = dict(status='EXACT_SHA_SKIP_CI_VERIFIED', sha=commit, actions=0, deployments=0,
                  verified_utc=dt.datetime.now(dt.timezone.utc).isoformat())
    save(ROOT/'publication.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'calibrate', 'delivery', 'publication'))
    parser.add_argument('--sha')
    args = parser.parse_args()
    if args.action == 'publication':
        publication(args.sha or '')
    else:
        globals()[args.action]()
