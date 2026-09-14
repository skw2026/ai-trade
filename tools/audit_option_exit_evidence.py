#!/usr/bin/env python3
"""Classify frozen research-ledger exit quotes without inventing liquidity.

L1 capacity is not full-book capacity or evidence of an actual failed exit.
Neighbour observations bracket observations, not the duration of an outage.
"""
from __future__ import annotations
import argparse
from collections import Counter
from decimal import Decimal, localcontext
import pathlib

import audit_bybit_readonly_evidence as wire
import audit_option_subaccount_ledger as ledger

require = wire.require


def _audit(data):
    baseline = ledger.audit(ledger.SCOPE, data)
    require(baseline['status'] != 'TECHNICALLY_INVALID', 'EXIT_SOURCE_LEDGER_INVALID')
    identity = wire.digest(ledger.canonical(data))
    positions, observations, findings = {}, {}, []
    observed_bad_seqs = set()
    for event, checkpoint in zip(data['events'], baseline['checkpoints']):
        now, kind = event['ts_ms'], event['type']
        if kind == 'FILL':
            symbol = event['symbol']
            positions[symbol] = positions.get(symbol, Decimal(0)) + Decimal(event['signed_qty_btc'])
        elif kind == 'DELIVERY':
            positions.pop(event['symbol'])
        for symbol, quote in event['valuation'].items():
            qty = positions[symbol]
            side = 'bid' if qty > 0 else 'ask'
            price, size = Decimal(quote[side]), Decimal(quote[side + '_size'])
            reasons = []
            if price == 0: reasons.append('ZERO_EXIT_PRICE')
            if size == 0: reasons.append('ZERO_EXIT_SIZE')
            elif size < abs(qty): reasons.append('L1_SIZE_BELOW_POSITION')
            expiry = data['instruments'][symbol].get('expiry_ts_ms')
            pending_delivery = expiry == now and any(e['seq'] > event['seq'] and
                e['ts_ms'] == now and e['type'] == 'DELIVERY' and e['symbol'] == symbol for e in data['events'])
            observation = {'seq': event['seq'], 'ts_ms': now, 'quote_ts_ms': quote['ts_ms'],
                'symbol': symbol, 'position_qty_btc': format(qty, 'f'), 'exit_side': side,
                'price_usdt_per_btc': format(price, 'f'), 'l1_size_btc': format(size, 'f'),
                'has_full_l1_exit': not reasons, 'same_timestamp_delivery_pending': pending_delivery}
            observations.setdefault(symbol, []).append(observation)
            if reasons:
                observed_bad_seqs.add(event['seq'])
                findings.append({**observation, 'reasons': reasons,
                    'classification': 'DELIVERY_ORDERING_REVIEW' if pending_delivery else 'OBSERVED_L1_EXIT_CONSTRAINT',
                    'required_qty_btc': format(abs(qty), 'f'),
                    'shortfall_btc': format(max(Decimal(0), abs(qty) - size), 'f'),
                    'l1_capacity_fraction': format(min(Decimal(1), size / abs(qty)), 'f') if price > 0 else '0',
                    'ledger_risk_reasons_at_checkpoint': checkpoint['risk_reasons'],
                    'ledger_exit_latched': checkpoint['exit_review_latched']})
    require(observed_bad_seqs == {i for i, c in enumerate(baseline['checkpoints']) if not c['exit_bbo_qualified']},
            'EXIT_CLASSIFICATION_DIFFERS_FROM_LEDGER')
    for finding in findings:
        same = observations[finding['symbol']]
        before = [o for o in same if o['ts_ms'] < finding['ts_ms'] and o['has_full_l1_exit']]
        after = [o for o in same if o['ts_ms'] > finding['ts_ms'] and o['has_full_l1_exit']]
        # Same-timestamp duplicate events are not independent recovery evidence.
        finding['previous_full_l1_observation'] = before[-1] if before else None
        finding['next_full_l1_observation'] = after[0] if after else None
        finding['neighbour_bracket_ms'] = after[0]['ts_ms'] - before[-1]['ts_ms'] if before and after else None
        finding['continuous_outage_duration_ms'] = None
    require(wire.digest(ledger.canonical(data)) == identity, 'EXIT_SOURCE_MUTATED')
    return {'schema_version': 'option_exit_evidence_v1', 'status': 'EXIT_CONSTRAINTS_CLASSIFIED' if findings else 'OBSERVED_L1_CHECKS_PASS',
        'ledger_canonical_sha256': identity, 'ledger_events': len(data['events']),
        'unqualified_checkpoint_count': len(observed_bad_seqs), 'symbol_checkpoint_findings': len(findings),
        'distinct_bad_quote_observations': len({(r['symbol'], r['quote_ts_ms'], r['price_usdt_per_btc'], r['l1_size_btc']) for r in findings}),
        'classification_counts': dict(Counter(r['classification'] for r in findings)), 'findings': findings,
        'source_ledger_unchanged': True, 'full_orderbook_capacity_known': False,
        'actual_exit_failure_proven': False, 'historical_liquidity_qualified': False,
        'interpretation_limits': ['local_receipt_option_timestamps', 'no_unobserved_depth_or_interpolation',
            'l1_shortfall_does_not_prove_full_book_shortfall', 'neighbours_not_continuous_outage_duration',
            'research_risk_latch_not_actual_exchange_liquidation'],
        'authorities': ledger.AUTHORITIES.copy()}


def audit(data):
    with localcontext() as context:
        context.prec = 100
        return _audit(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=pathlib.Path, required=True)
    parser.add_argument('--ledger-sha256', required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    try:
        raw = wire.safe_read(args.ledger)
        require(wire.SHA.fullmatch(args.ledger_sha256) and wire.digest(raw) == args.ledger_sha256, 'EXIT_LEDGER_HASH_MISMATCH')
        with localcontext() as context:
            context.prec = 100
            report = audit(wire.decode(raw))
        report.update(ledger_file_sha256=args.ledger_sha256,
            engine_sha256=wire.digest(pathlib.Path(__file__).read_bytes()),
            ledger_engine_sha256=wire.digest(pathlib.Path(ledger.__file__).read_bytes()))
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not args.output.parent.is_symlink(), 'EXIT_OUTPUT_PARENT_SYMLINK')
        wire.safe_file(args.output, wire.encode(report))
        print(wire.encode(report).decode(), end='')
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        print(wire.encode({'status': 'EXIT_EVIDENCE_NOT_COMPLETED', 'reason': wire.error_code(exc),
            'authorities': ledger.AUTHORITIES.copy()}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
