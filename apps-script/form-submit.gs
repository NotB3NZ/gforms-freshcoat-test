/** Bind this to the RESPONSE SPREADSHEET, not to the Form.
 * Set CONFIG and header overrides to match .env/config/headers.json.
 * Run setupTrigger manually once and authorize it. Do not run onRegistrationSubmit.
 */
const CONFIG = {
  eventId: 'REPLACE_WITH_EVENT_ID',
  sheetTab: 'REPLACE_WITH_EXACT_RESPONSE_TAB',
  // Only override metadata header labels here when HEADERS_FILE overrides them in Python.
  headerOverrides: {},
};

const META_KEYS = [
  'event_id', 'participant_id', 'ticket_token', 'normalized_email', 'ticket_name',
  'state', 'render_status', 'email_status', 'duplicate_of', 'card_path',
  'render_attempts', 'email_attempts', 'message_id', 'last_error', 'updated_at',
  'email_sent_at', 'checked_in_at', 'checkin_request_id', 'ticket_status', 'checked_in_by',
];

function label_(key) { return CONFIG.headerOverrides[key] || key; }

function validateConfig_() {
  if (!CONFIG.eventId || CONFIG.eventId.startsWith('REPLACE') || CONFIG.sheetTab.startsWith('REPLACE')) {
    throw new Error('Set eventId and sheetTab before installing the trigger.');
  }
}

function headers_(sheet) {
  const values = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getDisplayValues()[0];
  const nonempty = values.filter(Boolean);
  if (new Set(nonempty).size !== nonempty.length) throw new Error('Duplicate headers; fix them first.');
  META_KEYS.forEach(key => {
    if (!values.includes(label_(key))) throw new Error('Run Python init-sheet first; missing metadata header.');
  });
  return values;
}

function setupTrigger() {
  validateConfig_();
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(CONFIG.sheetTab);
  if (!sheet) throw new Error('Configured response tab not found.');
  headers_(sheet);
  // Idempotent installation: retain at most one trigger owned by this installer.
  ScriptApp.getProjectTriggers().filter(t => t.getHandlerFunction() === 'onRegistrationSubmit')
    .forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('onRegistrationSubmit').forSpreadsheet(ss).onFormSubmit().create();
}

function onRegistrationSubmit(e) {
  validateConfig_();
  if (!e || !e.range) throw new Error('This function needs an installed spreadsheet form-submit trigger.');
  const sheet = e.range.getSheet();
  if (sheet.getName() !== CONFIG.sheetTab || e.range.getRow() <= 1) return;
  const lock = LockService.getDocumentLock();
  lock.waitLock(30000);
  try {
    const headers = headers_(sheet);
    const row = e.range.getRow(); // Submission locator only; Python assigns permanent identity.
    const cell = key => sheet.getRange(row, headers.indexOf(label_(key)) + 1);
    // Never reset identities, completed/failed jobs, or an already queued submission.
    if (cell('participant_id').getValue() || cell('state').getValue() || cell('ticket_token').getValue()) return;
    const existingEvent = cell('event_id').getDisplayValue();
    if (existingEvent && existingEvent !== CONFIG.eventId) throw new Error('Event mismatch; registration left untouched.');
    cell('event_id').setValue(CONFIG.eventId);
    cell('updated_at').setValue(new Date().toISOString());
    SpreadsheetApp.flush();
    cell('state').setValue('PENDING'); // Set last: the worker cannot see a half-queued job.
    SpreadsheetApp.flush();
  } finally {
    lock.releaseLock();
  }
}
