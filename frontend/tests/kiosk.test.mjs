import {test} from 'node:test';
import assert from 'node:assert/strict';
import {KioskFlow, QrGate} from '../kiosk.js';
import {PendingAttempt} from '../pending.js';
import {CameraScanner} from '../camera.js';

function fixture() {
  const data = new Map(), storage = {getItem: k => data.get(k) ?? null, setItem: (k,v) => data.set(k,v), removeItem: k => data.delete(k)};
  const store = new PendingAttempt(storage, 'event', 'origin'), calls = [], states = [], timers = [];
  let ready = 0, committed = null, loseResponse = false, offline = false, lookupStatus = 'found', status = 'accepted';
  const person = {participant_id: 'p', name: 'Synthetic Guest', tier: 'Pass', price_category: 'Regular', eligible: true};
  const api = async (path, body) => {
    calls.push({path, body});
    if (offline) throw new Error('Backend unavailable');
    if (path === 'lookup') return {status: lookupStatus, participant: person};
    if (path.startsWith('requests/')) return {resolved: Boolean(committed), result: committed};
    assert.ok(store.read(), 'request persisted before POST');
    assert.equal(store.read().request_id, body.request_id);
    committed = {status, request_id: body.request_id, participant: {...person, checked_in_at: 123, checked_in_by: 'self checkin', method: 'qr'}};
    if (loseResponse) throw new Error('Response lost');
    return committed;
  };
  const create = () => new KioskFlow({api, store: new PendingAttempt(storage, 'event', 'origin'), show: state => states.push(state), ready: () => ready++, uuid: () => 'one-durable-id',
    schedule: (fn, ms) => {const timer = {fn, ms}; timers.push(timer); return timer;}, cancel: timer => {if (timer) timer.cancelled = true;}});
  return {store, calls, states, timers, create, ready: () => ready, committed: () => committed,
    set: values => {if ('loseResponse' in values) loseResponse = values.loseResponse; if ('offline' in values) offline = values.offline; if ('lookupStatus' in values) lookupStatus = values.lookupStatus; if ('status' in values) status = values.status;}};
}

test('automatic check-in persists one ID and welcomes only after backend commit; resumes after interval', async () => {
  const f = fixture(), flow = f.create();
  await flow.scan('qr');
  assert.deepEqual(f.calls.map(c => c.path), ['lookup', 'requests/one-durable-id', 'checkins']);
  assert.equal(f.calls[2].body.method, 'qr');
  assert.equal(f.states.at(-1).title, 'Welcome, Synthetic Guest!');
  assert.equal(f.committed().participant.checked_in_by, 'self checkin');
  assert.equal(f.store.read(), null); assert.equal(f.ready(), 0);
  await flow.scan('another'); assert.equal(f.calls.length, 3);
  assert.equal(f.timers.at(-1).ms, 5000); f.timers.at(-1).fn(); assert.equal(f.ready(), 1);
});

test('response lost after commit stays pending; refresh reconciles same ID without another POST', async () => {
  const f = fixture(); f.set({loseResponse: true});
  const original = f.create(); await original.scan('qr');
  assert.equal(f.states.at(-1).title, 'Confirmation pending');
  assert.equal(f.states.some(s => s.kind === 'success'), false);
  assert.equal(f.store.read().request_id, 'one-durable-id'); assert.equal(f.store.read().kiosk, true);
  await original.scan('another'); assert.equal(f.calls.length, 3);
  original.dispose(); const refreshed = f.create(); await refreshed.recover();
  assert.equal(f.states.at(-1).kind, 'success');
  assert.equal(f.calls.filter(c => c.path === 'checkins').length, 1); assert.equal(f.store.read(), null);
});

test('outage before commit retries persisted ID after refresh and blocks another participant', async () => {
  const f = fixture();
  f.store.begin('p', 'qr', 'original-id', true); f.set({offline: true});
  const original = f.create(); await original.recover();
  assert.equal(f.states.at(-1).kind, 'pending'); assert.equal(f.ready(), 0);
  await original.scan('another'); assert.equal(f.calls.length, 1); original.dispose();
  f.set({offline: false}); const refreshed = f.create(); await refreshed.recover();
  assert.equal(f.calls.at(-1).body.request_id, 'original-id'); assert.equal(f.states.at(-1).kind, 'success');
});

test('automatic retry reconciles a lost response using the same request', async () => {
  const f = fixture(); f.set({loseResponse: true}); await f.create().scan('qr');
  const retry = f.timers.at(-1); assert.equal(retry.ms, 3000); retry.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.states.at(-1).kind, 'success'); assert.equal(f.calls.filter(c => c.path === 'checkins').length, 1);
});

