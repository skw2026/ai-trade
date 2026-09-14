#!/usr/bin/env python3
"""Evidence-derived C2 research acceptance and explicit unsupported verifiers.

Recomputes from pinned input bytes, not caller-supplied PASS booleans. It does
not issue historical certification; unsupported proof classes are named, not
represented as if waiting longer or rerunning this tool could certify them.
"""
from __future__ import annotations
import argparse
from decimal import Decimal, localcontext
import pathlib

import audit_bybit_readonly_evidence as wire
import audit_option_subaccount_ledger as ledger
import audit_option_c2_integration as integration
import audit_option_exit_evidence as exits
import audit_c2_reference_conformance as references
import collect_bybit_c2_market as market_tool

require = wire.require


@integration.ref.exact
def audit(data, market, market_identity):
    before = wire.digest(ledger.canonical(data))
    baseline = ledger.audit(ledger.SCOPE, data)
    require(baseline['status'] != 'TECHNICALLY_INVALID', 'ACCEPTANCE_INVALID_LEDGER')
    summary, trace = integration.integrate(data, market, market_identity)
    exit_report = exits.audit(data)
    reference = references.audit()
    require(before == wire.digest(ledger.canonical(data)), 'ACCEPTANCE_SOURCE_MUTATED')
    require(before == summary['ledger_canonical_sha256'] == exit_report['ledger_canonical_sha256'], 'ACCEPTANCE_SOURCE_MISMATCH')
    checks = []
    def check(identity, state, observation, close_condition):
        checks.append({'id': identity, 'state': state, 'observation': observation, 'close_condition': close_condition})
    check('source_and_cash_replay', 'PASS',
          {'events': len(data['events']), 'reference_checkpoints': len(trace), 'source_unchanged': True,
           'base_pnl_usdt': summary['base_pnl_usdt']},
          'Already recomputed cash per event; does not establish external source authenticity.')
    check('published_reference_arithmetic', 'PASS' if reference['passed_cases'] == len(reference['cases']) else 'FAIL',
          {'passed': reference['passed_cases'], 'total': len(reference['cases']),
           'quarantined_source_count': len(reference['quarantined_sources'])},
          'Every supported published numeric case must match; conflicting source parameters cannot be accepted.')
    check('observed_exit_l1', 'PASS_OBSERVED' if exit_report['unqualified_checkpoint_count'] == 0 else 'CONSTRAINT_OBSERVED',
          {'checkpoints': exit_report['unqualified_checkpoint_count'],
           'distinct_quote_observations': exit_report['distinct_bad_quote_observations'],
           'classifications': exit_report['classification_counts']},
          'Investigate each original quote; deeper same-venue evidence may resolve L1-only uncertainty, never invent liquidity.')
    with localcontext() as context:
        context.prec = 100
        width = Decimal(summary['funding_cash_high_usdt']) - Decimal(summary['funding_cash_low_usdt'])
        low, high = Decimal(summary['base_plus_funding_low_usdt']), Decimal(summary['base_plus_funding_high_usdt'])
    zero_obligation = all(not row['position_inclusion_ambiguous'] and Decimal(row['position_qty_min']) == 0
                          and Decimal(row['position_qty_max']) == 0 for row in summary['funding_boundaries'])
    check('funding_amount', 'NO_OBLIGATION_ON_RETURNED_BOUNDARIES' if zero_obligation else 'BOUNDED_RESEARCH_ONLY',
          {'cash_low_usdt': summary['funding_cash_low_usdt'], 'cash_high_usdt': summary['funding_cash_high_usdt'],
           'interval_width_usdt': format(width, 'f'), 'base_plus_funding_interval_contains_zero': low <= 0 <= high,
           'returned_boundaries': len(summary['funding_boundaries'])},
          'Exact settlement price or approved error-bounded research basis; minute OHLC is not exact cashflow evidence.')
    # Explicit capability limits of the accepted input classes. No external
    # boolean/file stating "verified" can bypass a missing independent verifier.
    unsupported = [
        ('historical_parameters', 'Current risk snapshot and illustrative option factors; historical fee applicability not proven.',
         'Effective-dated parameter/fee source covering the target interval and field-level verifier.'),
        ('independent_cross_account', 'Published isolated examples do not supply the mixed option/hedge/order account reference.',
         'Independent same-state Cross sample and discrepancy analysis, without changing the existing Demo mode.'),
        ('historical_price_and_source', 'Option local receipts, prior minute prices, linear option-index proxy and FX scenario.',
         'Proven source/time coverage or separately approved bounded model contract, not an assertion in JSON.'),
        ('historical_order_path', 'Explicit instantaneous-taker/no-resting-order model; not observed exchange orders.',
         'Validated execution/order-occupancy model or an appropriately sourced order path.')]
    for identity, observation, condition in unsupported:
        check(identity, 'VERIFIER_NOT_SUPPORTED_FOR_THESE_INPUTS', observation, condition)
    research_pass = all(row['state'] == 'PASS' for row in checks if row['id'] in
                        ('source_and_cash_replay', 'published_reference_arithmetic'))
    return {'schema_version': 'option_c2_acceptance_v1',
        'status': 'RESEARCH_REPLAY_ACCEPTED_WITH_LIMITS' if research_pass else 'RESEARCH_REPLAY_REJECTED',
        'research_replay_accepted': research_pass,
        'observed_exit_l1_passed': exit_report['unqualified_checkpoint_count'] == 0,
        'historical_verification_supported': False,
        'historical_status': 'NOT_QUALIFIED_UNSUPPORTED_PROOF_CLASSES',
        'unsupported_verifiers': [r[0] for r in unsupported], 'checks': checks,
        'ledger_canonical_sha256': before, 'market_manifest_sha256': market_identity,
        'source_ledger_status': baseline['status'], 'source_ledger_missing_evidence': baseline['missing_evidence'],
        'reference_conformance': reference, 'exit_evidence': exit_report,
        'funding_interval_width_usdt': format(width, 'f'),
        'prior_failures_not_erased': True, 'source_ledger_unchanged': True,
        'candidate_state': 'CLOSED', 'forward_started': False, 'authorities': ledger.AUTHORITIES.copy()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', required=True, type=pathlib.Path)
    parser.add_argument('--ledger-sha256', required=True)
    parser.add_argument('--market-capture', required=True, type=pathlib.Path)
    parser.add_argument('--market-sha256', required=True)
    parser.add_argument('--output', required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        raw = wire.safe_read(args.ledger)
        require(wire.SHA.fullmatch(args.ledger_sha256) and wire.digest(raw) == args.ledger_sha256, 'ACCEPTANCE_LEDGER_HASH_MISMATCH')
        data = wire.decode(raw)
        market, source = market_tool.replay(args.market_capture, args.market_sha256)
        require((source['start_ms'], source['end_ms']) == (data['start_ts_ms'], data['end_ts_ms']), 'ACCEPTANCE_WINDOW_MISMATCH')
        with localcontext() as context:
            context.prec = 100
            report = audit(data, market, args.market_sha256)
        report.update(ledger_file_sha256=args.ledger_sha256, market_source=source,
            engine_sha256=wire.digest(pathlib.Path(__file__).read_bytes()),
            dependency_sha256={pathlib.Path(module.__file__).name: wire.digest(pathlib.Path(module.__file__).read_bytes())
                               for module in (wire, ledger, integration, exits, references, market_tool,
                                              integration.cross, integration.ref)})
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not args.output.parent.is_symlink(), 'ACCEPTANCE_OUTPUT_PARENT_SYMLINK')
        wire.safe_file(args.output, wire.encode(report))
        print(wire.encode(report).decode(), end='')
        return 0 if report['research_replay_accepted'] else 2
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        print(wire.encode({'status': 'ACCEPTANCE_NOT_COMPLETED', 'reason': wire.error_code(exc),
            'authorities': ledger.AUTHORITIES.copy()}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
