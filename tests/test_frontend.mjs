import assert from 'node:assert/strict';
import test from 'node:test';
import {api} from '../api.js';
import {displayedQuote, isSnapshot, latestRequest, nextMinuteReadAt, nextSettlement, querySignature} from '../view-model.js';

const now = Date.parse('2031-06-18T01:00:00Z');
const asOf = new Date(now).toISOString();
const stamp = Date.parse('2031-06-18T08:00:00Z');
const position = {
  asset: 'BTC', symbol: 'BTCUSDT', instrument: 'PERP', side: 'SHORT',
  quantity: 1, entry_price_usdt: 100, current_price: 99, value: 99, pnl: 1,
  funding_rate: 0, next_funding_time: stamp, expected_funding: 0,
};
const quote = {
  as_of: asOf, request_id: 'ready', status: 'ready', equity: 100,
  positions: [position], asset_deltas: [{asset: 'BTC', delta: 1}], issues: [],
  sources: {spot: {status: 'available'}, linear: {status: 'available'}},
  next_settlement: {
    timestamp_ms: stamp, amount_usdt: 0, included_contracts: 1, total_contracts: 1, as_of: asOf,
  },
};
const payload = {...quote, samples: [{t: now, value: 100}]};

test('reject malformed post-fetch or stored snapshot before replacing valid state', () => {
  assert(isSnapshot(payload));
  assert(isSnapshot({...payload, next_settlement: undefined}));
  assert(!isSnapshot({...payload, positions: []}));
  assert(!isSnapshot({...payload, equity: null}));
  assert(!isSnapshot({...payload, last_complete: {...quote, asset_deltas: null}}));
  assert(!isSnapshot({...payload, samples: [{t: now, value: '100'}]}));
  assert(!isSnapshot({...payload, next_settlement: {...quote.next_settlement, timestamp_ms: now}}));
  assert(!isSnapshot({...payload, next_settlement: {...quote.next_settlement, included_contracts: 2}}));
});

test('keep coherent new equity on funding-only degradation and retain it through price loss', () => {
  const fresh = {...quote, request_id: 'funding-unavailable', status: 'partial', equity: 105,
    as_of: '2031-06-18T01:00:05Z', next_settlement: undefined};
  const unavailable = {...fresh, equity: null};
  assert.equal(displayedQuote(fresh, quote, quote), fresh);
  assert.equal(displayedQuote(unavailable, quote, fresh), fresh);
  assert.equal(displayedQuote(unavailable, quote, null), quote);
  assert.equal(displayedQuote(unavailable, null, null), unavailable);
});

test('display the canonical settlement even when other positions have different estimates', () => {
  const older = {...quote, positions: [{...position, expected_funding: 8}, position]};
  const selection = nextSettlement(older, now);
  assert.deepEqual(selection, {state: 'scheduled', stamp, amount: 0, included: 1, total: 1});
  assert.deepEqual(nextSettlement(older, stamp), {state: 'expired', amount: null});
  assert.deepEqual(nextSettlement({...older, next_settlement: undefined}, now), {state: 'unavailable', amount: null});
});

test('retry a delayed minute sample without indefinitely switching to fast polling', () => {
  const previousMinute = now - 60000;
  const nextBoundary = now + 61500;
  assert.equal(nextMinuteReadAt(now, undefined, 5000), now + 1500);
  assert.equal(nextMinuteReadAt(now + 1500, previousMinute, 5000), now + 6500);
  assert.equal(nextMinuteReadAt(now + 11500, previousMinute, 5000), now + 15000);
  assert.equal(nextMinuteReadAt(now + 15000, previousMinute, 5000), nextBoundary);
  assert.equal(nextMinuteReadAt(now + 6500, now, 5000), nextBoundary);
});

test('a delayed older query cannot replace the newest response', async () => {
  const issue = latestRequest();
  let first, second;
  const read = async (response) => {
    const isCurrent = issue();
    const data = await response;
    if (isCurrent()) return data;
  };
  const older = read(new Promise((resolve) => { first = resolve; }));
  const newer = read(new Promise((resolve) => { second = resolve; }));
  second('new result');
  assert.equal(await newer, 'new result');
  first('stale result');
  assert.equal(await older, undefined);
});

test('bind results to submitted form values while normalizing date whitespace', () => {
  const sent = querySignature('capital=12000&start=2031-06-01&end=2031-06-17');
  assert.equal(sent, querySignature('start=+2031-06-01+&end=2031-06-17&capital=12000'));
  assert.notEqual(sent, querySignature('capital=24000&start=2031-06-01&end=2031-06-17'));
});

test('HTTP failures, successful malformed payloads, and valid stale metadata differ', async (t) => {
  const render = (data, status = 200) => new Response(JSON.stringify(data), {status});
  t.mock.method(globalThis, 'fetch', async () => render({issues: ['Diagnostics storage unavailable.']}, 503));
  await assert.rejects(api.health(), /Diagnostics storage unavailable/);
  globalThis.fetch.mock.mockImplementation(async () => render(null, 503));
  await assert.rejects(api.health(), /HTTP 503/);
  globalThis.fetch.mock.mockImplementation(async () => render({state: 'stale'}));
  assert.equal((await api.health()).state, 'stale');
  globalThis.fetch.mock.mockImplementation(async () => render({
    state: 'unavailable', covered_through: null,
  }));
  assert.equal((await api.historyStatus()).state, 'unavailable');
  globalThis.fetch.mock.mockImplementation(async () => render({...payload, equity: null}));
  await assert.rejects(api.snapshot(), /Unexpected data from the server/);
  globalThis.fetch.mock.mockImplementation(async () => render(payload));
  assert.equal((await api.snapshot()).equity, 100);
});