test('an unconfirmed or mismatched backend response never welcomes or clears the durable request', async () => {
  const data = new Map();
  const store = new PendingAttempt({getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value)}, 'event', 'origin');
  store.begin('p', 'qr', 'request', true);
  const states = [];
  const flow = new KioskFlow({store, show: state => states.push(state), ready: () => assert.fail('must remain paused'), schedule: () => 1,
    api: async () => ({resolved: true, result: {request_id: 'wrong-request', status: 'accepted', participant: {name: 'Guest', checked_in_at: 123}}})});
  await flow.recover();
  assert.equal(states.at(-1).kind, 'pending'); assert.equal(states.at(-1).sound, 'error');
  assert.equal(states.some(state => state.kind === 'success'), false);
  assert.equal(store.read().request_id, 'request'); flow.dispose();
});

test('duplicate and ineligible tickets are transactional results; unknown QR never submits attendance', async () => {
  for (const [status, kind] of [['duplicate', 'duplicate'], ['invalid', 'invalid']]) {
    const f = fixture(); f.set({status}); await f.create().scan('qr');
    assert.equal(f.states.at(-1).kind, kind); assert.equal(f.calls.at(-1).path, 'checkins');
    if (status === 'duplicate') assert.equal(f.states.at(-1).participant.checked_in_at, 123);
  }
  for (const lookupStatus of ['invalid', 'unknown']) {
    const f = fixture(); f.set({lookupStatus}); await f.create().scan('bad');
    assert.equal(f.states.at(-1).title, 'Invalid QR'); assert.equal(f.calls.length, 1); assert.equal(f.store.read(), null);
  }
});

test('same QR frames stay suppressed until removed for a sustained interval', () => {
  const gate = new QrGate(); assert.equal(gate.observe('qr', 0), true);
  for (let i = 1; i < 100; i++) assert.equal(gate.observe('qr', i * 1000), false);
  gate.observe(null, 100000); gate.observe(null, 100500);
  assert.equal(gate.observe('qr', 100600), false, 'one missed frame does not retrigger');
  gate.observe(null, 101000); gate.observe(null, 102500); assert.equal(gate.observe('qr', 102600), true);
});

test('kiosk camera pauses decoding while retaining permission stream and resumes without getUserMedia', async () => {
  let starts = 0, stops = 0, decodes = 0, ended, error;
  const track = {stop: () => stops++, addEventListener: (name, fn) => {if (name === 'ended') ended = fn;}};
  const video = {videoWidth: 640, videoHeight: 480, play: async () => {}, srcObject: null};
  const scanner = new CameraScanner({video, pauseOnResult: true,
    mediaDevices: {getUserMedia: async () => {starts++; return {getTracks: () => [track]};}, enumerateDevices: async () => []},
    canvasFactory: () => ({getContext: () => ({drawImage() {}})}),
    decodeFrame: async () => {decodes++; return {status: 'qr', token: 'test'};},
    onResult() {}, onDevices() {}, onState() {}, onError: message => {error = message;}});
  await scanner.start(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(scanner.paused, true); assert.equal(starts, 1); assert.equal(stops, 0);
  await scanner.tick(scanner.generation); assert.equal(decodes, 1);
  scanner.resume(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(decodes, 2); assert.equal(starts, 1); assert.equal(stops, 0);
  ended(); assert.match(error, /disconnected.*Restart camera/); assert.equal(scanner.running, false);
});

test('default timers support browser receiver requirements during recovery and dismissal', async () => {
  const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  const timers = [], cancelled = [];
  globalThis.setTimeout = function (callback, delay) {
    if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation');
    const timer = {callback, delay}; timers.push(timer); return timer;
  };
  globalThis.clearTimeout = function (timer) {
    if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation');
    cancelled.push(timer);
  };
  let flow;
  try {
    const f = fixture();
    f.store.begin('p', 'qr', 'existing-request', true);
    const states = [];
    let online = false, ready = 0;
    const result = {request_id: 'existing-request', status: 'duplicate',
      participant: {participant_id: 'p', name: 'Jamie Reyes', checked_in_at: 123, checked_in_by: 'Demo opening shift'}};
    flow = new KioskFlow({store: f.store, show: state => states.push(state), ready: () => ready++,
      api: async () => { if (!online) throw new Error('Response lost'); return {resolved: true, result}; }});
    await flow.recover();
    assert.equal(states.at(-1).kind, 'pending');
    assert.equal(timers.at(-1).delay, 3000);
    assert.equal(f.store.read().request_id, 'existing-request');
    online = true;
    timers.at(-1).callback();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(states.at(-1).kind, 'duplicate');
    assert.equal(f.store.read(), null);
    assert.equal(timers.at(-1).delay, 5000);
    timers.at(-1).callback();
    assert.equal(ready, 1);
    assert.equal(flow.busy, false);
    flow.dispose();
    assert.ok(cancelled.length >= 3);
  } finally {
    globalThis.setTimeout = originalSet;
    globalThis.clearTimeout = originalClear;
  }
});
