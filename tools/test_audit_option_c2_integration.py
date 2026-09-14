#!/usr/bin/env python3
import copy
from decimal import Decimal
import pathlib
import subprocess
import sys
import tempfile
import unittest

import audit_option_c2_integration as integration
import audit_option_subaccount_ledger as ledger
import audit_bybit_readonly_evidence as wire
from test_audit_option_subaccount_ledger import fixture as old_fixture, CALL, PUT, PERP, START, EXPIRY, bbo


def fixture():
    data = old_fixture()
    data['events'] = [e for e in data['events'] if e['type'] != 'FUNDING']
    for seq, event in enumerate(data['events']):
        event.pop('margin', None)
        event['seq'] = seq
    data['funding_schedule'] = [{'settlement_id': 'at-end', 'ts_ms': EXPIRY, 'symbol': PERP}]
    first = START // 60000 * 60000 - 60000
    last = EXPIRY // 60000 * 60000
    candles = {t: {'open': '80000', 'high': '80001', 'low': '79999', 'close': '80000'}
               for t in range(first, last + 60000, 60000)}
    market = {'mark': copy.deepcopy(candles), 'index': copy.deepcopy(candles), 'funding': {EXPIRY: '0.0001'},
              'risk': [{'symbol': PERP, 'isLowestRisk': 1, 'riskLimitValue': '100000',
                        'maintenanceMargin': '0.005', 'maxLeverage': '100', 'mmDeduction': ''}]}
    return data, market


