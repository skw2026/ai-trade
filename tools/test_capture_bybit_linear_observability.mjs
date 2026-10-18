import test from 'node:test';
import assert from 'node:assert/strict';
import { classify, subscription, sources, expiries } from './capture_bybit_linear_observability.mjs';

const book = () => ({topic:'orderbook.1.BTCUSDT',type:'snapshot',ts:1000,cts:995,
  data:{s:'BTCUSDT',u:10,seq:20,b:[['100','1']],a:[['101','1']]}});
test('only two exact public sources and nine fixed topics', () => {
  assert.deepEqual(Object.keys(sources),['spot','linear']);
  assert.equal(expiries.length,8);
  assert.equal(subscription('linear').args.length,8);
  assert.deepEqual(subscription('spot').args,['orderbook.1.BTCUSDT']);
  assert.throws(() => subscription('private'));
});
test('valid L1 snapshot', () => assert.equal(classify(book(),'spot'),'book'));
test('subscription ack and rejection', () => {
  assert.equal(classify({op:'subscribe',success:true,req_id:'obs-spot'},'spot'),'ack');
  for (const success of [false,'true',1]) assert.throws(() => classify({op:'subscribe',success},'spot'));
  assert.throws(() => classify({op:'subscribe',success:true,req_id:'other'},'spot'));
});
test('pong controls do not become books', () => {
  assert.equal(classify({op:'ping',success:true,ret_msg:'pong'},'linear'),'pong');
  assert.equal(classify({op:'pong',args:['1']},'spot'),'pong');
});
test('wrong socket topic symbol and delta rejected', () => {
  assert.throws(() => classify(book(),'linear'));
  for (const [key,value] of [['topic','orderbook.50.BTCUSDT'],['type','delta']]) {
    const b=book();b[key]=value;assert.throws(() => classify(b,'spot'));
  }
  const b=book();b.data.s='ETHUSDT';assert.throws(() => classify(b,'spot'));
});
test('clock and sequence must be safe positive integers', () => {
  for (const value of [null,0,-1,1.1,'100',Infinity,Number.MAX_SAFE_INTEGER+1]) {
    const b=book();b.cts=value;assert.throws(() => classify(b,'spot'));
  }
});
test('nonfinite and nonpositive price quantity rejected', () => {
  for (const v of ['NaN','Infinity','0','-1','',true,100]) {
    const b=book();b.data.b[0][1]=v;assert.throws(() => classify(b,'spot'));
  }
});
test('empty side observable but multi-level and crossed invalid', () => {
  const b=book();b.data.b=[];assert.equal(classify(b,'spot'),'book');
  b.data.b=[['100','1'],['99','1']];assert.throws(() => classify(b,'spot'));
  b.data.b=[['101','1']];assert.throws(() => classify(b,'spot'));
});
