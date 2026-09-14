#!/usr/bin/env python3
"""Narrow published-arithmetic conformance, never an exchange account sample.

Sources and contradictory spread example are recorded in the stage report.
Case prices/tiers are published ILLUSTRATIONS, not actual BTC risk parameters.
"""
import copy
from decimal import Decimal, localcontext
import pathlib

import audit_bybit_cross_account as cross
import audit_bybit_readonly_evidence as wire
import bybit_cross_margin_reference as ref

OPTION_SOURCE = 'https://www.bybit.com/en/help-center/article/Initial-Maintenance-Margin-Calculations-Options'
LINEAR_SOURCE = 'https://www.bybit.com/en/help-center/article/Maintenance-Margin-USDT-Contract'
SPREAD_SOURCE = 'https://www.bybit.com/en/help-center/article/Comparison-of-Spread-Strategies-in-Cross-Margin-and-Portfolio-Margin'


def audit():
    # A valid synthetic date/symbol substitutes for obsolete example identifiers.
    # No assertion that these prices or parameters applied on that date.
    now, expiry = 1789286400000, 1789372800000
    symbol = 'BTC-14SEP26-31000-C-USDT'
    quote = lambda value: {'value': value, 'ts_ms': now, 'source_kind': 'synthetic'}
    option = {'kind': 'call', 'settle_coin': 'USDT', 'quantity_unit': 'BTC',
              'qty_step': '0.01', 'strike': '31000', 'expiry_ts_ms': expiry}
    perp = {'kind': 'linear_perpetual', 'settle_coin': 'USDT', 'quantity_unit': 'BTC', 'qty_step': '0.001'}
    state = {'schema_version': cross.SCHEMA, 'evidence_kind': 'synthetic_fixture',
        'scope_id': 'offline_subaccount', 'margin_mode': 'REGULAR_MARGIN', 'currency': 'USDT',
        'ts_ms': now, 'price_max_age_ms': 1000, 'wallet_usdt': '10000', 'liabilities_usdt': '0',
        'other_assets': [], 'instruments': {symbol: option, 'BTCUSDT': perp},
        'index': quote('30000'), 'usdt_usd': quote('1'), 'marks': {symbol: quote('300')},
        'positions': {symbol: {'qty': '-1', 'entry': '350'}}, 'orders': []}
    rules = {'schema_version': 'bybit_cross_rules_v1', 'basis': 'synthetic_fixture',
        'source_id': 'published-illustrations-not-market-parameters', 'historical_applicability_qualified': False,
        'option_factors': copy.deepcopy(ref.BTC_REFERENCE), 'usdt_collateral_ratio': '1',
        'linear': {'leverage': '10', 'open_fee_rate': '0', 'close_reserve_fee_rate': '0', 'tiers': [
            {'limit_usdt': str(cap), 'mmr': rate, 'max_leverage': lev, 'mm_deduction_usdt': str(ded)}
            for cap, rate, lev, ded in ((100000, '.02', '25', 0), (200000, '.025', '20', 500),
                (300000, '.03', '16.67', 1500), (400000, '.035', '14.29', 3000))]}}
    results = []
    def check(identity, source, expected):
        actual = cross.calculate(state, rules)
        equal = {key: Decimal(actual[key]) == Decimal(value) for key, value in expected.items()}
        results.append({'id': identity, 'source': source, 'status': 'PASS' if all(equal.values()) else 'FAIL',
            'expected': expected, 'actual': {key: actual[key] for key in expected}, 'field_matches': equal})
    check('option_published_position_and_account_rates', OPTION_SOURCE,
          {'total_initial_margin_usd': '2350', 'total_maintenance_margin_usd': '1260',
           'account_im_rate': '.235', 'account_mm_rate': '.126'})
    # Official ETH illustrative case mapped only for arithmetic to this narrow
    # BTC schema. Zero fees isolate the published base MM (not fee-inclusive MM).
    state.update(wallet_usdt='100000', marks={'BTCUSDT': quote('4000')},
                 positions={'BTCUSDT': {'qty': '50', 'entry': '4000'}},
                 orders=[{'id': 'illustrative-buy', 'symbol': 'BTCUSDT', 'side': 'Buy', 'intent': 'open',
                     'remaining_qty': '50', 'limit_price': '3000', 'created_ms': now}])
    check('linear_published_position_plus_order_base_mm', LINEAR_SOURCE,
          {'position_maintenance_margin_usdt': '4500', 'order_maintenance_margin_usdt': '5250',
           'total_maintenance_margin_usd': '9750'})
    with localcontext() as context:
        context.prec = 100
        # The spread page uses 15%/10% while the parameter page uses 10%/5%.
        current_im = max(max(Decimal('.10') * 20250 - (20250 - 18500), Decimal('.05') * 20250) + 290,
                         Decimal('.03') * 20250 + 290 + Decimal('.002') * 20250)
        legacy_im = max(max(Decimal('.15') * 20250 - (20250 - 18500), Decimal('.10') * 20250) + 290, Decimal(938))
    return {'schema_version': 'c2_reference_conformance_v1',
        'status': 'REFERENCE_CASES_PASS_WITH_QUARANTINED_SOURCE' if all(r['status'] == 'PASS' for r in results) else 'REFERENCE_CASE_MISMATCH',
        'cases': results, 'passed_cases': sum(r['status'] == 'PASS' for r in results),
        'quarantined_sources': [{'url': SPREAD_SOURCE, 'status': 'PARAMETER_AND_IM_MM_LABEL_CONFLICT',
            'current_parameter_im_usdt': format(current_im, 'f'), 'page_parameter_im_usdt': format(legacy_im, 'f'),
            'page_computed_mm_usdt': '938', 'page_table_labeled_mm_usdt': '2315',
            'accepted_as_current_reference': False}],
        'coverage_limits': ['not_independent_exchange_account_snapshot', 'no_option_plus_hedge_plus_order_published_case',
            'linear_case_excludes_close_fee_reserve', 'illustrative_eth_numbers_mapped_to_synthetic_btc_schema',
            'synthetic_symbol_date_not_historical_applicability'],
        'cross_engine_sha256': wire.digest(pathlib.Path(cross.__file__).read_bytes()),
        'engine_sha256': wire.digest(pathlib.Path(__file__).read_bytes()),
        'historical_margin_qualified': False, 'authorities': cross.AUTHORITY.copy()}


if __name__ == '__main__':
    report = audit()
    print(wire.encode(report).decode(), end='')
    raise SystemExit(0 if report['passed_cases'] == len(report['cases']) else 2)
