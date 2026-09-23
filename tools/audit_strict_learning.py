#!/usr/bin/env python3
"""TEST_ONLY strict-control diagnostic using a frozen, actually learned model.

This is deliberately not a full-mechanism PASS generator. --diagnose preserves
observations; --require-bounded-updates fails on candidate/step violations.
No training, network, registry writes, exchange fills or trading authority.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import sys

import verify_offline_learning_loop as loop

GRID = (0.4, 0.45, 0.5, 0.55, 0.6)
COST = 0.00065
FUNDING = 0.000025


def read_trace(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


def t_stat(values):
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = max(0.0, sum(v * v for v in values) / len(values) - mean * mean)
    if variance <= 1e-12:
        return 0.0
    return mean / math.sqrt(variance / len(values))


def strict_audit(records, interval):
    """Independent virtual-ledger reconstruction and causal selection audit."""
    loop.require(records, 'empty strict trace')
    train, hold, current = [], [], []
    windows, violations = [], []
    last_update = 0
    for offset, row in enumerate(records):
        tick = offset + 1
        before, after = float(row['before']), float(row['weight'])
        expected_delayed = float(records[offset - 1]['model_signal']) if offset else 0.0
        loop.require(abs(float(row['delayed_signal']) - expected_delayed) < 1e-10,
                     'model signal latency mismatch')
        if offset:
            prev = records[offset - 1]
            loop.require(abs(before - float(prev['weight'])) < 1e-10, 'weight discontinuity')
            old_signal, new_signal = float(prev['delayed_signal']), float(row['delayed_signal'])
            unit = (old_signal * (float(row['price']) / float(prev['price']) - 1)
                    - old_signal * FUNDING - abs(new_signal - old_signal) * COST)
            # Defensive notional is zero in this declared virtual-control probe.
            sample = {w: w * unit for w in GRID}
            window_tick = (tick - 1) % interval + 1
            (train if window_tick <= math.ceil(interval * 0.7) else hold).append(sample)
            current.append(before * unit)
        loop.require((row['action'] != 'none') == (tick % interval == 0),
                     'strict assessment cadence mismatch')
        loop.require(0.4 - 1e-9 <= after <= 0.6 + 1e-9, 'strict weight cap')
        if row['action'] == 'none':
            loop.require(abs(after - before) < 1e-10, 'unattributed strict update')
            continue
        loop.require(row['used_search'] == row['strict'] == '1' and row['fallback'] == '0',
                     'strict guard or fallback bypass')
        loop.require(int(row['train_samples']) == len(train)
                     and int(row['holdout_samples']) == len(hold), 'strict sample counts')
        loop.require(abs(float(row['virtual_pnl']) - sum(current)) < 1e-7,
                     'strict virtual ledger mismatch')
        eligible = [w for w in GRID if abs(w - before) <= 0.05 + 1e-9]
        scores = {w: sum(s[w] for s in train) for w in GRID}
        best = max(eligible, key=lambda w: scores[w])
        full_best = max(GRID, key=lambda w: scores[w])
        selected = float(row['best'])
        # Retain both explanations for an unmodified-source diagnosis.
        bounded_selection = abs(selected - best) < 1e-8
        full_selection = abs(selected - full_best) < 1e-8
        loop.require(bounded_selection or full_selection, 'unexplained candidate selection')
        if not bounded_selection:
            violations.append({'tick': tick, 'reason': 'UNBOUNDED_SELECTION',
                               'before': before, 'selected': selected, 'bounded_best': best})
        matched = min(GRID, key=lambda w: abs(w - selected))
        diffs = [s[matched] - before * s[matched] / matched for s in hold]
        enough = len(train) >= 10 and len(hold) >= 10
        if enough:
            loop.require(int(row['hold_n']) == len(hold)
                         and abs(float(row['hold_t']) - t_stat(diffs)) < 1e-6,
                         'holdout statistics mismatch')
        else:
            loop.require(row['action'] == 'EVOLUTION_COUNTERFACTUAL_HOLDOUT_INSUFFICIENT'
                         and abs(after - before) < 1e-10, 'insufficient holdout did not freeze')
        updated = int(row['action_type']) == 0
        if updated:
            loop.require(enough and row['learn_pass'] == row['superiority_pass'] == '1'
                         and int(row['learn_samples']) >= 120
                         and abs(float(row['learn_t'])) >= 1.5
                         and sum(diffs) > 0 and t_stat(diffs) >= 1.5,
                         'update escaped strict evidence gates')
            loop.require(abs(after - selected) < 1e-8, 'updated a different unvalidated candidate')
            loop.require(tick >= 72 and tick - last_update >= 72, 'strict update rate limit')
            last_update = tick
            if abs(after - before) > 0.05 + 1e-9:
                violations.append({'tick': tick, 'reason': 'MAX_WEIGHT_STEP_EXCEEDED',
                                   'before': before, 'after': after})
        else:
            loop.require(abs(after - before) < 1e-10, 'skipped action changed weights')
        windows.append({'tick': tick, 'action': row['action'], 'before': before, 'after': after,
                        'selected': selected, 'bounded_best': best,
                        'train_samples': len(train), 'holdout_samples': len(hold),
                        'holdout_delta': sum(diffs), 'holdout_t': t_stat(diffs),
                        'virtual_pnl': sum(current), 'updated': updated,
                        'rollback': row['rolled_back'] == '1'})
        train, hold, current = [], [], []
    return {'windows': windows, 'violations': violations,
            'updates': sum(w['updated'] for w in windows),
            'rollbacks': sum(w['rollback'] for w in windows),
            'independent_virtual_ledger_matched': True}


def exposure_attribution(root):
    """Pair actual old fixture decisions; never call equal notional equal risk."""
    loop.require(loop.sha(root / 'positive.csv') == loop.sha(root / 'adaptive.csv'),
                 'unpaired market path')
    arms = {}
    for name in ('positive', 'adaptive'):
        # Original CSV values, not a fresh generator or a changed model.
        with (root / f'{name}.csv').open() as stream:
            bars = [tuple(float(r[f]) for f in loop.FIELDS) for r in csv.DictReader(stream)]
        summary, records = loop.summarize(root / f'{name}.trace.csv', bars, 5.5)
        episodes = {}
        normalized = 0.0
        exposure = 0.0
        for r in records:
            if r['applied'] != '1':
                continue
            i, direction = int(r['index']), int(r['direction'])
            weight = float(r['weight'])
            loop.require(weight > 0, 'zero exposure in attribution')
            qty = 40 / bars[i + 1][4]
            entry = bars[i + 1][4] * (1 + direction * 0.0001)
            end = bars[i + 2][4] * (1 - direction * 0.0001)
            net = direction * qty * (end - entry) - qty * (entry + end) * 0.00055
            net -= direction * qty * bars[i + 2][4] * 0.000025
            loop.require(i not in episodes, 'duplicate episode')
            episodes[i] = (direction, net)
            normalized += net
            exposure += 80 * weight
        arms[name] = {'raw_net': summary['net'], 'fixed_40_notional_net': normalized,
                      'entry_notional_sum': exposure, 'episodes': episodes}
    a, b = arms['positive'], arms['adaptive']
    loop.require(a['episodes'].keys() == b['episodes'].keys(), 'unpaired episode times')
    loop.require(all(a['episodes'][i][0] == b['episodes'][i][0] for i in a['episodes']),
                 'unpaired episode directions')
    fixed_delta = b['fixed_40_notional_net'] - a['fixed_40_notional_net']
    raw_delta = b['raw_net'] - a['raw_net']
    return {'paired_episodes': len(a['episodes']), 'raw_net_delta': raw_delta,
            'fixed_notional_net_delta': fixed_delta,
            'exposure_scaling_component': raw_delta - fixed_delta,
            'attribution_residual': raw_delta - (raw_delta - fixed_delta) - fixed_delta,
            'verdict': 'EXPOSURE_SCALING_ONLY' if abs(fixed_delta) < 1e-9 else 'RESIDUAL_REQUIRES_ANALYSIS',
            'equal_risk_proven': False, 'market_alpha_proven': False,
            'arms': {n: {k: v for k, v in arm.items() if k != 'episodes'} for n, arm in arms.items()}}


def audit(driver, learning_result, output):
    root = learning_result.parent
    report = json.loads(learning_result.read_text())
    loop.require(report['status'] == 'PASS' and report['scope'] == 'TEST_ONLY_OFFLINE_COMPONENT_INTEGRATION'
                 and report['production_promotion_authority'] is False, 'untrusted learning scope')
    frozen = {}
    for group in ('training_artifact_sha256', 'artifact_sha256'):
        for name, digest in report[group].items():
            path = root / name
            loop.require(path.resolve().is_relative_to(root.resolve()) and loop.sha(path) == digest,
                         'frozen learning artifact changed:' + name)
            frozen[name] = digest
    output.mkdir(parents=True, exist_ok=False)
    version = json.loads((root / 'learned.json').read_text())['model_version']
    inputs = [('positive', root / 'positive.csv', 240), ('short', root / 'positive.csv', 12),
              ('drift', root / 'drift.csv', 240)]
    # Perturb only the holdout/future of the first selection window, preserving
    # every earlier OHLCV byte value. Real model inference still supplies signals.
    with (root / 'positive.csv').open() as stream:
        original = [tuple(float(r[f]) for f in loop.FIELDS) for r in csv.DictReader(stream)]
    changed = [(int(r[0]), *r[1:]) for r in original[:loop.START + 168]]
    for i in range(len(changed), len(original)):
        opening = changed[-1][4]
        closing = opening * (2 - original[i][4] / original[i - 1][4])
        changed.append((int(original[i][0]), opening, max(opening, closing) * 1.0001,
                        min(opening, closing) * 0.9999, closing, original[i][5]))
    perturbed = output / 'holdout_reversal.csv'
    loop.write_csv(perturbed, changed)
    inputs.append(('holdout_reversal', perturbed, 240))
    cases, traces = {}, {}
    for name, csv_path, interval in inputs:
        trace = output / f'{name}.trace.csv'
        loop.run_driver(driver, ['strict', root / 'learned.json', root / 'learned.cbm', csv_path,
                                loop.START, interval, version, trace], output / f'{name}.log')
        traces[name] = read_trace(trace)
        cases[name] = strict_audit(traces[name], interval)
    p, r = traces['positive'], traces['holdout_reversal']
    loop.require(p[:168] == r[:168], 'future changed pre-holdout inference/control')
    first_p, first_r = cases['positive']['windows'][0], cases['holdout_reversal']['windows'][0]
    loop.require(first_p['selected'] == first_r['selected'], 'holdout contaminated candidate selection')
    loop.require(not first_r['updated'] and first_r['holdout_delta'] < 0,
                 'adverse holdout did not reject selected candidate')
    loop.require(cases['positive']['updates'] > 0 and cases['short']['updates'] == 0,
                 'positive path or insufficient-sample freeze not exercised')
    loop.require(all(loop.sha(root / n) == h for n, h in frozen.items()), 'original inputs mutated')
    evidence = {'schema_version': 'strict_learning_diagnostic_v1',
                'scope': 'TEST_ONLY_STRICT_LEARNING_DIAGNOSIS',
                'status': 'OBSERVED_NOT_FULL_MECHANISM_ACCEPTANCE',
                'production_promotion_authority': False, 'market_economic_evidence': False,
                'full_mechanism_valid': False, 'demo_trading_authority': False,
                'learning_result_sha256': loop.sha(learning_result),
                'model_sha256': frozen['learned.cbm'], 'driver_sha256': loop.sha(driver),
                'verifier_sha256': loop.sha(pathlib.Path(__file__)), 'cases': cases,
                'holdout_causal_rejection_proven': True, 'original_inputs_unchanged': True,
                'exposure_attribution': exposure_attribution(root),
                'limits': ['virtual control ledger, not fills or live strategy',
                           '240-bar probe is not the deployed hourly profile',
                           'strict rollback must be separately adjudicated; never inferred from legacy mode'],
                'artifact_sha256': {p.name: loop.sha(p) for p in output.iterdir() if p.is_file()}}
    loop.write_json(output / 'diagnostic.json', evidence)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', required=True, type=pathlib.Path)
    parser.add_argument('--learning-result', required=True, type=pathlib.Path)
    parser.add_argument('--output', required=True, type=pathlib.Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--diagnose', action='store_true')
    mode.add_argument('--require-bounded-updates', action='store_true')
    args = parser.parse_args()
    try:
        evidence = audit(args.driver.resolve(), args.learning_result.resolve(), args.output.resolve())
        violations = sum(len(c['violations']) for c in evidence['cases'].values())
        print(json.dumps({'observed': True, 'step_or_selection_violations': violations,
                          'exposure': evidence['exposure_attribution'], 'full_mechanism_valid': False}))
        if args.require_bounded_updates:
            loop.require(violations == 0, 'strict candidate selection/update violated max_weight_step')
        return 0
    except Exception as error:
        print('STRICT_LEARNING_AUDIT_FAIL: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
