import {CameraScanner, imageFrame} from './camera.js';
import {PendingAttempt} from './pending.js';
import {participantReview} from './review.js';
import {KioskFlow, QrGate} from './kiosk.js';

const $ = id => document.getElementById(id);
let identity = null, pendingStore = null, current = null, method = 'qr', view = 'scan';
let pending = null, submissionBusy = false, modalBusy = false, lastFocus = null;
let dashboard = null, offset = 0, searchGeneration = 0, busyLookup = false, resumeCamera = false;
let dashboardBusy = false, lastConnected = null, timeZone = 'Asia/Manila';
let kioskActive = false, kioskFlow = null, kioskGate = new QrGate(), kioskAudio = null, kioskDevice = '';
const readOnly = () => identity?.read_only_preview === true;

function showDataMode() {
  const preview = readOnly();
  $('kiosk-setup-open').hidden = preview;
  $('preview-banner').hidden = !preview;
  $('demo-label').hidden = identity?.mode === 'live';
  text('demo-label', preview ? 'Real registrations · Read-only preview' : 'Synthetic demo');
  text('scan-description', preview ? 'Scan an issued ticket to verify the registration. Attendance changes are disabled.' : 'Review the participant, then confirm their arrival.');
  text('review-instruction', preview ? 'Review the matched registration. Check-in is disabled in preview.' : 'Select Check in to record arrival.');
  text('sync-footnote', preview ? 'Reads real registrations only. This preview never exports attendance or sends email.' : 'Registration changes take effect after a successful sync. Confirmed arrivals are saved immediately.');
}

function text(id, value) { $(id).textContent = value; }
function node(tag, value, className = '') { const n = document.createElement(tag); n.textContent = value; n.className = className; return n; }
function stamp(value) {
  if (value == null) return 'Not yet';
  return new Intl.DateTimeFormat(undefined, {timeZone, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'short'}).format(new Date(value * 1000));
}
function status(p) { return p.checked_in_at != null ? 'Checked in' : p.eligible ? 'Not yet arrived' : 'Ineligible'; }
function connection(ok, message = '') {
  if (ok) lastConnected = Date.now();
  $('connection').classList.toggle('error', !ok);
  const source = readOnly() ? 'Real registration preview'
    : identity?.mode === 'synthetic' || dashboard?.synthetic === true ? 'Demo data'
    : identity?.mode === 'live' || dashboard?.synthetic === false ? 'Live event' : 'Data source loading';
  text('connection', ok ? `Backend connected · ${source} · Updated ${new Date(lastConnected).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}` : `Backend unavailable · ${lastConnected ? 'Showing stale data. ' : ''}${message || 'Reconnect before confirming attendance.'}`);
}
async function api(path, body, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      headers: {'Content-Type': 'application/json', ...(identity?.csrf ? {'X-CSRF-Token': identity.csrf} : {})},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}), ...options});
    let result;
    try { result = await response.json(); } catch { throw new Error('Unexpected backend response. Retry to reconcile.'); }
    if (!response.ok) {
      if (response.status === 401 && path !== 'login' && !kioskActive) showLogin();
      throw new Error(typeof result.detail === 'string' ? result.detail : 'Request could not be processed. Review the input and retry.');
    }
    return result;
  } catch (error) {
    if (error.name === 'AbortError' || error instanceof TypeError) { connection(false); throw new Error('Unable to confirm — retry. Check the backend connection.'); }
    throw error;
  } finally { clearTimeout(timeout); }
}

