import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {CameraScanner, cameraError} from '../camera.js';
import {PendingAttempt} from '../pending.js';

const storage = () => {
  const data = new Map();
  return {getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key)};
};
const deferred = () => { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve}; };
const flush = () => new Promise(resolve => setImmediate(resolve));

test('pending attempt survives refresh and retries with the same ID; another attendee is blocked', () => {
  const s = storage();
  const first = new PendingAttempt(s, 'event', 'http://localhost:8080');
  first.begin('participant-one', 'manual', 'request-one');
  assert.throws(() => first.begin('participant-two', 'qr', 'request-two'), /pending/);
  const refreshed = new PendingAttempt(s, 'event', 'http://localhost:8080');
  assert.equal(refreshed.read().request_id, 'request-one');
  refreshed.clear('wrong-id'); assert.ok(refreshed.read());
  refreshed.clear('request-one'); assert.equal(first.read(), null);
});

test('event mismatch and storage failure fail closed', () => {
  const s = storage(); new PendingAttempt(s, 'event-a', 'origin').begin('p', 'qr', 'r');
  assert.throws(() => new PendingAttempt(s, 'event-b', 'origin').read(), /another event/);
  const broken = {getItem: () => null, setItem: () => {throw new Error('Storage disabled');}};
  assert.throws(() => new PendingAttempt(broken,'event','origin').begin('p','qr','r'), /Storage disabled/);
});

function camera(overrides = {}) {
  let stopped = 0, decoded = 0, results = [], errors = [];
  const stream = {getTracks: () => [{stop: () => stopped++}]};
  const video = {videoWidth: 640, videoHeight: 480, play: async () => {}, srcObject: null};
  const scanner = new CameraScanner({video,
    mediaDevices: {getUserMedia: async () => stream, enumerateDevices: async () => []},
    decodeFrame: async () => {decoded++; return {status:'found',participant:{participant_id:'p'}};},
    onResult: result => results.push(result), onError: error => errors.push(error), onDevices: () => {}, onState: () => {},
    canvasFactory: () => ({getContext: () => ({drawImage: () => {}}),toDataURL: () => 'image'}), ...overrides});
  return {scanner, video, results, errors, stopped: () => stopped, decoded: () => decoded};
}
test('captured QR pauses decoding and releases capture throughout review', async () => {
  const c = camera(); await c.scanner.start(); await flush();
  assert.equal(c.results.length,1); assert.equal(c.decoded(),1); assert.equal(c.scanner.running,false); assert.equal(c.stopped(),1);
  await c.scanner.tick(c.scanner.generation); assert.equal(c.results.length,1);
  // Explicit Scan next is required; a fresh start yields exactly one next result.
  await c.scanner.start(); await flush(); assert.equal(c.results.length,2); assert.equal(c.stopped(),2);
});
test('camera ignores late lookup results when leaving scanner', async () => {
  const request = deferred(); const c=camera({decodeFrame: () => request.promise});
  await c.scanner.start(); c.scanner.stop(); request.resolve({status:'found'}); await flush();
  assert.equal(c.results.length,0); assert.equal(c.stopped(),1);
});
test('late permission result is released after navigation/logout', async () => {
  const request=deferred(); let stopped=0;
  const c=camera({mediaDevices:{getUserMedia:() => request.promise}});
  const opening=c.scanner.start(); c.scanner.stop();
  request.resolve({getTracks:()=>[{stop:()=>stopped++}]}); await opening;
  assert.equal(stopped,1); assert.equal(c.scanner.running,false); assert.equal(c.video.srcObject,null);
});
test('permission and unsupported-context errors offer manual recovery', async () => {
  const c=camera({mediaDevices:null});await c.scanner.start();assert.match(c.errors[0],/HTTPS|localhost/);
  assert.match(cameraError({name:'NotAllowedError'}),/permission.*search/i);
  assert.match(cameraError({name:'NotReadableError'}),/in use.*search/i);
  assert.match(cameraError({name:'NotFoundError'}),/No camera.*search/i);
});
test('actual vendored jsQR decodes the synthetic ticket bitmap without native BarcodeDetector', async () => {
  const context={Uint8ClampedArray, Uint8Array, Int32Array, Uint32Array, Float64Array, Math};
  vm.createContext(context);
  vm.runInContext(await readFile(new URL('../vendor/jsQR.js',import.meta.url),'utf8'),context);
  const fixture=JSON.parse(await readFile(new URL('./qr-matrix.json',import.meta.url),'utf8'));
  const scale=6,width=fixture.rows.length*scale,data=new Uint8ClampedArray(width*width*4);
  for(let y=0;y<width;y++)for(let x=0;x<width;x++){
    const i=(y*width+x)*4, v=fixture.rows[Math.floor(y/scale)][Math.floor(x/scale)]==='1'?0:255;
    data[i]=data[i+1]=data[i+2]=v;data[i+3]=255;
  }
  const code=context.jsQR(data,width,width);
  assert.equal(code.data,fixture.token);
  assert.equal(context.jsQR(new Uint8ClampedArray(100*100*4).fill(255),100,100),null);
});
