"""Post-fill fee-matching identity, not a causal order policy or risk engine."""
from decimal import Decimal as D, localcontext

from dated_basis_execution_bridge import AssetLedger, Instrument, decimal, rate


def target_from_confirmed_fills(fills, observation_ms, face_per_contract='100'):
    if type(observation_ms) is not int or observation_ms<0 or not fills:
        raise ValueError('CONFIRMED_INPUT_REQUIRED')
    identities=set();basis=fees=face=D(0)
    with localcontext() as ctx:
        ctx.prec=50
        multiplier=decimal(face_per_contract,True)
        for fill in fills:
            if (fill.get('confirmed') is not True or fill.get('fee_asset')!='BTC'
                    or type(fill.get('ms')) is not int or not 0<=fill['ms']<=observation_ms
                    or not isinstance(fill.get('id'),str) or not fill['id']
                    or fill['id'] in identities):
                raise ValueError('CONFIRMED_INPUT_REQUIRED')
            identities.add(fill['id'])
            quantity=decimal(fill['contracts'],True)
            if quantity%1:raise ValueError('CONTRACT_QUANTITY')
            n=quantity*multiplier;p=decimal(fill['price'],True)
            fee=decimal(fill['fee_btc'])
            face+=n;basis+=n/p;fees+=fee
        return dict(face_usd=face,inverse_basis_btc=basis,entry_fees_btc=fees,
                    target_net_btc=basis+fees,asof_ms=observation_ms,
                    authority='POST_FILL_DIAGNOSTIC_NOT_ORDER',orders_authorized=False)


def gross_spot_target(spot,net,fee_asset,fee_rate='.002'):
    net=decimal(net,True);fee=rate(fee_rate)
    if fee_asset not in ('BTC','USDT'):raise ValueError('FEE_ASSET')
    gross=spot.round_up(net/(1-fee) if fee_asset=='BTC' else net)
    return dict(gross=gross,net=gross*(1-fee) if fee_asset=='BTC' else gross,
                actual_execution_qualified=False)


def synthetic_path(spot,future,fill_spec,asset,policy,terminal):
    """Known synthetic prices test an identity; sizing is explicitly not causal."""
    if policy not in ('ORIGINAL','FEE_MATCHED'):raise ValueError('POLICY')
    with localcontext() as ctx:
        ctx.prec=50
        price=decimal(terminal,True);entry_spot=D(49000);sf=D('.002');ff=D('.001')
        fills=[dict(id='f'+str(i),confirmed=True,ms=10+i,contracts=str(q),price=str(p),fee_asset='BTC',
                    fee_btc=str(D(q)*future.face_usd/D(p)*ff)) for i,(q,p) in enumerate(fill_spec)]
        target=target_from_confirmed_fills(fills,100,str(future.face_usd))
        needed=target['target_net_btc'] if policy=='FEE_MATCHED' else target['inverse_basis_btc']
        sizing=gross_spot_target(spot,needed,asset)
        ledger=AssetLedger(spot,future)
        def event(identity,ms,kind,**kwargs):
            ledger.apply(dict(id=identity,ms=ms,kind=kind,**kwargs))
        event('buy',1,'SPOT_BUY',qty=str(sizing['gross']),price=str(entry_spot),fee_asset=asset,fee_rate=str(sf))
        event('send',2,'TRANSFER_START',qty=str(sizing['net']))
        event('received',3,'TRANSFER_CONFIRM',qty=str(sizing['net']),transfer_id='send')
        for fill in fills:
            event(fill['id'],fill['ms'],'SHORT_FILL',qty=fill['contracts'],price=fill['price'],fee_asset='BTC',fee_rate=str(ff))
        event('settle',200,'SETTLE',price=str(price),fee_asset='BTC',fee_rate=str(ff))
        wallet=ledger.state['margin_btc'];residual=sizing['net']-target['target_net_btc']
        expected_wallet=target['face_usd']*(1-ff)/price+residual
        assert abs(wallet-expected_wallet)<D('1e-35')
        event('return',201,'RETURN_MARGIN_CONFIRMED')
        sell,dust=ledger.liquidation_quantity()
        event('sell',202,'SPOT_SELL',qty=str(sell),price=str(price),fee_asset='USDT',fee_rate=str(sf))
        debit=sizing['gross']*entry_spot*(1+sf if asset=='USDT' else 1)
        expected_cash=10000-debit+sell*price*(1-sf)
        assert abs(ledger.state['usdt']-expected_cash)<D('1e-35')
        assert abs(ledger.state['spot_btc']-dust)<D('1e-35')
        assert not ledger.state['contracts'] and not ledger.state['margin_btc'] and not ledger.state['transit_btc']
        assert not ledger.state['uncertain'] and not ledger.state['halted']
        expected_equity=expected_cash+dust*price
        assert abs(ledger.equity(price,price)-expected_equity)<D('1e-35')
        return dict(policy=policy,fee_asset=asset,fill_spec=fill_spec,terminal_price=str(price),
                    face_usd=str(target['face_usd']),inverse_basis_btc=str(target['inverse_basis_btc']),
                    entry_fees_btc=str(target['entry_fees_btc']),gross_spot_btc=str(sizing['gross']),
                    net_spot_btc=str(sizing['net']),entry_debit_usdt=str(debit),residual_btc=str(residual),
                    terminal_wallet_btc=str(wallet),terminal_cash_usdt=str(expected_cash),
                    terminal_dust_btc=str(dust),cash_plus_marked_dust_usdt=str(expected_equity),
                    independent_cash_and_wallet_match=True,causal_strategy=False,
                    actual_execution_qualified=False,g4=False)


def batch(spot_meta,future_meta):
    spot=Instrument.metadata(spot_meta,'spot');future=Instrument.metadata(future_meta,'future')
    if future.face_usd!=100 or spot.step()!=D('.00001'):raise ValueError('FIXED_INSTRUMENT_SEMANTICS')
    rows=[]
    for fills in ([(25,50000)],[(10,40000),(15,60000)]):
        for asset in ('USDT','BTC'):
            for policy in ('ORIGINAL','FEE_MATCHED'):
                for terminal in (25000,50000,100000):rows.append(synthetic_path(spot,future,fills,asset,policy,terminal))
    comparisons=[]
    with localcontext() as ctx:
        ctx.prec=50
        for matched in (r for r in rows if r['policy']=='FEE_MATCHED'):
            old=next(r for r in rows if r['policy']=='ORIGINAL' and all(r[k]==matched[k] for k in ('fill_spec','fee_asset','terminal_price')))
            delta=D(matched['cash_plus_marked_dust_usdt'])-D(old['cash_plus_marked_dust_usdt'])
            assert 0<=D(matched['residual_btc'])<spot.step()
            assert D(old['residual_btc'])<0
            comparisons.append(dict(fill_spec=matched['fill_spec'],fee_asset=matched['fee_asset'],
                                    terminal_price=matched['terminal_price'],equity_difference_usdt=str(delta)))
    assert len(rows)==24 and len(comparisons)==12
    assert any(D(r['equity_difference_usdt'])<0 for r in comparisons)
    assert any(D(r['equity_difference_usdt'])>0 for r in comparisons)
    return dict(summary=dict(status='FEE_MATCH_IDENTITY_VERIFIED',synthetic_paths=24,comparisons=12,
                             economic_attempts=0,orders=0,confirmation_attempts=0,g4=False,
                             strict_zero_exposure=False,unconditional_profit_improvement=False,
                             causal_entry_policy=False,actual_execution_qualified=False),
                rows=rows,comparisons=comparisons)