class C2IntegrationTest(unittest.TestCase):
    def test_full_account_path_preserves_original_ledger_and_gaps(self):
        data, market = fixture()
        saved = copy.deepcopy(data)
        report, trace = integration.integrate(data, market, 'synthetic-market')
        self.assertEqual(data, saved)
        self.assertEqual(report['ledger_events'], 7)
        self.assertEqual(report['pending_order_checks'], 4)
        self.assertEqual(report['model_checkpoints'], 11)
        self.assertEqual(report['base_pnl_usdt'], ledger.audit(ledger.SCOPE, data)['pnl_on_simulated_capital_usdt'])
        self.assertIn('MARGIN_EVIDENCE_MISSING', report['source_ledger_missing_evidence'])
        self.assertIn('SCHEDULED_FUNDING_MISSING', report['source_ledger_missing_evidence'])
        self.assertFalse(report['c2_qualified'])
        self.assertFalse(report['funding_amounts_qualified'])
        self.assertEqual(report['candidate_state'], 'CLOSED')
        self.assertTrue(any(t['active_orders'] == 1 for t in trace))
        self.assertEqual(trace[-1]['active_orders'], 0)

    def test_funding_inclusion_ambiguity_encloses_flat_and_exposed(self):
        data, market = fixture()
        bands = integration.funding_bands(data, market)
        self.assertEqual(Decimal(bands[0]['cash_low_usdt']), Decimal('-0.080001'))
        self.assertEqual(Decimal(bands[0]['cash_high_usdt']), 0)
        self.assertTrue(bands[0]['position_inclusion_ambiguous'])
        data['events'][4]['ts_ms'] -= 1
        bands = integration.funding_bands(data, market)
        self.assertEqual(Decimal(bands[0]['cash_low_usdt']), 0)
        self.assertFalse(bands[0]['position_inclusion_ambiguous'])

    def test_positive_short_funding_sign(self):
        data, market = fixture()
        for event in data['events']:
            if event['type'] == 'FILL' and event['symbol'] == PERP:
                event['signed_qty_btc'] = str(-Decimal(event['signed_qty_btc']))
        bands = integration.funding_bands(data, market)
        self.assertEqual(Decimal(bands[0]['cash_low_usdt']), 0)
        self.assertEqual(Decimal(bands[0]['cash_high_usdt']), Decimal('0.080001'))

    def test_calendar_mismatch_and_double_funding_rejected(self):
        data, market = fixture()
        market['funding'] = {}
        with self.assertRaisesRegex(ValueError, 'CALENDAR'):
            integration.integrate(data, market, 'synthetic')
        data, market = fixture()
        data['events'].append({'type': 'FUNDING'})
        with self.assertRaisesRegex(ValueError, 'ALREADY_PRESENT'):
            integration.funding_bands(data, market)

    def test_prior_candle_no_lookahead(self):
        _, market = fixture()
        found = integration.prior_close(market['mark'], EXPIRY)
        self.assertLess(found['ts_ms'], EXPIRY)
        self.assertEqual(found['ts_ms'], EXPIRY - 60000)
        found = integration.prior_close(market['mark'], EXPIRY + 1000)
        self.assertEqual(found['ts_ms'], EXPIRY)

    def test_public_option_proxy_is_used_without_upgrading_qualification(self):
        data, market = fixture()
        for symbol in (CALL, PUT):
            market['option:' + symbol] = {t: {'open': '1001', 'high': '1002', 'low': '1000', 'close': '1001'}
                                         for t in market['mark']}
        report, _ = integration.integrate(data, market, 'synthetic')
        self.assertGreater(report['public_option_mark_valuations'], 0)
        self.assertFalse(report['historical_margin_qualified'])
        self.assertGreater(Decimal(report['max_abs_nav_difference_from_frozen_mark_basis_usdt']), 0)

    def test_fx_scenario_does_not_change_ratios(self):
        data, market = fixture()
        a, _ = integration.integrate(data, market, 'synthetic', fx='1')
        b, _ = integration.integrate(data, market, 'synthetic', fx='0.99')
        self.assertEqual(a['peak_imr_reference'], b['peak_imr_reference'])
        self.assertEqual(a['peak_mmr_reference'], b['peak_mmr_reference'])
        self.assertNotEqual(a['minimum_risk_headroom_usd'], b['minimum_risk_headroom_usd'])

    def test_zero_scaled_option_mark_keeps_fixed_decimal_boundary(self):
        data, market = fixture()
        for symbol in (CALL, PUT):
            market['option:' + symbol] = {t: {'open': '0.00000000', 'high': '0.00000000',
                'low': '0.00000000', 'close': '0.00000000'} for t in market['mark']}
        report, _ = integration.integrate(data, market, 'synthetic')
        self.assertGreater(report['public_option_mark_valuations'], 0)
        self.assertFalse(report['c2_qualified'])

    def test_reversal_split_retains_cash_and_fees(self):
        data, market = fixture()
        event = data['events'][4]
        event['signed_qty_btc'] = '-0.02'
        event['valuation'][PERP] = bbo(event['ts_ms'], 79500, 79510, 79500)
        extra = copy.deepcopy(event)
        extra.update(id='close-reversal', ts_ms=START + 6000, signed_qty_btc='0.01', price_usdt_per_btc='79510',
                     fee_usdt='0.1', execution_quote=bbo(START + 6000, 79500, 79510))
        extra['valuation'].pop(PERP)
        data['events'].insert(5, extra)
        for seq, e in enumerate(data['events']): e['seq'] = seq
        report, _ = integration.integrate(data, market, 'synthetic')
        self.assertEqual(report['reversal_fills_split_without_changing_total_qty_or_fee'], 1)
        self.assertEqual(report['pending_order_checks'], 6)
        self.assertTrue(report['source_ledger_unchanged'])

    def test_supplied_margin_not_overwritten(self):
        data, market = fixture()
        data['events'][0]['margin'] = {}
        with self.assertRaisesRegex(ValueError, 'OVERWRITTEN'):
            integration.integrate(data, market, 'synthetic')

    def test_missing_history_not_replaced_with_quote_midpoint(self):
        data, market = fixture()
        market['index'].clear()
        with self.assertRaisesRegex(ValueError, 'CANDLE_MISSING'):
            integration.integrate(data, market, 'synthetic')

    def test_cli_hash_rejection_no_trace_write(self):
        data, _ = fixture()
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / 'ledger.json').write_bytes(wire.encode(data))
            run = subprocess.run([sys.executable, integration.__file__, '--ledger', str(root / 'ledger.json'),
                '--ledger-sha256', '0' * 64, '--trace-output', str(root / 'trace.json')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertIn('PINNED_LEDGER_HASH_MISMATCH', run.stdout)
            self.assertFalse((root / 'trace.json').exists())


if __name__ == '__main__':
    unittest.main()
