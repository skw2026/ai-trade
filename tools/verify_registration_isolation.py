#!/usr/bin/env python3
"""TEST_ONLY real-artifact rejection and isolated transaction fault injection.

Never runs the production runner, services, accounts, or a live registry. A
rejected model does NOT advance into CANARY: evaluator states below are separate
fault injections, not evidence that registration or production promotion passed.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import evaluate_activation_transaction as activation
from evolution_safety_evidence import extract as extract_safety

ROOT = Path(__file__).resolve().parents[1]
SCOPE = 'TEST_ONLY_REGISTRATION_ISOLATION'
FILES = ('learned.cbm', 'learned.json', 'shuffled.cbm', 'shuffled.json', 'miner.json', 'development.csv')
POLICY = {'schema_version': activation.ACTIVATION_POLICY_SCHEMA,
          'min_complete_episodes': 30, 'min_positive_episode_ratio': 0.5,
          'min_mean_realized_net_per_fill_usd': 0.0, 'max_pending_hours': 72.0}
ORIGIN = dt.datetime(2026, 9, 23, tzinfo=dt.timezone.utc)


def require(ok, reason):
    if not ok:
        raise AssertionError(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


def inputs(path):
    receipt = read(path)
    require(receipt['status'] == 'PASS' and receipt['scope'] == 'TEST_ONLY_OFFLINE_COMPONENT_INTEGRATION',
            'learning receipt not a passing TEST_ONLY integration')
    for key in ('production_promotion_authority', 'market_economic_evidence',
                'demo_trading_authority', 'full_mechanism_valid'):
        require(receipt[key] is False, 'learning authority invalid: ' + key)
    root = path.parent
    hashes = {name: sha(root / name) for name in FILES}
    require(all(receipt['training_artifact_sha256'][name] == digest for name, digest in hashes.items()),
            'learning artifact hash mismatch')
    report = read(root / 'learned.json')
    require(report.get('test_only') is True and report.get('production_promotion_authority') is False
            and report.get('governance', {}).get('pass') is False, 'synthetic authority markers missing')
    require(report['model_sha256'] == hashes['learned.cbm']
            and report['data']['training_csv_sha256'] == hashes['development.csv'], 'training identity mismatch')
    require(report['data']['source_venue'] == 'synthetic' and report['data']['source_category'] == 'test_only',
            'synthetic source relabeled')
    require(report['model_version'].startswith('TEST_ONLY_'), 'candidate version missing TEST_ONLY identity')
    return receipt, report, hashes


def file_set(root):
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob('*')) if p.is_file()}


def confined_artifact(path, root):
    artifact = Path(path).resolve()
    require(artifact.is_relative_to(root.resolve()), 'registry artifact escaped isolated root')
    return artifact


def registration(source, output, with_previous):
    case = output / ('registry_with_previous' if with_previous else 'registry_empty')
    case.mkdir()
    active = case / 'active'
    active.mkdir()
    targets = {name: active / name for name in ('model.cbm', 'report.json', 'miner.json', 'meta.json')}
    if with_previous:
        for target, original in (('model.cbm', 'shuffled.cbm'), ('report.json', 'shuffled.json'),
                                 ('miner.json', 'miner.json')):
            shutil.copyfile(source / original, targets[target])
        write(targets['meta.json'], {'scope': SCOPE, 'previous_model_sha256': sha(source / 'shuffled.cbm')})
    before = file_set(active)
    result_path = case / 'registration.json'
    argv = [sys.executable, str(ROOT / 'tools/model_registry.py'), 'register',
            '--model_file', str(source / 'learned.cbm'), '--integrator_report', str(source / 'learned.json'),
            '--miner_report', str(source / 'miner.json'), '--registry_dir', str(case / 'registry'),
            '--active_model_path', str(targets['model.cbm']), '--active_report_path', str(targets['report.json']),
            '--active_miner_report_path', str(targets['miner.json']), '--active_meta_path', str(targets['meta.json']),
            '--activate_on_pass', '--registration_out', str(result_path)]
    result = subprocess.run(argv, cwd=case, capture_output=True, text=True, timeout=60)
    with (case / 'registry.log').open('x') as log:
        log.write(result.stdout + result.stderr)
    require(result.returncode == 3,
            f'registry must reject TEST_ONLY with exit 3; got {result.returncode}: {result.stderr[-1600:]}')
    entry = read(result_path)
    require(entry['gate']['pass'] is False and entry['activated'] is False, 'synthetic model activated')
    for reason in ('integrator_report.governance.pass != true', 'data.source_venue != bybit',
                   'activation_transaction is required for activate_on_pass'):
        require(reason in entry['gate']['fail_reasons'], 'expected production rejection missing: ' + reason)
    for label, filename, checksum in (('model_file', 'learned.cbm', 'model_sha256'),
                                     ('integrator_report', 'learned.json', 'integrator_report_sha256'),
                                     ('miner_report', 'miner.json', 'miner_report_sha256')):
        artifact = confined_artifact(entry['artifacts'][label], case / 'registry')
        require(sha(artifact) == sha(source / filename) == entry['checksums'][checksum],
                'registered rejection identity mismatch: ' + label)
    index = read(case / 'registry/index.json')
    require(len(index) == 1 and index[0] == entry, 'registry index not bound to rejected entry')
    require(before == file_set(active), 'rejected registration changed active artifacts')
    return {'decision': 'REJECTED', 'activated': False, 'with_previous_active': with_previous,
            'active_files_unchanged': True, 'active_file_count': len(before),
            'checksums': entry['checksums'], 'registration_receipt_sha256': sha(result_path)}


def transaction(source, output, metadata, fault, driver_sha256):
    case = output / ('transaction_' + fault)
    case.mkdir()
    artifacts = {}
    for name, original in (('model', 'learned.cbm'), ('report', 'learned.json'), ('miner_report', 'miner.json')):
        path = case / original
        shutil.copyfile(source / original, path)
        artifacts[name] = {'path': str(path), 'sha256': sha(path)}
    path = case / 'meta.json'
    write(path, {'scope': SCOPE, 'fault_injection_not_registered': True, 'model_version': metadata['model_version']})
    artifacts['active_meta'] = {'path': str(path), 'sha256': sha(path)}
    identity = {'model_sha256': artifacts['model']['sha256'], 'report_sha256': artifacts['report']['sha256'],
                'runtime_config_sha256': sha(ROOT / 'config/bybit.replay.mvp.yaml'),
                'trade_bot_sha256': driver_sha256}
    # Config and offline driver hashes below are fixture bindings, not live
    # runtime/binary attestations. No fabricated live episode is supplied.
    state = {'schema_version': 'closed_loop_activation_transaction_v2', 'scope': SCOPE,
             'production_promotion_authority': False, 'run_id': 'TEST_ONLY_' + fault,
             'status': 'canary_pending_evidence', 'created_at_utc': activation.utc_iso(ORIGIN),
             'activation_policy': copy.deepcopy(POLICY), 'activation_policy_sha256': activation.canonical_sha256(POLICY),
             'candidate': {'model_version': metadata['model_version'], 'identity': identity, 'artifacts': artifacts,
                           'training_symbol': metadata['data']['training_symbol'],
                           'bar_interval_ms': metadata['data']['bar_interval_ms']}}
    expected = activation.candidate_identity(state)
    metrics = {key: 0 for key in activation.HARD_SAFETY_METRICS}
    metrics.update({'integrator_model_version_latest': expected['model_version'],
                    'integrator_model_sha256_latest': expected['model_sha256'],
                    'integrator_report_sha256_latest': expected['report_sha256'],
                    'integrator_runtime_config_sha256_latest': expected['runtime_config_sha256'],
                    'integrator_trade_bot_sha256_latest': expected['trade_bot_sha256'],
                    'integrator_feature_training_symbol_latest': expected['training_symbol'],
                    'integrator_feature_bar_interval_ms_latest': int(expected['bar_interval_ms']),
                    'runtime_boot_id_latest': 'TEST_ONLY_BOOT', 'integrator_policy_filled_candidate_ids': [],
                    'integrator_policy_closed_episode_events': []})
    now = ORIGIN + dt.timedelta(hours=1)
    if fault == 'runtime_identity':
        metrics['integrator_model_sha256_latest'] = '0' * 64
    elif fault == 'staged_identity':
        state['candidate']['identity']['report_sha256'] = '0' * 64
    elif fault == 'artifact_corruption':
        with Path(artifacts['model']['path']).open('ab') as stream:
            stream.write(b'TEST_ONLY_CORRUPTION')
    elif fault == 'deadline':
        now = ORIGIN + dt.timedelta(hours=73)
    else:
        require(fault == 'zero_episodes', 'unknown fault injection')
    write(case / 'state-before.json', state)
    runtime = {'verdict': 'PASS', 'metrics': metrics, 'evolution_safety': extract_safety(
        'RUNTIME_STATUS: evolution_safety_withdrawn=false, boot={id=TEST_ONLY_BOOT}, '
        'evolution_safety_identity={runtime_config_sha256=' + identity['runtime_config_sha256']
        + ', trade_bot_sha256=' + identity['trade_bot_sha256'] + '}')}
    safety_cases = safety_interlock_cases(state, runtime, now) if fault == 'zero_episodes' else None
    result = activation.evaluate(state, runtime, mechanism={}, now=now,
        min_complete_episodes=30, min_positive_episode_ratio=0.5,
        min_mean_realized_net_per_fill_usd=0.0, max_pending_hours=72.0)
    decision = 'pending' if fault == 'zero_episodes' else 'rollback'
    require(result['decision'] == decision, 'transaction decision mismatch: ' + fault)
    require(result['evidence']['complete_episode_count'] == 0, 'fabricated live episode')
    reasons = {'runtime_identity': 'runtime four-part/model feature identity mismatch',
               'staged_identity': 'candidate report identity differs from staged artifact',
               'artifact_corruption': 'active candidate artifact hash mismatch: model',
               'deadline': 'canary validation deadline exceeded'}
    if fault == 'zero_episodes':
        require(result['identity_match'] is True and not result['hard_fail_reasons'], 'valid zero sample not pending')
    else:
        require(any(reasons[fault] in r for r in result['hard_fail_reasons']), 'wrong rollback reason: ' + fault)
    write(case / 'decision.json', result)
    write(case / 'state-after.json', state)
    return {'decision': decision, 'fault': fault, 'complete_episode_count': 0,
            'input_model_sha256': artifacts['model']['sha256'], 'input_report_sha256': artifacts['report']['sha256'],
            'decision_sha256': sha(case / 'decision.json'), 'live_runtime_observed': False,
            'rollback_service_executed': False, 'safety_interlock': safety_cases}


def safety_interlock_cases(state, runtime, now):
    """Synthetic positive economics are ONLY a counterfactual refusal test."""
    expected = activation.candidate_identity(state)
    clean = copy.deepcopy(runtime)
    clean['metrics']['integrator_policy_closed_episode_events'] = [
        {'position_episode_id': 'TEST_ONLY_EPISODE_' + str(i),
         'candidate_id': expected['model_version'], 'model_version': expected['model_version'],
         'mode': 'canary', 'policy_reason': 'canary_independent_signal',
         'symbol': expected['training_symbol'], 'realized_net_usd': 0.1, 'funding_paid_usd': 0.0,
         'fill_event_count': 2, 'unique_order_count': 2, 'evidence_complete': True,
         'activation_transaction_id': state['run_id'], 'evidence_boot_id': 'TEST_ONLY_BOOT',
         'runtime_config_sha256': expected['runtime_config_sha256'],
         'trade_bot_sha256': expected['trade_bot_sha256'], 'closed_at_utc': activation.utc_iso(now),
         'recovered_after_restart': False} for i in range(30)]
    kwargs = dict(mechanism={'status': 'pass'}, now=now, min_complete_episodes=30,
                  min_positive_episode_ratio=0.5, min_mean_realized_net_per_fill_usd=0.0, max_pending_hours=72.0)
    outcomes = {}
    for fault in ('clear_control', 'withdrawn', 'missing', 'malformed', 'boot', 'config', 'binary'):
        case, observed = copy.deepcopy(state), copy.deepcopy(clean)
        proof = observed['evolution_safety']
        if fault == 'withdrawn':
            proof = extract_safety('EVOLUTION_SAFETY_WITHDRAWAL_LATCHED: TEST_ONLY\n')
            observed['evolution_safety'] = proof
        elif fault == 'missing':
            observed.pop('evolution_safety')
        elif fault == 'malformed':
            proof['withdrawn_count'] = False
        elif fault in ('boot', 'config', 'binary'):
            proof[{'boot': 'boot_id', 'config': 'runtime_config_sha256', 'binary': 'trade_bot_sha256'}[fault]] = 'wrong'
        result = activation.evaluate(case, observed, **kwargs)
        require(result['decision'] == ('commit' if fault == 'clear_control' else 'rollback'),
                'safety positive-economics refusal mismatch: ' + fault)
        repeat = activation.evaluate(json.loads(json.dumps(case)), clean, **kwargs)
        require(repeat['decision'] == result['decision'], 'safety refusal was automatically cleared: ' + fault)
        outcomes[fault] = {'decision': result['decision'], 'repeat_decision': repeat['decision'],
                           'reason_codes': result['hard_fail_reasons'], 'synthetic_complete_episodes': 30}
    return {'scope': 'TEST_ONLY_SYNTHETIC_POSITIVE_ECONOMICS_REFUSAL', 'status': 'PASS',
            'cases': outcomes, 'live_runtime_observed': False, 'production_promotion_authority': False}


def verify(learning_path, output):
    learning_path, output = learning_path.resolve(), output.resolve()
    source = learning_path.parent
    require(not output.exists() and not output.is_relative_to(source) and not source.is_relative_to(output),
            'output must be a new isolated sibling, never learning input/ancestor')
    receipt, metadata, hashes = inputs(learning_path)
    from catboost import CatBoostClassifier
    model = CatBoostClassifier()
    model.load_model(str(source / 'learned.cbm'))
    require(model.tree_count_ > 0, 'real CatBoost model has no trees')
    output.mkdir(parents=True)
    registration_cases = [registration(source, output, previous) for previous in (False, True)]
    transactions = {fault: transaction(source, output, metadata, fault, receipt['driver_sha256']) for fault in
                    ('zero_episodes', 'runtime_identity', 'staged_identity', 'artifact_corruption', 'deadline')}
    require(hashes == {name: sha(source / name) for name in FILES}, 'original training input modified')
    report = {'schema_version': 'registration_isolation_v1', 'status': 'PASS', 'scope': SCOPE,
              'verification_git_sha': os.environ.get('VERIFICATION_SHA', 'uncommitted_local'),
              'learning_verification_git_sha': receipt['verification_git_sha'], 'learning_report_sha256': sha(learning_path),
              'input_sha256': hashes, 'model_version': metadata['model_version'], 'model_tree_count': model.tree_count_,
              'registration_cases': registration_cases, 'transaction_fault_injections': transactions,
              'original_inputs_unchanged': True, 'production_registry_written': False,
              'production_promotion_authority': False, 'demo_trading_authority': False,
              'market_economic_evidence': False, 'full_mechanism_valid': False,
              'limits': ['fault-injected states are not successors of rejected registration',
                         'no live canary or production promotion', 'transaction evaluator decisions, not live service rollback',
                         'runtime identity uses an offline driver and fixture config, not live attestations'],
              'source_sha256': {name: sha(ROOT / 'tools' / name) for name in
                               ('verify_registration_isolation.py', 'model_registry.py', 'evaluate_activation_transaction.py',
                                'evolution_safety_evidence.py')},
              'artifact_sha256': file_set(output)}
    write(output / 'registration-result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--learning-result', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.learning_result, args.output)
    except Exception as error:
        print('REGISTRATION_ISOLATION_FAIL: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps({'status': result['status'], 'scope': SCOPE, 'production_registry_written': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
