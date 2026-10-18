import test from 'node:test';
import assert from 'node:assert/strict';
import {classify,subscription,sources,expiries} from './capture_bybit_linear_depth.mjs';
const book=()=>({topic:'orderbook.50.BTCUSDT',type:'snapshot',ts:1000,cts:995,
  data:{s:'BTCUSDT',u:10,seq:20,b:[['100','1']],a:[['101','1']]}});
test('only fixed public sources and all eight expiries',()=>{
  assert.deepEqual(Object.keys(sources),['spot','linear']);assert.equal(expiries.length,8);
  assert.deepEqual(subscription('spot').args,['orderbook.50.BTCUSDT']);
  assert.equal(subscription('linear').args.length,8);assert.throws(()=>subscription('private'));
});
test('snapshot valid',()=>assert.equal(classify(book(),'spot'),'book'));
test('delta zero removes but negative and snapshot zero rejected',()=>{
  const b=book();b.type='delta';b.data.b[0][1]='0';assert.equal(classify(b,'spot'),'book');
  b.type='snapshot';assert.throws(()=>classify(b,'spot'));
  b.type='delta';b.data.b[0][1]='-1';assert.throws(()=>classify(b,'spot'));
});
test('ACKs explicit success and source identity',()=>{
  assert.equal(classify({op:'subscribe',success:true,req_id:'obs-spot'},'spot'),'ack');
  for(const success of [false,1,'true'])assert.throws(()=>classify({op:'subscribe',success},'spot'));
  assert.throws(()=>classify({op:'subscribe',success:true,req_id:'obs-linear'},'spot'));
});
test('control pongs',()=>{
  assert.equal(classify({op:'pong'},'spot'),'pong');
  assert.equal(classify({op:'ping',success:true,ret_msg:'pong'},'linear'),'pong');
});
test('wrong source topic symbol message type rejected',()=>{
  assert.throws(()=>classify(book(),'linear'));
  for(const [key,v] of [['topic','orderbook.1.BTCUSDT'],['type','update']]){
    const b=book();b[key]=v;assert.throws(()=>classify(b,'spot'));
  }
  const b=book();b.data.s='ETHUSDT';assert.throws(()=>classify(b,'spot'));
});
test('safe positive integer clock sequence',()=>{
  for(const v of [null,0,-1,1.1,'100',Infinity,Number.MAX_SAFE_INTEGER+1]){
    const b=book();b.cts=v;assert.throws(()=>classify(b,'spot'));
  }
});
test('strict finite decimal strings',()=>{
  for(const v of ['NaN','Infinity','0','-1','',true,100,'.1']){
    const b=book();b.data.b[0][1]=v;assert.throws(()=>classify(b,'spot'));
  }
});
test('fifty snapshot levels and bounded delta',()=>{
  const b=book();b.data.b=Array.from({length:50},(_,i)=>[String(100-i),'1']);
  assert.equal(classify(b,'spot'),'book');b.data.b.push(['50','1']);assert.throws(()=>classify(b,'spot'));
  b.type='delta';assert.equal(classify(b,'spot'),'book');
  b.data.b=Array.from({length:1001},()=>['1','0']);assert.throws(()=>classify(b,'spot'));
});
test('empty snapshot not claimed tradable and crossed snapshot rejected',()=>{
  const b=book();b.data.b=[];assert.equal(classify(b,'spot'),'book');
  b.data.b=[['101','1']];assert.throws(()=>classify(b,'spot'));
});
