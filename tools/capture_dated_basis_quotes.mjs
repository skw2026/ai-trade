// Bounded, public-only input diagnostic. Never reconnects or sends orders.
import fs from 'node:fs';
import path from 'node:path';
import { performance } from 'node:perf_hooks';

const output = process.argv[2];
if (!output || process.argv.length !== 3) throw new Error('usage: node capture_dated_basis_quotes.mjs NEW_DIRECTORY');
fs.mkdirSync(output, { mode: 0o700 });
const fd = fs.openSync(path.join(output, 'frames.jsonl'), 'wx', 0o600);
const start = performance.now();
let bytes = 0, frames = 0, finished = false, timer, tickTimer, hardTimer;
const sockets = [], controllers = [];
const sources = {
  spot: {
    ws: 'wss://stream.binance.com:443/ws/btcusdt@depth@100ms',
    rest: 'https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=1000',
    symbol: 'BTCUSDT',
  },
  future: {
    ws: 'wss://dstream.binance.com/ws/btcusd_261225@depth@100ms',
    rest: 'https://dapi.binance.com/dapi/v1/depth?symbol=BTCUSD_261225&limit=1000',
    symbol: 'BTCUSD_261225',
  },
};
function record(kind, source, payload) {
  if (finished) return;
  const row = { kind, source, wall_ms: Date.now(), mono_ms: performance.now() - start, payload };
  const line = JSON.stringify(row) + '\n';
  if (bytes + Buffer.byteLength(line) > 10000000 || frames >= 10000) throw new Error('CAPTURE_LIMIT');
  fs.writeSync(fd, line); bytes += Buffer.byteLength(line); frames++;
}
function finish(status, reason) {
  if (finished) return;
  finished = true;
  clearTimeout(timer); clearTimeout(hardTimer); clearInterval(tickTimer);
  for (const c of controllers) c.abort();
  for (const ws of sockets) { try { ws.close(); } catch {} }
  fs.closeSync(fd);
  const receipt = { status, reason, frames, bytes, elapsed_ms: performance.now() - start,
    websocket_attempts: sockets.length, snapshot_attempts: controllers.length,
    reconnects: 0, rest_retries: 0, node: process.version };
  fs.writeFileSync(path.join(output, 'receipt.json'), JSON.stringify(receipt, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  console.log(JSON.stringify(receipt));
  // All evidence is synchronously flushed. Force socket teardown within a fixed bound.
  setTimeout(() => process.exit(status === 'CAPTURE_COMPLETE' ? 0 : 1), 100).unref();
  setTimeout(() => process.exit(status === 'CAPTURE_COMPLETE' ? 0 : 1), 500);
}
async function snapshot(source) {
  const controller = new AbortController(); controllers.push(controller);
  const deadline = setTimeout(() => controller.abort(), 10000);
  try {
    record('snapshot_request', source, { url: sources[source].rest });
    const response = await fetch(sources[source].rest, { signal: controller.signal, redirect: 'error' });
    if (!response.ok) throw new Error('SNAPSHOT_HTTP_' + response.status);
    const chunks = []; let size = 0;
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > 1000000) throw new Error('SNAPSHOT_SIZE_LIMIT');
      chunks.push(chunk);
    }
    const raw = Buffer.concat(chunks).toString('utf8');
    const data = JSON.parse(raw);
    if (!Number.isSafeInteger(data.lastUpdateId) || !Array.isArray(data.bids) || !Array.isArray(data.asks)) throw new Error('SNAPSHOT_SHAPE');
    record('snapshot', source, { http_status: response.status, raw });
  } finally { clearTimeout(deadline); }
}
function connect(source) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(sources[source].ws); sockets.push(ws);
    let first = true;
    const deadline = setTimeout(() => reject(new Error('FIRST_FRAME_TIMEOUT_' + source)), 10000);
    ws.addEventListener('open', () => {
      try { record('open', source, { url: sources[source].ws }); }
      catch (e) { reject(e); }
    });
    ws.addEventListener('message', event => {
      if (finished) return;
      try {
        if (typeof event.data !== 'string') throw new Error('NON_TEXT_FRAME');
        const data = JSON.parse(event.data);
        if (data.e !== 'depthUpdate' || data.s !== sources[source].symbol ||
            !Number.isSafeInteger(data.E) || !Number.isSafeInteger(data.U) ||
            !Number.isSafeInteger(data.u) || !Array.isArray(data.a) || !Array.isArray(data.b) ||
            (source === 'future' && !Number.isSafeInteger(data.pu))) throw new Error('FRAME_SHAPE_' + source);
        record('update', source, { raw: event.data });
        if (first) { first = false; clearTimeout(deadline); resolve(); }
      } catch (e) { clearTimeout(deadline); reject(e); finish('INCOMPLETE', e.message); }
    });
    ws.addEventListener('error', () => {
      clearTimeout(deadline); reject(new Error('WS_ERROR_' + source));
      finish('INCOMPLETE', 'WS_ERROR_' + source);
    });
    ws.addEventListener('close', event => {
      clearTimeout(deadline); reject(new Error('EARLY_CLOSE_' + source));
      if (!finished) finish('INCOMPLETE', 'EARLY_CLOSE_' + source + '_' + event.code);
    });
  });
}
hardTimer = setTimeout(() => finish('INCOMPLETE', 'HARD_WALL_LIMIT'), 150000);
process.once('SIGINT', () => finish('INCOMPLETE', 'SIGINT'));
process.once('SIGTERM', () => finish('INCOMPLETE', 'SIGTERM'));
try {
  record('start', 'both', { capture_seconds: 120, symbols: sources });
  await Promise.all(Object.keys(sources).map(connect));
  if (!finished) await Promise.all(Object.keys(sources).map(snapshot));
  if (!finished) {
    record('sampling_start', 'both', {});
    tickTimer = setInterval(() => {
      try { record('tick', 'both', {}); } catch (e) { finish('INCOMPLETE', e.message); }
    }, 1000);
    timer = setTimeout(() => finish('CAPTURE_COMPLETE', 'FIXED_120_SECONDS'), 120000);
  }
} catch (e) { finish('INCOMPLETE', e.message); }
