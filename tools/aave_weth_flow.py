"""Exact receipt-level WETH/V3 parsing, not a trading signal or total-flow proof.

V3 identities use the official v1.0.0 PoolAddress CREATE2 formula. Swap amounts
are pool balance deltas, not quantities of proven liquidation-origin collateral.
No network, price, account, strategy admission, or UNKNOWN-to-zero conversion.
"""
from functools import lru_cache
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from aave_complete_input import hx, require

WETH = '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2'
V3_FACTORY = '0x1f98431c8ad98523631ae4a59f267346ea31f984'
V3_INIT_HASH = 'e34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54'
V3_SWAP = '0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67'
TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
APPROVAL = '0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925'
# Discovery candidates, not a claim that these are the only enabled V3 fees.
DISCOVERY_FEES = (100,500,3000,10000)


@lru_cache(maxsize=16384)
def keccak(raw):
    require(isinstance(raw,bytes),'KECCAK_BYTES_REQUIRED')
    binary=shutil.which('openssl')
    require(binary is not None,'OPENSSL_NOT_FOUND')
    result=subprocess.run([binary,'dgst','-keccak-256','-binary'],input=raw,
                          capture_output=True,check=True,timeout=10)
    require(len(result.stdout)==32,'KECCAK_OUTPUT_WIDTH')
    return result.stdout


def topic(signature):
    return '0x'+keccak(signature.encode('ascii')).hex()


def addr(value):
    require(hx(value,40),'ADDRESS_WIDTH')
    return value.lower()


def topic_address(value):
    require(hx(value,64) and value[2:26]=='0'*24,'ADDRESS_TOPIC_PADDING')
    return addr('0x'+value[-40:])


def words(data,count):
    require(hx(data,64*count),'ABI_WORD_LAYOUT')
    return [int(data[2+i*64:2+(i+1)*64],16) for i in range(count)]


def signed(value,bits=256):
    require(type(value) is int and 0<=value<2**256,'ABI_INTEGER_RANGE')
    result=value if value<2**255 else value-2**256
    require(-2**(bits-1)<=result<2**(bits-1),'ABI_SIGN_EXTENSION')
    return result


def pool_address(token0,token1,fee):
    token0,token1=addr(token0),addr(token1)
    require(0<int(token0,16)<int(token1,16),'POOL_TOKEN_ORDER')
    require(type(fee) is int and 0<=fee<2**24,'POOL_FEE_RANGE')
    salt=keccak(int(token0,16).to_bytes(32,'big')+int(token1,16).to_bytes(32,'big')+
                fee.to_bytes(32,'big'))
    return '0x'+keccak(b'\xff'+bytes.fromhex(V3_FACTORY[2:])+salt+bytes.fromhex(V3_INIT_HASH))[-20:].hex()


def validate_pool(address,identity):
    require(isinstance(identity,dict) and identity.get('factory','').lower()==V3_FACTORY,
            'UNSUPPORTED_FACTORY')
    require(addr(address)==pool_address(identity['token0'],identity['token1'],identity['fee']),
            'POOL_CREATE2_MISMATCH')
    return {'factory':V3_FACTORY,'token0':addr(identity['token0']),
            'token1':addr(identity['token1']),'fee':identity['fee']}


def discover_weth_pools(receipt):
    """Recover exact CREATE2 identities, without trusting token/pool labels.

    Candidate token addresses come from this receipt's emitters. A match is
    accepted only if the canonical factory/init-code formula equals the actual
    Swap emitter. No match says UNKNOWN, not non-WETH or zero net flow. Neither
    this finite search nor a match proves coverage of other DEX protocols.
    """
    require(isinstance(receipt,dict) and receipt.get('status')=='0x1',
            'RECEIPT_NOT_SUCCESSFUL')
    logs=receipt.get('logs')
    require(isinstance(logs,list),'MISSING_RECEIPT_LOGS')
    emitters={addr(log['address']) for log in logs}
    targets={addr(log['address']) for log in logs if log.get('topics',[])[:1]==[V3_SWAP]}
    matches={}
    for token in sorted(emitters-{WETH,'0x'+'0'*40}):
        token0,token1=sorted((token,WETH))
        for fee in DISCOVERY_FEES:
            candidate=pool_address(token0,token1,fee)
            if candidate in targets:
                require(candidate not in matches,'AMBIGUOUS_CREATE2_IDENTITY')
                matches[candidate]={'factory':V3_FACTORY,'token0':token0,
                                    'token1':token1,'fee':fee}
    return {'identities':matches,'unresolved_emitters':sorted(targets-set(matches)),
            'method':'CANONICAL_CREATE2_EXACT_ADDRESS_MATCH',
            'token_candidates':'CURRENT_RECEIPT_EMITTERS',
            'fee_candidates':list(DISCOVERY_FEES),
            'coverage':'FINITE_WETH_PAIR_SEARCH_NOT_ALL_DEX'}


def parse_v3_swap(log,identity):
    pool=validate_pool(log['address'],identity)
    topics=log['topics']
    require(len(topics)==3 and topics[0]==V3_SWAP,'SWAP_TOPICS')
    sender,recipient=topic_address(topics[1]),topic_address(topics[2])
    amount0,amount1,price,liquidity,tick=words(log['data'],5)
    amount0,amount1=signed(amount0),signed(amount1)
    require(amount0*amount1<=0 and not (amount0<0 and amount1<0),'SWAP_DIRECTION')
    require(0<price<2**160 and liquidity<2**128,'SWAP_UNSIGNED_WIDTH')
    tick=signed(tick,24)
    weth_delta=amount0 if pool['token0']==WETH else amount1 if pool['token1']==WETH else 0
    return {'pool':addr(log['address']),'sender':sender,'recipient':recipient,
            'amount0':amount0,'amount1':amount1,'sqrt_price_x96':price,
            'liquidity':liquidity,'tick':tick,'weth_pool_delta_wei':weth_delta,
            'weth_in_pair':WETH in (pool['token0'],pool['token1'])}


