// Only opaque database identifiers are persisted; no ticket tokens or participant data.
export class PendingAttempt {
  constructor(storage, eventId, origin) {
    this.storage = storage;
    this.eventId = eventId;
    this.key = `freshcoat.pending:${origin}`;
  }
  read() {
    const raw = this.storage.getItem(this.key);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (!p.request_id || !p.participant_id || !['qr', 'manual'].includes(p.method) || p.event_id !== this.eventId) {
      throw new Error('A pending check-in belongs to another event or needs recovery. Restore the original event before continuing.');
    }
    return p;
  }
  begin(participantId, method, requestId) {
    if (this.read()) throw new Error('Resolve the pending check-in first.');
    const p = {participant_id: participantId, method, request_id: requestId, event_id: this.eventId};
    this.storage.setItem(this.key, JSON.stringify(p));
    if (this.read()?.request_id !== requestId) throw new Error('Unable to retain the check-in request. Do not continue.');
    return p;
  }
  clear(requestId) {
    if (this.read()?.request_id === requestId) this.storage.removeItem(this.key);
  }
}
