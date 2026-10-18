// Offline integration checks for the actual trigger source. No Google calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../apps-script/form-submit.gs', import.meta.url), 'utf8');
const context = vm.createContext({});
vm.runInContext(source, context);
vm.runInContext("CONFIG.eventId = 'test-event'; CONFIG.sheetTab = 'Responses';", context);
const keys = Array.from(vm.runInContext('META_KEYS', context));
const values = Object.fromEntries(keys.map(k => [k, '']));
const writes = [];
let locked = false;
const sheet = {
  getName: () => 'Responses',
  getLastColumn: () => keys.length,
  getRange(row, column) {
    if (row === 1) return { getDisplayValues: () => [keys] };
    const key = keys[column - 1];
    return {
      getValue: () => values[key],
      getDisplayValue: () => values[key],
      setValue(value) {
        assert.ok(locked);
        writes.push(key);
        values[key] = value;
      },
    };
  },
};
context.LockService = { getDocumentLock: () => ({
  waitLock: () => { assert.equal(locked, false); locked = true; },
  releaseLock: () => { locked = false; },
}) };
let triggers = [{ getHandlerFunction: () => 'unrelatedHandler' }];
context.SpreadsheetApp = {
  flush: () => {},
  getActiveSpreadsheet: () => ({ getSheetByName: () => sheet }),
};
context.ScriptApp = {
  getProjectTriggers: () => triggers.slice(),
  deleteTrigger: t => { triggers = triggers.filter(other => other !== t); },
  newTrigger: name => ({ forSpreadsheet: () => ({ onFormSubmit: () => ({ create: () => {
    triggers.push({ getHandlerFunction: () => name });
  } }) }) }),
};
const event = { range: { getSheet: () => sheet, getRow: () => 2 } };
context.onRegistrationSubmit(event);
assert.equal(values.event_id, 'test-event');
assert.equal(values.state, 'PENDING');
assert.equal(values.ticket_token, '');
assert.deepEqual(writes, ['event_id', 'updated_at', 'state']);
assert.equal(locked, false);
context.onRegistrationSubmit(event);
assert.equal(writes.length, 3); // Repeated trigger cannot reset queued work.
values.state = '';
values.ticket_token = 'existing-ticket';
context.onRegistrationSubmit(event);
assert.equal(writes.length, 3); // Issued identities remain untouched.
values.ticket_token = '';
values.event_id = 'another-event';
assert.throws(() => context.onRegistrationSubmit(event), /Event mismatch/);
assert.equal(writes.length, 3);
assert.equal(locked, false);
assert.throws(() => context.onRegistrationSubmit(), /installed spreadsheet/);
context.setupTrigger();
context.setupTrigger();
assert.equal(triggers.filter(t => t.getHandlerFunction() === 'onRegistrationSubmit').length, 1);
assert.equal(triggers.filter(t => t.getHandlerFunction() === 'unrelatedHandler').length, 1);
console.log(JSON.stringify(keys));
