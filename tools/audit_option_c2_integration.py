#!/usr/bin/env python3
"""Pinned first-lifecycle offline Cross integration + retrospective funding bands.

Frozen counterfactual fills, explicit instantaneous taker/no-resting-order model,
prior CLOSED public candles, current rules. Never upgrades original C2 evidence.
"""
from __future__ import annotations
import argparse
import copy
import pathlib
import traceback
from decimal import Decimal, ROUND_HALF_EVEN

import audit_bybit_cross_account as cross
import audit_bybit_readonly_evidence as wire
import audit_option_subaccount_ledger as ledger
import bybit_cross_margin_reference as ref
import collect_bybit_c2_market as market_tool

ZERO, ONE = Decimal(0), Decimal(1)
require = wire.require
MINUTE = 60000


def bounded_text(value):
    # Input interface accepts 18 fractional digits; the exact reconstruction
    # stays at 100 digits. Per input rounding is bounded by 0.5e-18 units.
    return format(value.quantize(Decimal('1e-18'), rounding=ROUND_HALF_EVEN), 'f')


def reference_rules(market, identity):
    rows = market['risk']
    require(rows and rows[0].get('isLowestRisk') == 1, 'LOWEST_RISK_TIER_MISSING')
    tiers = []
    for index, row in enumerate(rows):
        require(row.get('symbol') == 'BTCUSDT', 'RISK_SYMBOL_INVALID')
        deduction = row.get('mmDeduction')
        # Only the documented/observed first tier structural zero; missing
        # deductions elsewhere cannot be back-filled as historical evidence.
        if index == 0 and deduction == '': deduction = '0'
        tiers.append({'limit_usdt': row['riskLimitValue'], 'mmr': row['maintenanceMargin'],
                      'max_leverage': row['maxLeverage'], 'mm_deduction_usdt': deduction})
    rules = {'schema_version': 'bybit_cross_rules_v1', 'basis': 'current_rule_research',
        'source_id': identity, 'historical_applicability_qualified': False,
        'option_factors': copy.deepcopy(ref.BTC_REFERENCE), 'usdt_collateral_ratio': '1',
        'linear': {'leverage': '10', 'open_fee_rate': '0.00055', 'close_reserve_fee_rate': '0.00055', 'tiers': tiers}}
    cross.validate_rules(rules)
    return rules


def prior_close(series, now):
    # Explicit research publication delay convention, NOT a proven historical
    # latency SLA. It never uses a candle that has not yet ended.
    stamp = (now - 1000) // MINUTE * MINUTE - MINUTE
    require(stamp in series, 'PRIOR_CLOSED_CANDLE_MISSING')
    return {'value': series[stamp]['close'], 'ts_ms': stamp + MINUTE,
            'source_kind': 'prior_closed_candle_proxy'}


def update_fill(positions, averages, cash, instruments, symbol, qty, price, fee):
    old = positions.get(symbol, ZERO)
    average = averages.get(symbol, ZERO)
    new = old + qty
    if symbol == 'BTCUSDT':
        realized = min(abs(old), abs(qty)) * (price - average) * (ONE if old > 0 else -ONE) if old * qty < 0 else ZERO
        cash += realized
    else:
        cash -= qty * price
    if new == 0:
        positions.pop(symbol, None)
        averages.pop(symbol, None)
    else:
        if old * qty >= 0:
            averages[symbol] = (abs(old) * average + abs(qty) * price) / abs(new)
        elif old * new < 0:
            averages[symbol] = price
        positions[symbol] = new
    return cash - fee


