// One 120-second public L1 diagnostic. No credentials, orders, REST or reconnect.
import fs from 'node:fs';
import path from 'node:path';
import { performance } from 'node:perf_hooks';
import { pathToFileURL } from 'node:url';

export const expiries = ['23OCT26','30OCT26','06NOV26','27NOV26',
  '25DEC26','26MAR27','25JUN27','24SEP27'].map(x => 'BTCUSDT-' + x);
export const sources = {
  spot: { url: 'wss://stream.bybit.com/v5/public/spot', symbols: ['BTCUSDT'] },
  linear: { url: 'wss://stream.bybit.com/v5/public/linear', symbols: expiries },
};
export function subscription(source) {
  if (!Object.hasOwn(sources, source)) throw new Error('UNKNOWN_SOURCE');
  return { op: 'subscribe', req_id: 'obs-' + source,
    args: sources[source].symbols.map(s => 'orderbook.1.' + s) };
}
export function classify(data, source) {
  if (!data || typeof data !== 'object' || !Object.hasOwn(sources, source)) throw new Error('SHAPE');
  if (data.op === 'subscribe') {
    if (data.success !== true || (data.req_id && data.req_id !== 'obs-' + source)) throw new Error('SUBSCRIPTION_REJECTED');
    return 'ack';
  }
  if ((data.op === 'ping' && data.success === true && data.ret_msg === 'pong') || data.op === 'pong') return 'pong';
  const d = data.data;
  if (!d || data.type !== 'snapshot' || !sources[source].symbols.includes(d.s) ||
      data.topic !== 'orderbook.1.' + d.s) throw new Error('BOOK_IDENTITY_OR_TYPE');
  for (const value of [data.ts, data.cts, d.u, d.seq]) {
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error('BOOK_CLOCK_SEQUENCE');
  }
  for (const side of [d.b, d.a]) {
    if (!Array.isArray(side) || side.length > 1) throw new Error('L1_SHAPE');
    for (const level of side) {
      if (!Array.isArray(level) || level.length !== 2 || level.some(v => typeof v !== 'string' ||
          !/^\d+(?:\.\d+)?$/.test(v) || !Number.isFinite(Number(v)) || Number(v) <= 0)) throw new Error('L1_VALUE');
    }
  }
  if (d.b.length && d.a.length && Number(d.b[0][0]) >= Number(d.a[0][0])) throw new Error('L1_CROSSED');
  return 'book';
}

export function capture(output) {
  fs.mkdirSync(output, { mode: 0o700 });
  const fd = fs.openSync(path.join(output,'frames.jsonl'),'wx',0o600);
  const start = performance.now();
  const startWall = Date.now();
  const sockets = [], deadlines = [], acknowledged = new Set();
  let stopped = false, frames = 0, bytes = 0, pingTimer, endTimer;
  function record(kind, source, payload) {
    const mono = performance.now() - start;
    if (stopped || (mono > 120000 && kind !== 'start')) return;
    const line = JSON.stringify({ kind, source, mono_ms: mono, wall_ms: Date.now(), payload }) + '\n';
    if (frames >= 150000 || bytes + Buffer.byteLength(line) > 100 * 1024 * 1024) throw new Error('CAPTURE_LIMIT');
    fs.writeSync(fd,line); frames++; bytes += Buffer.byteLength(line);
  }
  function finish(status, reason) {
    if (stopped) return;
    const elapsed = performance.now() - start;
    stopped = true;
    clearTimeout(endTimer); clearInterval(pingTimer);
    for (const timer of deadlines) clearTimeout(timer);
    for (const ws of sockets) { try { ws.close(); } catch {} }
    fs.fsyncSync(fd); fs.closeSync(fd);
    const receipt = { status, reason, elapsed_ms: elapsed, start_wall_ms: startWall,
      fixed_duration_ms: 120000, observation_stop_ms: Math.min(elapsed,120000),
      frames, bytes, websocket_attempts: sockets.length, acknowledgements: acknowledged.size,
      reconnects: 0, rest_requests: 0, account_requests: 0, node: process.version };
    fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n',{flag:'wx',mode:0o600});
    console.log(JSON.stringify(receipt));
    setTimeout(() => process.exit(status === 'CAPTURE_COMPLETE' ? 0 : 1),200);
  }
  endTimer = setTimeout(() => finish('CAPTURE_COMPLETE','FIXED_120_SECONDS'),120000);
  process.once('SIGINT',() => finish('INCOMPLETE','SIGINT'));
  process.once('SIGTERM',() => finish('INCOMPLETE','SIGTERM'));
  try {
    record('start','all',{duration_ms:120000,sources});
    for (const source of Object.keys(sources)) {
      const ws = new WebSocket(sources[source].url); sockets.push(ws);
      const deadline = setTimeout(() => finish('INCOMPLETE','ACK_TIMEOUT_' + source),10000);
      deadlines.push(deadline);
      ws.addEventListener('open',() => {
        if (stopped) return;
        try {
          record('open',source,{url:sources[source].url});
          const request = subscription(source);
          record('subscribe_send',source,request); ws.send(JSON.stringify(request));
        } catch (error) { finish('INCOMPLETE',error.message); }
      });
      ws.addEventListener('message',event => {
        if (stopped) return;
        if (performance.now()-start > 120000) { finish('CAPTURE_COMPLETE','FIXED_120_SECONDS'); return; }
        try {
          if (typeof event.data !== 'string' || Buffer.byteLength(event.data)>65536) throw new Error('FRAME_SIZE_OR_TYPE');
          record('message',source,{raw:event.data});
          const kind = classify(JSON.parse(event.data),source);
          if (kind==='ack') { acknowledged.add(source); clearTimeout(deadline); }
        } catch (error) { finish('INCOMPLETE',error.message); }
      });
      ws.addEventListener('error',() => finish('INCOMPLETE','WS_ERROR_' + source));
      ws.addEventListener('close',event => { if (!stopped) finish('INCOMPLETE','EARLY_CLOSE_' + source + '_' + event.code); });
    }
    pingTimer = setInterval(() => {
      if (stopped) return;
      try {
        for (let i=0; i<sockets.length; i++) if (sockets[i].readyState === WebSocket.OPEN) {
          const source = Object.keys(sources)[i];
          record('ping_send',source,{op:'ping'}); sockets[i].send(JSON.stringify({op:'ping'}));
        }
      } catch (error) { finish('INCOMPLETE',error.message); }
    },20000);
  } catch (error) { finish('INCOMPLETE',error.message); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (process.argv.length!==3) throw new Error('usage: node capture_bybit_linear_observability.mjs NEW_DIRECTORY');
  capture(process.argv[2]);
}
