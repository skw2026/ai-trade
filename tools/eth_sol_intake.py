"""Bounded public contract-identity intake for the accepted ETH/SOL program.

No price history, economics, trading credentials or strategy activation here.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from aave_market_recovery import unpack
from mvp_reference_inputs import strict_json, number

ROOT = Path('.artifacts/eth-sol-program-20261015')
PRIOR = Path('.artifacts/eth-sol-validation-design-20261014')
PROPOSAL = Path('docs/plans/2026-10-14-eth-sol-bounded-validation.proposal.json')
ACCEPTANCE = Path('docs/plans/2026-10-15-eth-sol-execution.md')
START = dt.datetime(2026,10,15,2,14,49,tzinfo=dt.timezone.utc)
END_G0 = START+dt.timedelta(hours=2.25)
SYMBOLS = ('ETHUSDT','SOLUSDT')
WIRE = dict(maximum_body_bytes=2*1024*1024, maximum_header_bytes=32768)


def need(ok, reason):
    if not ok: raise ValueError(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')


def sources():
    return {p:sha(p) for p in ('tools/eth_sol_intake.py','tools/test_eth_sol_intake.py',
        'tools/aave_market_recovery.py','tools/aave_market_input.py','tools/aave_economic_model.py',
        'tools/mvp_reference_inputs.py','tools/validation_gate.py')}


def guard():
    need(strict_json((ROOT/'validation-state.json').read_bytes())['status']=='RUNNING', 'PROGRAM_GATE_NOT_RUNNING')
    now = dt.datetime.now(dt.timezone.utc)
    need(START<=now<END_G0, 'G0_TIME_BUDGET')
    if (ROOT/'freeze.json').exists():
        f = strict_json((ROOT/'freeze.json').read_bytes())
        need(f['proposal_sha256']==sha(PROPOSAL) and f['acceptance_sha256']==sha(ACCEPTANCE), 'ACCEPTED_SCOPE_CHANGED')
        need(f['sources']==sources(), 'INTAKE_SOURCE_CHANGED')


def preserve():
    f = strict_json((ROOT/'freeze.json').read_bytes())
    need(all(sha(p)==h for p,h in f['protected'].items()), 'PRIOR_EVIDENCE_CHANGED')
    return len(f['protected'])


def validate_identity(raw, symbol):
    need(symbol in SYMBOLS, 'SYMBOL_OUT_OF_SCOPE')
    obj = strict_json(raw)
    need(type(obj.get('retCode')) is int and obj['retCode']==0, 'PUBLIC_API_ERROR')
    r = obj['result']
    need(r['category']=='linear' and len(r['list'])==1, 'IDENTITY_CARDINALITY')
    v = r['list'][0]
    need(v['symbol']==symbol and v['baseCoin']==symbol[:-4] and
        v['quoteCoin']==v['settleCoin']=='USDT', 'INSTRUMENT_IDENTITY')
    need(v['contractType']=='LinearPerpetual' and v['status']=='Trading', 'INSTRUMENT_STATUS')
    need(str(v['launchTime']).isdigit() and 0<int(v['launchTime'])<1704067200000, 'INSTRUMENT_LAUNCH')
    fields = dict(price_tick=v['priceFilter']['tickSize'], quantity_step=v['lotSizeFilter']['qtyStep'],
        minimum_quantity=v['lotSizeFilter']['minOrderQty'], minimum_notional=v['lotSizeFilter']['minNotionalValue'])
    need(all(number(x)>0 for x in fields.values()), 'INSTRUMENT_FILTERS')
    return dict(symbol=symbol, contract_type=v['contractType'], launch_ms=int(v['launchTime']),
        current_filters=fields, historical_filters_verified=False, historical_returns_read=False)


def freeze():
    os.umask(0o077)
    guard()
    f = strict_json((PRIOR/'freeze.json').read_bytes())
    protected = dict(f['protected'])
    need(all(sha(p)==h for p,h in protected.items()), 'PRIOR_EVIDENCE_CHANGED')
    for p in PRIOR.rglob('*'):
        if p.is_file() and p.suffix!='.lock' and '__pycache__' not in p.parts:
            protected[str(p)] = sha(p)
    for p in [PROPOSAL, PROPOSAL.with_name('2026-10-14-eth-sol-bounded-validation.md')]:
        protected[str(p)] = sha(p)
    proposal = strict_json(PROPOSAL.read_bytes())
    need(proposal['budget']['engineering_hours']==32 and proposal['candidate_count']==1, 'PROPOSAL_SCOPE')
    (ROOT/'identity-attempts').mkdir(mode=0o700)
    save(ROOT/'freeze.json', dict(protected=protected, sources=sources(), proposal_sha256=sha(PROPOSAL),
        acceptance_sha256=sha(ACCEPTANCE), approved_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        baseline_commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
        prior_charged_seconds=6300, engineering_budget_seconds=115200,
        active_interval_start=START.isoformat(), g0_deadline=END_G0.isoformat(),
        planned_identity_requests=[f'https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={s}' for s in SYMBOLS],
        market_attempt_limit=1500, raw_byte_limit=1073741824, economics_frozen=False))
    print(json.dumps(dict(status='PROGRAM_ACCEPTED_INPUT_SCOPE_FROZEN', protected_files=preserve(),
        economic_attempts=0, confirmation_started=False)),flush=True)


def identities():
    os.umask(0o077)
    guard(); preserve()
    f = strict_json((ROOT/'freeze.json').read_bytes())
    need(not any((ROOT/'identity-attempts').iterdir()), 'IDENTITY_ATTEMPT_ALREADY_STARTED')
    results = []
    for i, (symbol,url) in enumerate(zip(SYMBOLS,f['planned_identity_requests'])):
        guard()
        folder = ROOT/'identity-attempts'/str(i)
        folder.mkdir(mode=0o700)
        save(folder/'started.json', dict(symbol=symbol,url=url,at=dt.datetime.now(dt.timezone.utc).isoformat()))
        started = time.monotonic(); wire = b''
        record = dict(symbol=symbol,url=url,status='FAILED')
        try:
            p = subprocess.run(['curl','--disable','--silent','--proto','=https','--include',
                '--suppress-connect-headers','--retry','0','--connect-timeout','15',
                '--max-time','30','--max-filesize',str(WIRE['maximum_body_bytes']),
                '--write-out','\n%{http_code}',url],capture_output=True,timeout=35)
            wire = p.stdout
            record['curl_returncode'] = p.returncode
            need(p.returncode==0, 'IDENTITY_TRANSPORT')
            status,headers,_,body = unpack(wire,WIRE)
            record['http_status'] = status
            need(status==200, 'IDENTITY_HTTP')
            value = validate_identity(body,symbol)
            record.update(status='VALID',summary=value)
            results.append(value)
        except Exception as exc:
            record['error_type'] = type(exc).__name__
            record['error'] = str(exc)[:180]
            raise
        finally:
            with (folder/'response.bin').open('xb') as stream: stream.write(wire)
            record.update(wire_sha256=hashlib.sha256(wire).hexdigest(),retained_bytes=len(wire),
                elapsed_seconds=time.monotonic()-started)
            save(folder/'record.json',record)
        print(json.dumps(dict(status='CURRENT_IDENTITY_VERIFIED',symbol=symbol)),flush=True)
    save(ROOT/'identities.json',dict(instruments=results,request_attempts=2,
        g0_complete=False, economic_attempts=0))
    preserve()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('freeze','identities'))
    args=parser.parse_args()
    {'freeze':freeze,'identities':identities}[args.action]()
