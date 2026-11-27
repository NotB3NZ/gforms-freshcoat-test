// A code must leave the view for 1.5 seconds before it can trigger again.
export class QrGate {
  constructor() { this.last = null; this.absentAt = null; }
  observe(token, now = Date.now()) {
    if (!token) {
      this.absentAt ??= now;
      if (now - this.absentAt >= 1500) this.last = null;
      return false;
    }
    this.absentAt = null;
    if (token === this.last) return false;
    this.last = token;
    return true;
  }
}

export function kioskOutcome(result) {
  const p = result.participant;
  if (result.status === 'accepted') return {kind: 'success', title: `Welcome, ${p.name}!`, participant: p};
  if (result.status === 'duplicate') return {kind: 'duplicate', title: `${p.name} is already checked in`, participant: p};
  return {kind: 'invalid', title: p ? 'Ticket not eligible' : 'Invalid QR',
    message: p ? 'Please ask staff to review this registration.' : 'This QR is not a recognized admission ticket. Please ask staff for help.'};
}

// The camera stays paused until a durable request has a confirmed outcome.
// Timers and transport are injected so lost responses and refresh are testable.
export class KioskFlow {
  constructor({api, store, show, ready, seconds = 5, uuid = () => crypto.randomUUID(), schedule = (callback, delay) => setTimeout(callback, delay), cancel = timer => clearTimeout(timer)}) {
    Object.assign(this, {api, store, show, ready, seconds, uuid, schedule, cancel});
    this.busy = false; this.disposed = false; this.timer = null; this.connectionFailed = false;
  }
  later(callback, delay) { this.cancel(this.timer); this.timer = this.schedule(() => { if (!this.disposed) callback(); }, delay); }
  dispose() { this.disposed = true; this.cancel(this.timer); }
  async scan(token) {
    if (this.busy || this.disposed) return;
    this.busy = true;
    this.show({kind: 'processing', title: 'Checking ticket…'});
    try {
      if (this.store.read()) return await this.reconcile();
      const lookup = await this.api('lookup', {token});
      if (this.disposed) return;
      if (lookup.status !== 'found') return this.result({status: 'invalid'});
      // Even duplicates/ineligible tickets pass through the transactional decision.
      this.store.begin(lookup.participant.participant_id, 'qr', this.uuid(), true);
      await this.reconcile();
    } catch (error) {
      this.pending(error);
      this.later(() => { this.busy = false; this.scan(token); }, 3000);
    }
  }
  async recover() {
    if (this.busy || this.disposed) return;
    this.busy = true;
    await this.reconcile();
  }
  async reconcile() {
    try {
      const p = this.store.read();
      if (!p) throw new Error('The pending request could not be restored. Ask staff for help.');
      this.show({kind: 'pending', title: 'Confirmation pending', message: 'Please wait. Recovering the same check-in before accepting another ticket.'});
      const recovered = await this.api(`requests/${p.request_id}`);
      if (this.disposed) return;
      const result = recovered.resolved ? recovered.result : await this.api('checkins', {
        participant_id: p.participant_id, method: p.method, request_id: p.request_id
      });
      if (this.disposed) return;
      if (result.request_id !== p.request_id || !['accepted', 'duplicate', 'invalid'].includes(result.status) ||
          (result.status !== 'invalid' && result.participant?.checked_in_at == null)) {
        throw new Error('Attendance has not been confirmed by the backend.');
      }
      this.store.clear(p.request_id);
      if (this.store.read()) throw new Error('Pending request storage could not be cleared. Ask staff for help.');
      this.result(result);
    } catch (error) {
      this.pending(error);
      this.later(() => this.reconcile(), 3000);
    }
  }
  pending(error) {
    if (this.disposed) return;
    this.show({kind: 'pending', title: 'Confirmation pending', sound: this.connectionFailed ? undefined : 'error',
      message: `Keep this ticket here. Reconnecting automatically. ${error.message}`});
    this.connectionFailed = true;
  }
  result(result) {
    this.connectionFailed = false;
    this.show(kioskOutcome(result));
    this.later(() => { this.busy = false; this.ready(); }, this.seconds * 1000);
  }
}
