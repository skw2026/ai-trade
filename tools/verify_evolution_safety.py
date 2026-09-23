#!/usr/bin/env python3
"""TEST_ONLY frozen-model safety proof; no training or account authority."""
import argparse
import json
from pathlib import Path

import audit_strict_learning as strict
import verify_offline_learning_loop as loop


def audit(records, interval):
    loop.require(bool(records), 'empty safety trace')
    current = []
    losses = 0
    withdrawal = None
    windows = []
    for offset, row in enumerate(records):
        before, after = float(row['before']), float(row['weight'])
        delayed = float(records[offset - 1]['model_signal']) if offset else 0.0
        loop.require(abs(float(row['delayed_signal']) - delayed) < 1e-10, 'safety signal latency')
        loop.require(row['rolled_back'] == '0', 'unsafe baseline restoration')
        if withdrawal is not None:
            loop.require(row['safety_withdrawn'] == '1' and row['action'] == 'none'
                         and before == after == withdrawal['weight'], 'withdrawal unlocked learning')
            continue
        if offset:
            prev = records[offset - 1]
            loop.require(abs(before - float(prev['weight'])) < 1e-10, 'weight continuity')
            old, new = float(prev['delayed_signal']), float(row['delayed_signal'])
            current.append(before * (old * (float(row['price']) / float(prev['price']) - 1)
                           - old * strict.FUNDING - abs(new - old) * strict.COST))
        tick = offset + 1
        loop.require((row['action'] != 'none') == (tick % interval == 0), 'safety cadence')
        if row['action'] == 'none':
            loop.require(row['safety_withdrawn'] == '0' and before == after, 'early or unexplained action')
            continue
        pnl = sum(current)
        loop.require(abs(float(row['virtual_pnl']) - pnl) < 1e-7, 'safety independent virtual ledger')
        # Fixed fixture: equity=10000, no actual drawdown/notional churn, alpha=1.
        loop.require(abs(float(row['objective']) - pnl) < 1e-7, 'safety objective')
        losses = losses + 1 if current and pnl < 0 else 0
        expected = losses >= 2
        loop.require((row['action'] == 'EVOLUTION_SAFETY_WITHDRAWAL_LATCHED') == expected
                     and (row['safety_withdrawn'] == '1') == expected, 'wrong withdrawal boundary')
        windows.append({'tick': tick, 'virtual_pnl': pnl, 'loss_streak': losses,
                        'withdrawn': expected})
        if expected:
            loop.require(row['action_type'] == '3' and after == before, 'withdrawal changed weights')
            withdrawal = {'tick': tick, 'weight': after}
            # All ordinary decisions before the safety event must still pass
            # the unchanged independent strict candidate/holdout verifier.
            strict.strict_audit(records[:offset - interval + 1], interval)
        current = []
    if withdrawal is None:
        strict.strict_audit(records, interval)
    return {'windows': windows, 'withdrawal': withdrawal,
            'post_withdrawal_observations': len(records) - withdrawal['tick'] if withdrawal else 0,
            'independent_virtual_ledger_matched': True}


def verify(driver, root, output):
    root, output = Path(root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((root / 'learned.json').read_text())
    loop.require(metadata['test_only'] is True and metadata['governance']['pass'] is False,
                 'not test-only model')
    model_hash = loop.sha(root / 'learned.cbm')
    results = {}
    for name, source, interval in [('positive', 'positive', 240), ('drift', 'drift', 240),
                                    ('short', 'positive', 12)]:
        trace = output / f'safety-{name}.trace.csv'
        log = output / f'safety-{name}.log'
        loop.require(not trace.exists() and not log.exists(), 'preserve earlier safety artifacts')
        loop.run_driver(driver, ['safety', root / 'learned.json', root / 'learned.cbm',
                        root / f'{source}.csv', loop.START, interval, metadata['model_version'], trace], log)
        results[name] = audit(strict.read_trace(trace), interval)
    loop.require(results['positive']['withdrawal'] is None, 'positive fixture withdrawn')
    loop.require(results['drift']['withdrawal'] is not None
                 and results['drift']['post_withdrawal_observations'] > 0, 'drift did not exercise latch')
    loop.require(loop.sha(root / 'learned.cbm') == model_hash, 'frozen model modified')
    return {'status': 'PASS', 'scope': 'TEST_ONLY_MODEL_SAFETY_CONTROL',
            'model_sha256': model_hash, 'cases': results,
            'production_promotion_authority': False, 'market_economic_evidence': False,
            'demo_trading_authority': False, 'full_mechanism_valid': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--driver', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.driver, args.root, args.output)
    loop.write_json(args.output / 'safety-result.json', result)
    print(json.dumps(result))
