#!/usr/bin/env python3
"""Classify the complete canonical WETH boundary, not global ETH exposure.

Supported evidence: bound Aave underlying collateral transfers, authenticated
V3 swaps, and WETH conversions. Every unclassified nonzero WETH transfer keeps
the whole transaction UNKNOWN. No prices, strategy admission or trading.
"""
import argparse
from collections import Counter,defaultdict
import json
import os
from pathlib import Path

import aave_complete_input as a
import aave_complete_catalog as c
import aave_complete_receipts as r
import aave_reserve_identity as q
import aave_weth_flow as f


def classify(receipt,indexed,block,reserve_raw):
    r.validate_transaction(receipt,indexed)
    for row in indexed:
        a.validate_block(block,row,receipt)
    reserve=q.decode(reserve_raw,block)
    discovered=f.discover_weth_pools(receipt)
    components=f.inspect_receipt(receipt,discovered['identities'])
    pools=set(discovered['identities'])
    a.require(reserve['atoken'] not in pools,'ATOKEN_DEX_IDENTITY_COLLISION')
    expected=defaultdict(list)
    liquidators=set()
    for row in indexed:
        _,amount,liquidator,receive_atoken=f.words(row['data'],4)
        a.require(liquidator<2**160 and receive_atoken in (0,1),'LIQUIDATION_ABI')
        liquidators.add('0x'+format(liquidator,'040x'))
        if receive_atoken==0 and amount:
            expected[('0x'+format(liquidator,'040x'),amount)].append(int(row['logIndex'],16))
    for values in expected.values():values.sort()
    collateral_seen=Counter()
    reasons=[]
    touched=set()
    for move in components['weth_movements']:
        if move['amount_wei'] and move['kind']!='APPROVAL':
            touched.update([move['from'],move['to']] if move['kind']=='TRANSFER'
                           else [move['account']])
    for address in touched.intersection(discovered['unresolved_emitters']):
        reasons.append('UNVERIFIED_SWAP_WITH_WETH_CONTACT:'+address)
    # A liquidation participant is not a blanket non-DEX allowlist. If it
    # also emits unclassified events, its additional role remains unresolved.
    participant_emitters={log['address'].lower() for log in receipt['logs']
                          if log['address'].lower() in liquidators-pools}
    for address in touched.intersection(participant_emitters):
        reasons.append('LIQUIDATOR_ADDITIONAL_ROLE_UNEXPLAINED:'+address)
    allowed_endpoints=pools|liquidators|{reserve['atoken']}
    for settlement in components['pool_settlements']:
        if not settlement['matched']:
            reasons.append('POOL_SETTLEMENT_NOT_EXPLAINED:'+settlement['pool'])
    for log in receipt['logs']:
        if log['address'].lower() in pools and log['topics'][:1]!=[f.V3_SWAP]:
            reasons.append('POOL_NON_SWAP_EVENT:'+str(int(log['logIndex'],16)))
    classified=[]
    deltas=defaultdict(int)
    for move in components['weth_movements']:
        kind,amount,index=move['kind'],move['amount_wei'],move['log_index']
        role='UNEXPLAINED'
        if kind=='APPROVAL':
            role='ALLOWANCE_NOT_BALANCE_FLOW'
        elif not amount:
            role='ZERO_AMOUNT_NOT_NET_FLOW'
        elif kind in ('DEPOSIT','WITHDRAWAL'):
            deltas[move['account']]+=amount if kind=='DEPOSIT' else -amount
            if move['account'] in pools:
                reasons.append('POOL_CONVERSION_OUTSIDE_SWAP:'+str(index))
            else:
                role='WETH_NATIVE_CONVERSION_NOT_DEX'
        elif kind=='TRANSFER':
            deltas[move['from']]-=amount
            deltas[move['to']]+=amount
            if move['from'] in pools or move['to'] in pools:
                if {move['from'],move['to']}<=allowed_endpoints:
                    role='AUTHENTICATED_POOL_SETTLEMENT'
                else:
                    reasons.append('POOL_COUNTERPARTY_ROLE_UNEXPLAINED:'+str(index))
            elif move['from']==reserve['atoken']:
                key=(move['to'],amount)
                position=collateral_seen[key]
                if position<len(expected[key]) and index<expected[key][position]:
                    collateral_seen[key]+=1
                    role='MATCHED_AAVE_UNDERLYING_COLLATERAL_TRANSFER'
            if role=='UNEXPLAINED':
                reasons.append('UNEXPLAINED_WETH_TRANSFER:'+str(index))
        classified.append(dict(move,role=role))
    for key,events in expected.items():
        if collateral_seen[key]!=len(events):
            reasons.append('UNMATCHED_AAVE_COLLATERAL_TRANSFER:'+key[0])
    verified=not reasons
    return {'status':'WETH_BOUNDARY_VERIFIED' if verified else 'WETH_BOUNDARY_UNKNOWN',
            'transaction_hash':receipt['transactionHash'],'block_hash':block['hash'],
            'weth_dex_net_sale_wei':components['known_v3_net_weth_sale_wei'] if verified else None,
            'decoded_v3_component_wei':components['known_v3_net_weth_sale_wei'],
            'unresolved_reasons':sorted(set(reasons)),'weth_ledger':classified,
            'net_weth_inventory_deltas_wei':dict(sorted(deltas.items())),
            'pool_settlements':components['pool_settlements'],
            'pool_discovery':discovered,'reserve_identity':reserve,
            'collateral_unit_provenance':'UNKNOWN',
            'native_eth_global_flow':'NOT_EVALUATED',
            'signal_admitted':False,'trade_action':None}


def inspect_first_control():
    a.protect()
    c.budget()
    row=a.first_transactions()[0]
    receipt=r.stored('receipt0-corrected','eth_getTransactionReceipt',[row['transactionHash']])
    block=r.stored('block','eth_getBlockByNumber',[row['blockNumber'],False])
    reserve=r.stored('reserve-control-0','eth_call',q.request(row['blockNumber']))
    result=classify(receipt,[row],block,reserve)
    output={'source_sha256':a.digest(Path(__file__).read_bytes()),'result':result,
            'input_bindings':{name:a.digest((a.ROOT/'operations'/name/'response.json').read_bytes())
                              for name in ['receipt0-corrected','block','reserve-control-0']},
            'new_public_operations':0,'economic_attempts':0}
    a.save(a.ROOT/'weth-boundary-control-0.json',output)
    a.protect()
    print(json.dumps({'status':result['status'],'weth_dex_net_sale_wei':result['weth_dex_net_sale_wei'],
                      'unresolved_reasons':result['unresolved_reasons'],'signal_admitted':False,
                      'new_public_operations':0,'economic_attempts':0}))


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['inspect-first-control'])
    parser.parse_args()
    inspect_first_control()
