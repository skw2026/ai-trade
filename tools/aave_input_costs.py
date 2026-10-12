"""Account for all known complete-batch inputs before native bulk acquisition.

This is an acquisition-cost plan, not a market-data or economic admission.
It cannot fetch data. Price access still requires a separate frozen economic
contract. Unknown extra identity work is bounded by a reserved allowance.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

import aave_complete_input as a
import aave_complete_catalog as c
import aave_complete_receipts as r

MARKET_START = '2023-12-31T23:00:00+00:00'
MARKET_END = '2025-01-01T00:00:00+00:00'
FUNDING_START = '2024-01-01T00:00:00+00:00'
ADDITIONAL_OPERATION_RESERVE = 200


def market_cost():
    start, end, funding_start = (dt.datetime.fromisoformat(x) for x in
                                 (MARKET_START, MARKET_END, FUNDING_START))
    minutes = int((end-start).total_seconds())//60
    days = (end-funding_start).days
    pages = (minutes+999)//1000
    return {'symbol': 'ETHUSDT', 'category': 'linear', 'interval_minutes': 1,
            'start_utc': MARKET_START, 'end_utc_exclusive': MARKET_END,
            'minute_rows_per_series': minutes, 'trade_pages': pages, 'mark_pages': pages,
            'funding_start_utc': FUNDING_START, 'funding_day_pages': days,
            'funding_limit': 200, 'funding_capped_page': 'STOP_INCOMPLETE_NEVER_ASSUME_COMPLETE',
            'total_requests': 2*pages+days,
            'price_access': 'DISABLED_UNTIL_SEPARATE_ECONOMIC_CONTRACT_AND_INPUT_CAPACITY'}


def reserve_name(number):
    for index, row in enumerate(a.first_transactions()):
        if row['blockNumber'] == number:
            return 'reserve-control-%d' % index
    return 'native-reserve-'+str(int(number, 16))


def calculate(transactions, blocks, cached_receipts, cached_blocks, cached_reserves,
              consumed, maximum=10000):
    for value in (transactions, blocks, cached_receipts, cached_blocks, cached_reserves,
                  consumed, maximum):
        a.require(type(value) is int and value >= 0, 'INVALID_REQUEST_COUNTS')
    a.require(blocks <= transactions and cached_receipts <= transactions and
              cached_blocks <= blocks and cached_reserves <= blocks, 'INVALID_CACHED_COUNTS')
    native = transactions-cached_receipts+blocks-cached_blocks+blocks-cached_reserves
    market = market_cost()
    projected = consumed+native+market['total_requests']+ADDITIONAL_OPERATION_RESERVE
    return {'transactions': transactions, 'blocks': blocks, 'public_operations_consumed': consumed,
            'missing_receipts': transactions-cached_receipts, 'missing_blocks': blocks-cached_blocks,
            'missing_reserve_calls': blocks-cached_reserves, 'native_requests': native,
            'market': market, 'additional_operation_reserve': ADDITIONAL_OPERATION_RESERVE,
            'projected_total_operations_with_reserve': projected, 'maximum_operations': maximum,
            'fits_request_budget': projected <= maximum,
            'input_admitted': False, 'economic_admitted': False,
            'bytes_and_latency': 'NOT_GUARANTEED_BY_REQUEST_COUNTS_ENFORCE_EACH_OPERATION',
            'reserve_scope': 'additional_identity_documentation_and_delivery_not_automatic_retries'}


def full_plan():
    a.protect()
    rows = r.catalog()
    transactions = r.grouped(rows)
    blocks = {row['blockNumber'] for row in rows}
    cached = [0, 0, 0]
    for tx, items in transactions.items():
        value = r.stored(r.receipt_name(tx), 'eth_getTransactionReceipt', [tx])
        if value is not None:
            r.validate_transaction(value, items)
            cached[0] += 1
    for number in blocks:
        if r.stored(r.block_name(number), 'eth_getBlockByNumber', [number, False]) is not None:
            cached[1] += 1
        params = [{'to': c.POOL, 'data': '0x35ea6a75'+'0'*24+c.WETH[2:]}, number]
        if r.stored(reserve_name(number), 'eth_call', params) is not None:
            cached[2] += 1
    resource = c.budget()
    result = calculate(len(transactions), len(blocks), *cached, resource['public_operations'])
    result.update(catalog_sha256=a.digest((a.ROOT/'annual-catalog.json').read_bytes()),
                  source_sha256=a.digest(Path(__file__).read_bytes()), resources=resource,
                  status='KNOWN_FULL_INPUT_REQUEST_PLAN_FITS' if result['fits_request_budget']
                  else 'KNOWN_FULL_INPUT_REQUEST_PLAN_EXCEEDS_BUDGET')
    return result


def require_bulk_plan():
    path = a.ROOT/'input-cost-plan.json'
    a.require(path.is_file(), 'FULL_INPUT_COST_PLAN_REQUIRED')
    plan = json.loads(path.read_text())
    a.require(plan['source_sha256'] == a.digest(Path(__file__).read_bytes()) and
              plan['catalog_sha256'] == a.digest((a.ROOT/'annual-catalog.json').read_bytes()),
              'FULL_INPUT_COST_PLAN_BINDING')
    a.require(plan['status'] == 'KNOWN_FULL_INPUT_REQUEST_PLAN_FITS' and
              plan['fits_request_budget'] is True and plan['market'] == market_cost(),
              'FULL_INPUT_COST_PLAN_NOT_FIT')
    # Recalculate from validated cached operations, never from stale remaining
    # counts. Any other reads reduce the same fixed total budget.
    current = full_plan()
    a.require(current['fits_request_budget'], 'REMAINING_FULL_INPUT_REQUESTS_EXCEED_BUDGET')
    return current


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--save-plan', action='store_true')
    args = parser.parse_args()
    result = full_plan()
    if args.save_plan:
        a.save(a.ROOT/'input-cost-plan.json', result)
    print(json.dumps(result, sort_keys=True))