def funding_bands(data, market):
    schedule = {row['ts_ms'] for row in data['funding_schedule']}
    require(schedule == set(market['funding']), 'FUNDING_CALENDAR_DIFFERS_FROM_PINNED_LEDGER')
    require(not any(e['type'] == 'FUNDING' for e in data['events']), 'FUNDING_ALREADY_PRESENT_NO_DOUBLE_COUNT')
    trades = [e for e in data['events'] if e['type'] == 'FILL' and e['symbol'] == 'BTCUSDT']
    results = []
    for stamp in sorted(schedule):
        before = sum((Decimal(e['signed_qty_btc']) for e in trades if e['ts_ms'] < stamp - 5000), ZERO)
        possible = [before]
        for event in trades:
            if stamp - 5000 <= event['ts_ms'] <= stamp + 5000:
                possible.append(possible[-1] + Decimal(event['signed_qty_btc']))
        # The guard straddles two minute buckets. OHLC is an ex-post price
        # envelope, never an exact funding mark or causal strategy feature.
        candle_keys = {(stamp - 5000) // MINUTE * MINUTE, stamp // MINUTE * MINUTE}
        require(all(key in market['mark'] for key in candle_keys), 'FUNDING_MARK_BAND_MISSING')
        low = min(Decimal(market['mark'][key]['low']) for key in candle_keys)
        high = max(Decimal(market['mark'][key]['high']) for key in candle_keys)
        rate = Decimal(market['funding'][stamp])
        outcomes = [-qty * mark * rate for qty in possible for mark in (low, high)]
        results.append({'ts_ms': stamp, 'position_inclusion_ambiguous': len(set(possible)) > 1,
                        'position_qty_min': str(min(possible)), 'position_qty_max': str(max(possible)),
                        'rate': str(rate), 'mark_low': str(low), 'mark_high': str(high),
                        'cash_low_usdt': str(min(outcomes)), 'cash_high_usdt': str(max(outcomes))})
    return results


@ref.exact
def integrate(data, market, market_identity, *, fx='1'):
    require(data.get('evidence_kind') in ('synthetic_fixture', 'local_unverified_research'), 'UNVERIFIED_LEDGER_REQUIRED')
    require(not any('margin' in event for event in data['events']), 'SUPPLIED_MARGIN_MUST_NOT_BE_OVERWRITTEN')
    baseline = ledger.audit(copy.deepcopy(ledger.SCOPE), data)
    require(baseline['status'] in ('INSUFFICIENT_EVIDENCE', 'RISK_REJECTED_OFFLINE', 'PASS_OFFLINE_ACCOUNTING_ONLY'),
            'SOURCE_LEDGER_INVALID')
    require(Decimal(fx).is_finite() and Decimal(fx) > 0, 'FX_SCENARIO_INVALID')
    rules = reference_rules(market, market_identity)
    bands = funding_bands(data, market)
    source_hash = wire.digest(ledger.canonical(data))
    positions, averages, local_marks = {}, {}, {}
    cash = Decimal(data['simulation_capital_usdt'])
    book = cross.OrderBook()
    traces = []
    order_check_count = split_fill_count = proxy_option_marks = 0
    max_nav_difference = ZERO

    def snapshot(now, pending, scenario_cash, needed_extra=()):
        nonlocal proxy_option_marks
        needed = set(positions) | {o['symbol'] for o in pending} | set(needed_extra)
        marks = {}
        for symbol in needed:
            stream = 'mark' if symbol == 'BTCUSDT' else 'option:' + symbol
            if stream in market and ((now - 1000) // MINUTE * MINUTE - MINUTE) in market[stream]:
                marks[symbol] = prior_close(market[stream], now)
                if symbol != 'BTCUSDT': proxy_option_marks += 1
            else:
                require(symbol != 'BTCUSDT', 'PERPETUAL_CLOSED_MARK_MISSING')
                require(symbol in local_marks, 'OPTION_MARK_CONTEXT_MISSING')
                marks[symbol] = local_marks[symbol]
        return {'schema_version': cross.SCHEMA, 'evidence_kind': 'explicit_research_model',
            'scope_id': 'offline_subaccount', 'margin_mode': 'REGULAR_MARGIN', 'currency': 'USDT',
            'ts_ms': now, 'price_max_age_ms': max(121000, data['illustrative_limits']['quote_max_age_ms']),
            'wallet_usdt': bounded_text(scenario_cash), 'liabilities_usdt': '0', 'other_assets': [],
            'instruments': data['instruments'], 'index': prior_close(market['index'], now),
            'usdt_usd': {'value': fx, 'ts_ms': now, 'source_kind': 'synthetic'},
            'marks': marks, 'positions': {symbol: {'qty': bounded_text(qty), 'entry': bounded_text(averages[symbol])}
                                         for symbol, qty in positions.items()}, 'orders': pending}

    def evaluate(now, phase, seq, base_cash):
        nonlocal order_check_count
        active = list(book.active.values())
        low = sum((Decimal(b['cash_low_usdt']) for b in bands if b['ts_ms'] <= now), ZERO)
        high = sum((Decimal(b['cash_high_usdt']) for b in bands if b['ts_ms'] <= now), ZERO)
        outputs = [cross.calculate(snapshot(now, active, base_cash + delta), rules) for delta in (low, high)]
        if phase == 'order_pending': order_check_count += 1
        traces.append({'seq': seq, 'ts_ms': now, 'phase': phase,
            'active_orders': len(active), 'cash_low_usdt': bounded_text(base_cash + low),
            'cash_high_usdt': bounded_text(base_cash + high),
            'im_usd_low_cash': outputs[0]['total_initial_margin_usd'],
            'mm_usd_low_cash': outputs[0]['total_maintenance_margin_usd'],
            'imr_low_cash': outputs[0]['account_im_rate'], 'mmr_low_cash': outputs[0]['account_mm_rate'],
            'imr_high_cash': outputs[1]['account_im_rate'], 'mmr_high_cash': outputs[1]['account_mm_rate'],
            'minimum_risk_headroom_usd': str(min(Decimal(o['risk_headroom_after_order_loss_usd']) for o in outputs)),
            'risk_review': any(o['risk_reasons'] for o in outputs)})
        return outputs[0], low

    for event, old_checkpoint in zip(data['events'], baseline['checkpoints']):
        now, kind, seq = event['ts_ms'], event['type'], event['seq']
        for symbol, quote in event['valuation'].items():
            local_marks[symbol] = {'value': quote['mark'], 'ts_ms': quote['ts_ms'], 'source_kind': 'local_receipt'}
        if kind == 'FILL':
            symbol = event['symbol']
            qty, price, fee = [Decimal(event[key]) for key in ('signed_qty_btc', 'price_usdt_per_btc', 'fee_usdt')]
            old = positions.get(symbol, ZERO)
            if old * qty < 0 and abs(qty) > abs(old):
                parts = [-old, qty + old]
                split_fill_count += 1
            else:
                parts = [qty]
            remaining_fee = fee
            for part_index, part in enumerate(parts):
                intent = 'reduce' if positions.get(symbol, ZERO) * part < 0 else 'open'
                identity = f'modeled-{seq}-{part_index}'
                order = {'id': identity, 'symbol': symbol, 'side': 'Buy' if part > 0 else 'Sell', 'intent': intent,
                    'remaining_qty': bounded_text(abs(part)), 'limit_price': str(price), 'created_ms': now}
                book.apply({'type': 'PLACE', 'ts_ms': now, 'order': order}, data['instruments'])
                evaluate(now, 'order_pending', seq, cash)
                book.apply({'type': 'FILL', 'ts_ms': now, 'id': identity, 'qty': bounded_text(abs(part)), 'price': str(price)}, data['instruments'])
                part_fee = remaining_fee if part_index == len(parts) - 1 else fee * abs(part / qty)
                remaining_fee -= part_fee
                cash = update_fill(positions, averages, cash, data['instruments'], symbol, part, price, part_fee)
        elif kind == 'DELIVERY':
            symbol = event['symbol']
            cash += Decimal(event['cash_delta_before_fee_usdt']) - Decimal(event['fee_usdt'])
            positions.pop(symbol)
            averages.pop(symbol)
        require(abs(cash - Decimal(old_checkpoint['cash_usdt'])) <= Decimal('1e-8'), 'INDEPENDENT_CASH_RECONSTRUCTION_MISMATCH')
        require(not book.active, 'RESTING_ORDER_AFTER_INSTANTANEOUS_FILL')
        out, low = evaluate(now, 'post_event', seq, cash)
        proxy_nav = Decimal(out['total_equity_usd']) / Decimal(fx) - low
        max_nav_difference = max(max_nav_difference, abs(proxy_nav - Decimal(old_checkpoint['nav_usdt'])))
    require(wire.digest(ledger.canonical(data)) == source_hash, 'SOURCE_LEDGER_MUTATED')
    funding_low = sum((Decimal(b['cash_low_usdt']) for b in bands), ZERO)
    funding_high = sum((Decimal(b['cash_high_usdt']) for b in bands), ZERO)
    imrs = [Decimal(t[key]) for t in traces for key in ('imr_low_cash', 'imr_high_cash') if t[key] is not None]
    mmrs = [Decimal(t[key]) for t in traces for key in ('mmr_low_cash', 'mmr_high_cash') if t[key] is not None]
    summary = {'schema_version': 'option_c2_cross_integration_v1', 'status': 'C2_REFERENCE_INTEGRATED_NOT_QUALIFIED',
        'ledger_canonical_sha256': source_hash, 'market_manifest_sha256': market_identity,
        'ledger_events': len(data['events']), 'model_checkpoints': len(traces), 'pending_order_checks': order_check_count,
        'reversal_fills_split_without_changing_total_qty_or_fee': split_fill_count,
        'funding_boundaries': bands, 'funding_cash_low_usdt': str(funding_low), 'funding_cash_high_usdt': str(funding_high),
        'base_pnl_usdt': baseline['pnl_on_simulated_capital_usdt'],
        'base_plus_funding_low_usdt': str(Decimal(baseline['pnl_on_simulated_capital_usdt']) + funding_low),
        'base_plus_funding_high_usdt': str(Decimal(baseline['pnl_on_simulated_capital_usdt']) + funding_high),
        'peak_imr_reference': str(max(imrs)) if imrs else None, 'peak_mmr_reference': str(max(mmrs)) if mmrs else None,
        'minimum_risk_headroom_usd': str(min(Decimal(t['minimum_risk_headroom_usd']) for t in traces)),
        'risk_review_checkpoints': sum(t['risk_review'] for t in traces),
        'max_abs_nav_difference_from_frozen_mark_basis_usdt': str(max_nav_difference),
        'public_option_mark_valuations': proxy_option_marks, 'fx_scenario_usdt_usd': fx,
        'source_ledger_status': baseline['status'], 'source_ledger_missing_evidence': baseline['missing_evidence'],
        'historical_qualification_gaps': ['historical_margin_and_fee_parameters_unverified', 'independent_cross_account_reference_missing',
            'one_minute_prices_not_checkpoint_or_settlement_truth', 'option_index_uses_linear_index_proxy',
            'fx_is_explicit_scenario_not_bybit_historical_index', 'instantaneous_taker_order_model_not_observed_orders',
            'funding_band_retrospective_not_causal_decision_feature', 'linear_order_mm_close_reserve_convention'],
        'order_model': 'instantaneous_taker_no_resting_orders', 'source_ledger_unchanged': True,
        'funding_amounts_qualified': False, 'historical_margin_qualified': False, 'c2_qualified': False,
        'candidate_state': 'CLOSED', 'forward_started': False, 'authorities': cross.AUTHORITY.copy()}
    return summary, traces


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=pathlib.Path, required=True)
    parser.add_argument('--ledger-sha256', required=True)
    parser.add_argument('--market-capture', type=pathlib.Path)
    parser.add_argument('--market-sha256')
    parser.add_argument('--collect-market-root', type=pathlib.Path)
    parser.add_argument('--trace-output', type=pathlib.Path, required=True)
    parser.add_argument('--fx-scenario', default='1')
    args = parser.parse_args()
    stage = 'read_ledger'
    try:
        data, identity = ledger.read_input(args.ledger)
        require(wire.SHA.fullmatch(args.ledger_sha256) and identity == args.ledger_sha256, 'PINNED_LEDGER_HASH_MISMATCH')
        if args.collect_market_root:
            stage = 'collect_market'
            require(args.market_capture is None and args.market_sha256 is None, 'AMBIGUOUS_MARKET_INPUT')
            options = sorted(s for s in data['instruments'] if s != 'BTCUSDT')
            requests = market_tool.plan(data['start_ts_ms'], data['end_ts_ms'], options)
            args.market_capture = market_tool.collect(args.collect_market_root, data['start_ts_ms'], data['end_ts_ms'], options,
                                                       market_tool.Transport(requests))
            args.market_sha256 = wire.digest(wire.safe_read(args.market_capture / 'manifest.json'))
        require(args.market_capture is not None and args.market_sha256 is not None, 'MARKET_PIN_REQUIRED')
        stage = 'replay_market'
        market, source = market_tool.replay(args.market_capture, args.market_sha256)
        require((source['start_ms'], source['end_ms']) == (data['start_ts_ms'], data['end_ts_ms']), 'MARKET_LEDGER_WINDOW_MISMATCH')
        stage = 'integrate'
        summary, traces = integrate(data, market, args.market_sha256, fx=args.fx_scenario)
        stage = 'write_trace'
        args.trace_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not args.trace_output.parent.is_symlink(), 'TRACE_PARENT_SYMLINK')
        wire.safe_file(args.trace_output, wire.encode({'summary': summary, 'checkpoints': traces}))
        summary.update(ledger_file_sha256=identity, trace_sha256=wire.digest(args.trace_output.read_bytes()),
            engine_sha256=wire.digest(pathlib.Path(__file__).read_bytes()),
            cross_engine_sha256=wire.digest(pathlib.Path(cross.__file__).read_bytes()),
            reference_engine_sha256=wire.digest(pathlib.Path(ref.__file__).read_bytes()),
            ledger_engine_sha256=wire.digest(pathlib.Path(ledger.__file__).read_bytes()),
            market_source=source)
        print(wire.encode(summary).decode(), end='')
        return 0
    except (ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        # Only our checked-in code locations, never values, stack locals,
        # exception text, ledger content or credentials.
        allowed_files = {pathlib.Path(module.__file__).name for module in (cross, wire, ledger, ref, market_tool)}
        allowed_files.add(pathlib.Path(__file__).name)
        locations = [{'file': pathlib.Path(frame.filename).name, 'line': frame.lineno}
                     for frame in traceback.extract_tb(exc.__traceback__)
                     if pathlib.Path(frame.filename).name in allowed_files]
        print(wire.encode({'status': 'C2_INTEGRATION_NOT_COMPLETED', 'reason': wire.error_code(exc),
                          'stage': stage, 'code_locations': locations,
                          'c2_qualified': False, 'authorities': cross.AUTHORITY.copy()}).decode(), end='')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