def parse_weth(log):
    require(addr(log['address'])==WETH,'NOT_WETH')
    topics=log['topics']
    require(bool(topics),'WETH_ANONYMOUS_EVENT')
    amount=words(log['data'],1)[0]
    if topics[0] in (TRANSFER,APPROVAL):
        require(len(topics)==3,'WETH_TOKEN_TOPICS')
        return {'kind':'TRANSFER' if topics[0]==TRANSFER else 'APPROVAL',
                'from':topic_address(topics[1]),'to':topic_address(topics[2]),'amount_wei':amount}
    kind={topic('Deposit(address,uint256)'):'DEPOSIT',topic('Withdrawal(address,uint256)'):'WITHDRAWAL'}.get(topics[0])
    require(kind is not None and len(topics)==2,'UNSUPPORTED_WETH_EVENT')
    return {'kind':kind,'account':topic_address(topics[1]),'amount_wei':amount}


def inspect_receipt(receipt,identities):
    """Expose exact components and unresolved evidence; never return a trade action."""
    require(isinstance(receipt,dict) and receipt.get('status')=='0x1','RECEIPT_NOT_SUCCESSFUL')
    logs=receipt.get('logs')
    require(isinstance(logs,list),'MISSING_RECEIPT_LOGS')
    pools={addr(address):validate_pool(address,value) for address,value in identities.items()}
    weth=[]
    swaps=[]
    unknown_pools=set()
    seen=set()
    for log in logs:
        require(hx(log.get('logIndex')),'LOG_INDEX')
        index=int(log['logIndex'],16)
        require(index not in seen,'DUPLICATE_LOG_INDEX')
        seen.add(index)
        address=addr(log['address'])
        if address==WETH:
            weth.append(dict(parse_weth(log),log_index=index))
        if log.get('topics',[])[:1]==[V3_SWAP]:
            if address not in pools:
                unknown_pools.add(address)
            else:
                swaps.append(dict(parse_v3_swap(log,pools[address]),log_index=index))
    settlements=[]
    for address in sorted({s['pool'] for s in swaps if s['weth_in_pair']}):
        delta=sum(s['weth_pool_delta_wei'] for s in swaps if s['pool']==address)
        transfer_delta=sum((int(r['to']==address)-int(r['from']==address))*r['amount_wei']
                           for r in weth if r['kind']=='TRANSFER')
        settlements.append({'pool':address,'swap_delta_wei':delta,'transfer_delta_wei':transfer_delta,
                            'matched':delta==transfer_delta})
    return {'status':'COMPONENTS_ONLY_NOT_TOTAL_FLOW_ADMITTED',
            'known_v3_net_weth_sale_wei':sum(s['weth_pool_delta_wei'] for s in swaps),
            'unverified_v3_emitters':sorted(unknown_pools),'pool_settlements':settlements,
            'weth_movements':weth,'verified_v3_swaps':swaps,
            'native_unwrap_observed':any(r['kind']=='WITHDRAWAL' and r['amount_wei']>0 for r in weth),
            'transaction_total_dex_net_flow':'UNKNOWN',
            'liquidation_collateral_provenance':'NOT_PROVED',
            'trade_action':None}


def inspect_fixed_controls():
    """Read the already frozen three controls only; no additional acquisition."""
    import aave_complete_input as a
    import aave_complete_catalog as c
    import aave_complete_receipts as r
    a.protect()
    resources=c.budget()
    output=[]
    for row in a.first_transactions():
        name=r.receipt_name(row['transactionHash'])
        receipt=r.stored(name,'eth_getTransactionReceipt',[row['transactionHash']])
        a.validate_receipt(receipt,row)
        discovery=discover_weth_pools(receipt)
        output.append({'transaction_hash':row['transactionHash'],
                       'receipt_sha256':a.digest((a.ROOT/'operations'/name/'response.json').read_bytes()),
                       'discovery':discovery,
                       'components':inspect_receipt(receipt,discovery['identities'])})
    report={'status':'FIXED_CONTROL_COMPONENT_INSPECTION_NOT_SIGNAL_ADMISSION',
            'source_sha256':a.digest(Path(__file__).read_bytes()),
            'controls':output,'resources':resources,'new_public_operations':0,
            'new_prices':0,'economic_attempts':0}
    path=a.ROOT/'weth-control-components.json'
    raw=(json.dumps(report,sort_keys=True,indent=2)+'\n').encode()
    a.require(c.budget()['input_bytes_conservative']+len(raw)<=a.MAX_INPUT,
              'COMPONENT_OUTPUT_BYTE_BUDGET')
    with path.open('xb') as handle:
        handle.write(raw)
    a.protect()
    print(json.dumps({'status':report['status'],'output':str(path),
                      'controls':len(output),'matched_pools':sum(len(x['discovery']['identities']) for x in output),
                      'all_dex_flow_admitted':False,'new_public_operations':0}))


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['inspect-fixed-controls'])
    parser.parse_args()
    inspect_fixed_controls()