async function decodeCanvas(canvas) {
  if (window.jsQR) {
    const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height);
    const code = window.jsQR(data.data, data.width, data.height, {inversionAttempts: 'attemptBoth'});
    return code ? api('lookup', {token: code.data}) : {status: 'no_qr'};
  }
  return api('decode', {image: canvas.toDataURL('image/jpeg', 0.82)});
}
const scanner = new CameraScanner({video: $('video'), mediaDevices: navigator.mediaDevices,
  decodeFrame: decodeCanvas, onResult: result => { resumeCamera = true; showLookupResult(result); },
  onError: message => { text('camera-message', message); },
  onDevices: devices => {
    const selected = $('camera-select').value;
    $('camera-select').replaceChildren(new Option('Rear camera (preferred)', ''));
    devices.forEach((d, i) => $('camera-select').add(new Option(d.label || `Camera ${i + 1}`, d.deviceId)));
    $('camera-select').value = selected;
  },
  onState: state => {
    text('camera-state', state.startsWith('Camera ready') ? 'Camera on' : state);
    $('camera-placeholder').hidden = state.startsWith('Camera ready');
    $('camera-start').hidden = state.startsWith('Camera ready');
    $('camera-stop').hidden = !state.startsWith('Camera ready');
    text('camera-message', state === 'Camera off' ? 'Camera stopped. Start it when you’re ready.' : state);
  }
});
function stopCamera() { scanner.stop(); }
function ensureNoPending() {
  if (readOnly()) return true;
  if (!pendingStore) {
    const message = 'Enable site storage and refresh before checking in. This browser must retain a pending request safely.';
    text('camera-message', message); text('participants-message', message); return false;
  }
  try {
    pending = pendingStore?.read();
    if (pending) { openPending(); return false; }
    return true;
  } catch (error) { text('camera-message', error.message); $('pending-banner').hidden = false; return false; }
}
function startCamera() {
  if (kioskActive || !identity?.nickname || busyLookup || view !== 'scan' || document.hidden || $('review').open || !ensureNoPending()) return;
  resumeCamera = true;
  scanner.start($('camera-select').value);
}
function showLogin() {
  stopCamera();
  if ($('review').open) $('review').close();
  identity = null;
  $('workspace').hidden = true;
  $('login').hidden = false;
  $('password-form').hidden = false;
  $('nickname-form').hidden = true;
  $('password').value = '';
  $('password').focus();
}
async function enter() {
  timeZone = identity.timezone || timeZone;
  $('password').value = '';
  $('password-form').hidden = true;
  $('nickname-form').hidden = Boolean(identity.nickname);
  if (!identity.nickname) {
    $('login').hidden = false;
    $('workspace').hidden = true;
    text('login-description', 'One more step: tell your teammates who’s at this station.');
    $('nickname').focus();
    return;
  }
  $('login').hidden = true;
  $('workspace').hidden = false;
  text('staff-name', `Signed in as ${identity.nickname}`);
  showDataMode(); pending = null;
  try { pendingStore = readOnly() ? null : new PendingAttempt(localStorage, identity.event_id, location.origin); }
  catch { pendingStore = null; }
  // A recovered kiosk request must retain server-controlled kiosk attribution,
  // including after an expired session requires a fresh staff sign-in.
  try {
    if (!readOnly() && !identity.kiosk && pendingStore?.read()?.kiosk) Object.assign(identity, await api('kiosk/start', {result_seconds: 5, muted: true}));
  } catch (error) { text('camera-message', error.message); return; }
  if (identity.kiosk) { enterKiosk(); return; }
  navigate('scan');
  await refreshDashboard();
  try { pending = pendingStore?.read(); if (pending) await openPending(); } catch (error) {
    text('camera-message', error.message); $('pending-banner').hidden = false;
  }
}
async function logout() {
  stopCamera();
  try { await api('logout', {}); showLogin(); text('login-error', ''); } catch (error) { text('connection', error.message); }
}
$('password-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; text('login-error', '');
  try { identity = await api('login', {password: $('password').value}); await enter(); }
  catch (error) { text('login-error', error.message); }
  finally { button.disabled = false; }
});
$('nickname-form').addEventListener('submit', async event => {
  event.preventDefault(); event.submitter.disabled = true; text('login-error', '');
  try { const result = await api('nickname', {nickname: $('nickname').value}); identity.nickname = result.nickname; await enter(); }
  catch (error) { text('login-error', error.message); }
  finally { event.submitter.disabled = false; }
});
$('logout').onclick = logout; $('nickname-logout').onclick = logout;
function navigate(next) {
  if (!identity?.nickname || kioskActive) return;
  if (next !== view || next !== 'scan') stopCamera();
  view = next;
  for (const name of ['scan', 'participants', 'overview', 'activity']) $(name + '-view').hidden = name !== next;
  document.querySelectorAll('[data-view]').forEach(b => { if (b.dataset.view === next) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
  if (next === 'participants') loadParticipants();
  if (next === 'overview' || next === 'activity') refreshDashboard();
}
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => navigate(b.dataset.view));
$('manual-search').onclick = () => { navigate('participants'); $('search').focus(); };
$('camera-start').onclick = startCamera; $('camera-stop').onclick = stopCamera;
$('camera-select').onchange = () => { if (scanner.running) startCamera(); };
$('qr-file').onchange = async event => {
  if (!event.target.files[0] || !ensureNoPending() || busyLookup) return;
  busyLookup = true; stopCamera(); resumeCamera = false; $('qr-file').disabled = true;
  text('camera-message', 'Reading QR image…');
  try {
    const frame = await imageFrame(event.target.files[0]);
    const result = await decodeCanvas(frame);
    if (identity?.nickname && view === 'scan') {
      if (result.status === 'no_qr') text('camera-message', 'No readable QR found. Try a clearer image or use participant search.');
      else showLookupResult(result);
    }
  } catch (error) { text('camera-message', error.message); }
  finally { busyLookup = false; $('qr-file').disabled = false; event.target.value = ''; }
};
function openDialog() {
  stopCamera();
  text('review-error', '');
  if (!$('review').open) { lastFocus = document.activeElement; $('review').showModal(); }
  $('review-title').focus();
}
function details(p) {
  const dl = $('review-details'); dl.replaceChildren();
  if (!p) return;
  for (const [key, value] of [['Ticket tier', p.tier], ['Source', p.source_id], ['Category', p.price_category], ['Registration', p.record_id], ['Validity', p.eligible ? 'Eligible ticket' : 'Ineligible ticket']]) {
    dl.append(node('dt', key), node('dd', value));
  }
}
function showLookupResult(result) {
  if (!identity?.nickname || kioskActive) return;
  if (result.status === 'found') return showParticipant(result.participant, 'qr');
  current = null; openDialog();
  text('review-title', result.status === 'unknown' ? 'Unknown ticket' : 'Invalid ticket');
  text('review-email', ''); details(null); text('review-status', result.message || 'Ticket could not be read. Use manual search.');
  $('review-status').className = 'review-status invalid';
  $('confirm').hidden = false; $('confirm').disabled = true; text('confirm', 'Check in');
  $('cancel').disabled = false; text('cancel', 'Cancel / Scan another');
}
function showParticipant(p, sourceMethod) {
  if (kioskActive) return;
  current = p; method = sourceMethod;
  openDialog(); text('review-eyebrow', sourceMethod === 'qr' ? 'QR · PARTICIPANT REVIEW' : 'MANUAL · PARTICIPANT REVIEW');
  text('review-title', p.name); text('review-email', p.email); details(p);
  const review = participantReview(p, {readOnly: readOnly(), pending: Boolean(pending), stamp});
  text('review-status', review.message); $('review-status').className = 'review-status' + (!p.eligible ? ' invalid' : '');
  $('confirm').hidden = false; $('confirm').disabled = review.disabled;
  text('confirm', review.label); $('cancel').disabled = false; text('cancel', readOnly() ? 'Close / Scan another' : 'Cancel / Scan another');
}
function restoreFocus() {
  if (!identity?.nickname) { $('password').focus(); return; }
  if (lastFocus?.isConnected && !lastFocus.closest('[hidden]')) lastFocus.focus();
  else $('main').focus();
}
function closeReview() {
  if (submissionBusy || modalBusy || pending) return;
  $('review').close(); current = null;
  restoreFocus();
  if (resumeCamera && view === 'scan') startCamera();
}
$('cancel').onclick = closeReview;
$('review').addEventListener('cancel', event => { event.preventDefault(); closeReview(); });
$('review').addEventListener('close', restoreFocus);
function pendingMessage(message) {
  text('review-status', message); $('review-status').className = 'review-status invalid';
  text('confirm', 'Retry same check-in'); $('confirm').disabled = false; $('confirm').hidden = false;
  $('cancel').disabled = true; text('cancel', 'Resolve before continuing');
  $('pending-banner').hidden = false;
}
function finish(result) {
  if (!pending || result.request_id !== pending.request_id || !['accepted', 'duplicate', 'invalid'].includes(result.status)) throw new Error('Unrecognized result. Retry the same check-in.');
  if (['accepted', 'duplicate'].includes(result.status) && result.participant?.checked_in_at == null) throw new Error('Attendance was not confirmed. Retry.');
  pendingStore.clear(pending.request_id); pending = null; $('pending-banner').hidden = true;
  const p = result.participant;
  if (p) { current = p; text('review-title', p.name); text('review-email', p.email); details(p); }
  text('review-status', result.status === 'accepted' ? `Check-in confirmed · ${stamp(p.checked_in_at)} by ${p.checked_in_by}` : result.status === 'duplicate' ? `Already checked in at ${stamp(p.checked_in_at)} by ${p.checked_in_by}` : `Cannot check in · ${p?.reason || 'Ticket is no longer eligible.'}`);
  $('review-status').className = 'review-status ' + (result.status === 'accepted' ? 'success' : result.status === 'invalid' ? 'invalid' : '');
  $('confirm').hidden = true; $('cancel').disabled = false; text('cancel', view === 'scan' ? 'Scan next' : 'Done');
  text('review-error', ''); refreshDashboard(); if (view === 'participants') loadParticipants();
}
async function reconcileAndSubmit() {
  if (readOnly()) return;
  if (!pending || submissionBusy) return;
  submissionBusy = true; $('confirm').disabled = true; $('cancel').disabled = true; text('confirm', 'Checking in…');
  try {
    const recovered = await api(`requests/${pending.request_id}`);
    if (recovered.resolved) finish(recovered.result);
    else {
      const {participant_id, method: pendingMethod, request_id} = pending;
      const result = await api('checkins', {participant_id, method: pendingMethod, request_id});
      finish(result);
    }
    connection(true);
  } catch (error) {
    // A timeout cannot establish whether the commit happened. Keep the durable ID.
    if (identity?.nickname && $('review').open) { pendingMessage('Unable to confirm — retry. Keep this check-in open until its result is confirmed.'); text('review-error', error.message); }
    connection(false);
  } finally { submissionBusy = false; }
}
$('confirm').onclick = async () => {
  if (readOnly()) return;
  if (submissionBusy || modalBusy) return;
  if (!pending) {
    if (!current?.eligible || current.checked_in_at != null) return;
    try { pending = pendingStore.begin(current.participant_id, method, crypto.randomUUID()); }
    catch (error) { text('review-error', error.message); return; }
  }
  await reconcileAndSubmit();
};
async function openPending() {
  if (readOnly()) return;
  if (modalBusy || submissionBusy) return;
  modalBusy = true; stopCamera();
  try {
    pending = pendingStore.read();
    if (!pending) return;
    current = null; openDialog(); text('review-title', 'Resolve pending check-in'); text('review-email', ''); details(null);
    pendingMessage('A check-in is awaiting confirmation. Retry to retrieve its saved result or complete the same attempt.');
    $('confirm').disabled = true;
    const recovered = await api(`requests/${pending.request_id}`);
    if (recovered.resolved) finish(recovered.result);
    else {
      const p = await api(`participants/${encodeURIComponent(pending.participant_id)}`);
      current = p; text('review-title', p.name); text('review-email', p.email); details(p);
      $('confirm').disabled = false;
    }
  } catch (error) { pendingMessage('Unable to confirm — retry.'); text('review-error', error.message); }
  finally { modalBusy = false; }
}
$('pending-open').onclick = openPending;
async function loadParticipants() {
  if (!identity?.nickname || kioskActive) return;
  const generation = ++searchGeneration; text('participants-message', 'Loading participants…');
  const params = new URLSearchParams({q: $('search').value, tier: $('tier').value, attendance: $('attendance').value, offset, limit: 25});
  try {
    const result = await api('participants?' + params);
    if (generation !== searchGeneration || !identity?.nickname || kioskActive) return;
    connection(true); text('participants-message', result.total ? `${result.total} matching participants` : 'No participants match. Try another name, email or filter.');
    const focusedParticipant = document.activeElement?.dataset.participant;
    const list = $('participant-list'); list.replaceChildren();
    for (const p of result.items) {
      const row = node('article', '', 'participant-row'); const person = node('div', '');
      person.append(node('strong', p.name), node('small', p.email));
      const ticket = node('div', p.tier); ticket.append(node('small', `${p.source_id} · ${p.price_category}`));
      const state = node('div', ''); state.append(node('span', status(p), 'status-label' + (!p.eligible ? ' invalid' : p.checked_in_at != null ? ' good' : '')));
      if (p.checked_in_at != null && !p.eligible) state.append(node('small', 'Ticket currently ineligible'));
      const button = node('button', 'Review'); button.dataset.participant = p.participant_id; button.setAttribute('aria-label', `Review ${p.name}`);
      button.onclick = async () => {
        if (!ensureNoPending() || busyLookup) return;
        busyLookup = true; button.disabled = true;
        try { const fresh = await api(`participants/${encodeURIComponent(p.participant_id)}`); if (identity?.nickname) { resumeCamera = false; showParticipant(fresh, 'manual'); } }
        catch (error) { text('participants-message', error.message); }
        finally { button.disabled = false; busyLookup = false; }
      };
      row.append(person, ticket, state, button); list.append(row);
    }
    if (focusedParticipant) [...list.querySelectorAll('button')].find(b => b.dataset.participant === focusedParticipant)?.focus();
    $('previous').disabled = offset === 0; $('next').disabled = offset + result.items.length >= result.total;
    text('page-info', result.total ? `${offset + 1}–${offset + result.items.length} of ${result.total}` : '0 participants');
  } catch (error) { if (generation === searchGeneration) { text('participants-message', 'Unable to refresh participants. Previous results may be stale. ' + error.message); connection(false); } }
}
$('search-form').onsubmit = event => { event.preventDefault(); offset = 0; loadParticipants(); };
$('previous').onclick = () => { offset = Math.max(0, offset - 25); loadParticipants(); };
$('next').onclick = () => { offset += 25; loadParticipants(); };
async function refreshDashboard() {
  if (!identity?.nickname || dashboardBusy || kioskActive) return;
  dashboardBusy = true;
  try {
    const data = await api('dashboard');
    if (!identity?.nickname) return;
    dashboard = data; timeZone = data.timezone; connection(true);
    showDataMode();
    for (const key of ['eligible', 'arrived', 'waiting']) text(`${key}-count`, data.counts[key]);
    text('sync-status', data.sync.last_success ? `Last successful registration sync: ${stamp(data.sync.last_success)}` : readOnly() ? 'No real registrations imported yet. Check source configuration and sync status.' : 'No successful registration sync yet. Admissions require initial reconciliation.');
    const age = data.sync.last_success ? Math.max(0, Math.round(data.server_time - data.sync.last_success)) : null;
    text('sync-details', `${data.admissions_open ? 'Admissions open' : 'Admissions closed'} · ${data.sync.pending_exports} attendance updates pending${age != null ? ` · Registration snapshot ${age}s old` : ''}${data.sync.omitted ? ` · ${data.sync.omitted} rows without this event/stable ID omitted` : ''}`);
    text('sync-error', [data.sync.error, data.sync.export_error, age > 120 ? 'Registration data is stale. Check synchronization before relying on recent approvals or cancellations.' : ''].filter(Boolean).join(' '));
    if ($('tier').options.length === 1) data.tiers.forEach(tier => $('tier').add(new Option(tier, tier)));
    const activity = $('activity-list'); activity.replaceChildren();
    if (!data.activity.length) activity.append(node('p', 'No arrivals yet. Confirmed check-ins will appear here.', 'panel muted'));
    for (const p of data.activity) {
      const row = node('article', '', 'activity-row'); row.setAttribute('role', 'listitem');
      const person = node('div', ''); person.append(node('strong', p.name), node('small', p.email), node('small', `${p.tier} · ${p.source_id}`));
      const detail = node('div', stamp(p.checked_in_at), 'activity-time'); detail.append(node('small', `By ${p.checked_in_by} · ${p.method === 'qr' ? 'QR' : p.method === 'manual' ? 'Manual' : 'Legacy import'}`));
      row.append(person, detail); activity.append(row);
    }
  } catch (error) {
    connection(false, error.message);
    if (!dashboard) text('sync-error', 'Dashboard unavailable. Reconnect and refresh.');
  } finally { dashboardBusy = false; }
}
$('dashboard-refresh').onclick = refreshDashboard; $('activity-refresh').onclick = refreshDashboard;
$('sync-now').onclick = async () => {
  $('sync-now').disabled = true;
  try { await api('sync', {}); text('sync-status', 'Sync requested. Status will update automatically.'); }
  catch (error) { text('sync-error', error.message); }
  finally { $('sync-now').disabled = false; }
};
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { stopCamera(); kioskScanner.stop(); }
  else if (kioskActive) startKioskCamera();
  else if (identity?.nickname) refreshDashboard();
});
window.addEventListener('pagehide', () => { stopCamera(); kioskScanner.stop(); kioskFlow?.dispose(); });
window.addEventListener('storage', event => {
  if (pendingStore && event.key === pendingStore.key && event.newValue) {
    stopCamera();
    if (kioskActive) { kioskScanner.pause(); kioskFlow?.recover(); } else openPending();
  }
});
setInterval(() => { if (!document.hidden && identity?.nickname && !kioskActive) { refreshDashboard(); if (view === 'participants' && !$('review').open) loadParticipants(); } }, 15000);
function showKiosk(state) {
  if (!kioskActive) return;
  $('kiosk-result').className = `panel kiosk-result ${state.kind}`;
  text('kiosk-icon', {success: '✓', duplicate: '↺', invalid: '✕', pending: '⌛', processing: '…', error: '!'}[state.kind] || '▣');
  text('kiosk-title', state.title);
  const p = state.participant;
  text('kiosk-detail', p ? `${p.tier} · ${p.price_category}${state.kind === 'duplicate' ? ` · Original arrival: ${stamp(p.checked_in_at)} by ${p.checked_in_by}` : ' · Arrival confirmed'}` : state.message || '');
  text('kiosk-registration', p ? `Registration ${p.record_id}` : '');
  if (state.sound) kioskSound(state.sound);
  else if (['success', 'duplicate', 'invalid', 'error'].includes(state.kind)) kioskSound(state.kind);
}
function kioskSound(kind) {
  if (identity?.muted || !kioskAudio || kioskAudio.state !== 'running') return;
  const tones = {success: [660, 880], duplicate: [440, 440], invalid: [220, 165], error: [165, 165, 165]}[kind];
  try {
    tones.forEach((frequency, i) => {
      const oscillator = kioskAudio.createOscillator(), gain = kioskAudio.createGain(), at = kioskAudio.currentTime + i * 0.18;
      oscillator.frequency.value = frequency; gain.gain.setValueAtTime(0.08, at); gain.gain.exponentialRampToValueAtTime(0.001, at + 0.14);
      oscillator.connect(gain); gain.connect(kioskAudio.destination); oscillator.start(at); oscillator.stop(at + 0.15);
    });
  } catch { /* Visible results remain available when audio is unavailable. */ }
}
const kioskScanner = new CameraScanner({video: $('kiosk-video'), mediaDevices: navigator.mediaDevices, pauseOnResult: true,
  decodeFrame: async canvas => {
    if (!window.jsQR) throw new Error('QR decoder unavailable. Reload this station or ask staff for help.');
    const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height);
    const code = window.jsQR(pixels.data, pixels.width, pixels.height, {inversionAttempts: 'attemptBoth'});
    return kioskGate.observe(code?.data || null) ? {status: 'qr', token: code.data} : {status: 'no_qr'};
  },
  onResult: result => kioskFlow?.scan(result.token),
  onError: message => {
    text('kiosk-camera-message', `Camera needs attention. ${message} Ask staff to check browser permissions and select Restart camera.`);
    $('kiosk-camera-restart').hidden = false;
    if (!kioskFlow?.busy) showKiosk({kind: 'error', title: 'Camera unavailable', message: 'Please ask staff for help.'});
  },
  onDevices: devices => {
    $('kiosk-camera').replaceChildren(new Option('Rear camera (preferred)', ''));
    devices.forEach((d, i) => $('kiosk-camera').add(new Option(d.label || `Camera ${i + 1}`, d.deviceId)));
  },
  onState: state => {
    if (state.startsWith('Camera ready')) {
      text('kiosk-camera-message', 'Camera ready · Remove your ticket after the result.'); $('kiosk-camera-restart').hidden = true;
      if (!kioskFlow?.busy) showKiosk({kind: 'ready', title: 'Ready to welcome you', message: 'Place one ticket QR in the camera view.'});
    }
    else if (state === 'Camera off') { text('kiosk-camera-message', 'Camera paused. Select Restart camera if it does not resume.'); $('kiosk-camera-restart').hidden = false; }
  }
});
function kioskReady() {
  showKiosk({kind: 'ready', title: 'Ready to welcome you', message: 'Place one QR in view. Remove the previous ticket before scanning it again.'});
  startKioskCamera();
}
function startKioskCamera() {
  if (!kioskActive || kioskFlow?.busy || document.hidden || $('kiosk-exit').open) return;
  if (kioskScanner.running) kioskScanner.resume();
  else kioskScanner.start(kioskDevice);
}
function enterKiosk() {
  stopCamera(); kioskActive = true;
  if ($('review').open) $('review').close();
  $('workspace').hidden = true; $('login').hidden = true; $('kiosk').hidden = false;
  text('kiosk-sound-state', identity.muted ? 'Sound muted by staff.' : 'Sound enabled when allowed by this browser.');
  try { kioskDevice = sessionStorage.getItem('pacheckq.kiosk.camera') || kioskDevice; } catch { /* Rear camera remains the default. */ }
  kioskFlow?.dispose();
  kioskFlow = new KioskFlow({api, store: pendingStore, show: showKiosk, ready: kioskReady, seconds: identity.result_seconds || 5});
  try {
    if (!pendingStore) throw new Error('Enable site storage and reload before starting this station.');
    if (pendingStore.read()) kioskFlow.recover(); else kioskReady();
  } catch (error) { showKiosk({kind: 'error', title: 'Staff assistance needed', message: error.message}); }
}
$('kiosk-setup-open').onclick = () => {
  if (readOnly() || !ensureNoPending()) return;
  stopCamera(); text('kiosk-setup-error', ''); $('kiosk-setup').showModal();
};
$('kiosk-setup-cancel').onclick = () => $('kiosk-setup').close();
$('kiosk-setup-form').onsubmit = async event => {
  event.preventDefault(); event.submitter.disabled = true;
  // Audio must be unlocked by the staff setup gesture.
  try {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!$('kiosk-muted').checked && Audio) { kioskAudio ??= new Audio(); await kioskAudio.resume(); }
  } catch { /* Browser may still block audio; text feedback is always shown. */ }
  try {
    if (!ensureNoPending()) return;
    kioskDevice = $('kiosk-camera').value;
    try { sessionStorage.setItem('pacheckq.kiosk.camera', kioskDevice); } catch { /* Camera selection still works for this page. */ }
    Object.assign(identity, await api('kiosk/start', {result_seconds: Number($('kiosk-seconds').value), muted: $('kiosk-muted').checked}));
    $('kiosk-setup').close(); enterKiosk();
  } catch (error) { text('kiosk-setup-error', error.message); }
  finally { event.submitter.disabled = false; }
};
$('kiosk-camera-restart').onclick = startKioskCamera;
$('kiosk-fullscreen').onclick = () => document.documentElement.requestFullscreen?.().catch(() => { text('kiosk-camera-message', 'Fullscreen unavailable. You can use this station in the current window.'); });
$('kiosk-unlock').onclick = () => { kioskScanner.pause(); $('kiosk-password').value = ''; text('kiosk-exit-error', ''); $('kiosk-exit').showModal(); };
$('kiosk-exit-cancel').onclick = () => $('kiosk-exit').close();
$('kiosk-exit').addEventListener('close', startKioskCamera);
$('kiosk-exit-form').onsubmit = async event => {
  event.preventDefault(); event.submitter.disabled = true;
  try {
    // Authenticate first, but never change attribution while an attempt is unresolved.
    const password = $('kiosk-password').value;
    if (pendingStore?.read() || kioskFlow?.busy) {
      const session = await api('session');
      if (session.authenticated) {
        await api('kiosk/exit', {password, recover_only: true});
        $('kiosk-password').value = '';
        text('kiosk-exit-error', 'Staff password verified. Wait for the pending check-in to resolve, then unlock again. Automatic recovery is continuing.');
        return;
      }
      // Restore an expired session without changing a pending arrival's attribution.
      const nickname = identity.nickname;
      const config = {result_seconds: identity.result_seconds || 5, muted: Boolean(identity.muted)};
      const restored = await api('login', {password});
      kioskFlow?.dispose();
      identity = restored;
      await api('nickname', {nickname}); identity.nickname = nickname;
      Object.assign(identity, await api('kiosk/start', config));
      $('kiosk-password').value = ''; $('kiosk-exit').close(); enterKiosk();
      return;
    }
    try { await api('kiosk/exit', {password}); }
    catch (error) {
      const session = await api('session');
      if (session.authenticated) throw error;
      identity = await api('login', {password});
    }
    $('kiosk-password').value = ''; $('kiosk-exit').close();
    kioskFlow?.dispose(); kioskScanner.stop(); kioskActive = false; identity.kiosk = false; $('kiosk').hidden = true;
    await enter();
  } catch (error) { $('kiosk-password').value = ''; text('kiosk-exit-error', error.message); }
  finally { event.submitter.disabled = false; }
};
try { identity = await api('session'); if (identity.authenticated) await enter(); else showLogin(); }
catch (error) { showLogin(); text('login-error', error.message); }
