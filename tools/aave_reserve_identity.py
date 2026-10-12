#!/usr/bin/env python3
"""Historical Aave WETH reserve identity, never a current-state substitute.

One first-control eth_call is permitted after its receipt/block are bound.
The returned identity is at block end; the collateral transfer must independently
match it. No receipt/flow admission, economic result or trade is emitted here.
"""
import argparse
import json
import os
from pathlib import Path

import aave_complete_input as a
import aave_complete_catalog as c
import aave_complete_receipts as r
import aave_weth_flow as f

SELECTOR='0x35ea6a75'


def request(number):
    a.require(a.hx(number) and int(number,16)>0,'HISTORICAL_BLOCK_REQUIRED')
    return [{'to':c.POOL,'data':SELECTOR+'0'*24+c.WETH[2:]},number]


def decode(raw,block):
    values=f.words(raw,15)
    widths=[256,128,128,128,128,128,40,16,160,160,160,160,128,128,128]
    a.require(all(v<2**bits for v,bits in zip(values,widths)),'RESERVE_ABI_WIDTH')
    a.require(values[8]>0,'RESERVE_ATOKEN_ZERO')
    a.require(values[6]<=int(block['timestamp'],16),'RESERVE_UPDATE_AFTER_BLOCK')
    a.require(a.hx(block['number']) and a.hx(block['hash'],64),'RESERVE_BLOCK_IDENTITY')
    return {'asset':c.WETH,'pool':c.POOL,'atoken':'0x'+format(values[8],'040x'),
            'block_number':block['number'],'block_hash':block['hash'],
            'state_position':'END_OF_BOUND_BLOCK',
            'schema':'AAVE_V3_RESERVE_DATA_STATIC_15_WORDS',
            'scope':'RESERVE_IDENTITY_ONLY_NOT_TRANSACTION_EXECUTION_PROOF'}


def collateral_transfer(receipt,indexed,identity):
    """Bind the actual underlying transfer, without fungible-unit provenance."""
    a.validate_receipt(receipt,indexed)
    a.require(identity['block_number']==receipt['blockNumber'] and
              identity['block_hash']==receipt['blockHash'] and identity['asset']==c.WETH
              and identity['pool']==c.POOL,'RESERVE_RECEIPT_BINDING')
    a.require(indexed['address'].lower()==c.POOL and indexed['topics'][:2]==
              [c.TOPIC,'0x'+'0'*24+c.WETH[2:]],'NOT_TARGET_LIQUIDATION')
    _,amount,liquidator,receive_atoken=f.words(indexed['data'],4)
    a.require(liquidator<2**160 and receive_atoken==0,'UNDERLYING_LIQUIDATION_REQUIRED')
    recipient='0x'+format(liquidator,'040x')
    matches=[]
    for log in receipt['logs']:
        if log['address'].lower()==c.WETH and log['topics'][:1]==[f.TRANSFER]:
            move=f.parse_weth(log)
            if move['from']==identity['atoken'] and move['to']==recipient and move['amount_wei']==amount:
                a.require(int(log['logIndex'],16)<int(indexed['logIndex'],16),
                          'COLLATERAL_TRANSFER_AFTER_LIQUIDATION_EVENT')
                matches.append(int(log['logIndex'],16))
    a.require(len(matches)==1,'COLLATERAL_TRANSFER_NOT_UNIQUE')
    return {'log_index':matches[0],'from':identity['atoken'],'to':recipient,'amount_wei':amount,
            'role':'MATCHED_AAVE_UNDERLYING_COLLATERAL_TRANSFER',
            'sold_unit_provenance':'UNKNOWN'}


def control():
    a.protect()
    c.budget()
    row=a.first_transactions()[0]
    receipt=r.stored('receipt0-corrected','eth_getTransactionReceipt',[row['transactionHash']])
    block=r.stored('block','eth_getBlockByNumber',[row['blockNumber'],False])
    a.validate_receipt(receipt,row)
    a.validate_block(block,row,receipt)
    def check(value):
        collateral_transfer(receipt,row,decode(value,block))
    raw=r.fetch('reserve-control-0','eth_call',request(row['blockNumber']),check)
    identity=decode(raw,block)
    output={'status':'FIRST_HISTORICAL_RESERVE_AND_COLLATERAL_IDENTITY_BOUND',
            'identity':identity,'collateral_transfer':collateral_transfer(receipt,row,identity),
            'source_sha256':a.digest(Path(__file__).read_bytes()),
            'response_sha256':a.digest((a.ROOT/'operations/reserve-control-0/response.json').read_bytes()),
            'receipt_sha256':a.digest((a.ROOT/'operations/receipt0-corrected/response.json').read_bytes()),
            'block_sha256':a.digest((a.ROOT/'operations/block/response.json').read_bytes()),
            'whole_weth_flow_admitted':False,'economic_attempts':0}
    a.save(a.ROOT/'reserve-control-0-binding.json',output)
    a.protect()
    print(json.dumps(output,sort_keys=True))


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['control'])
    parser.parse_args()
    control()
