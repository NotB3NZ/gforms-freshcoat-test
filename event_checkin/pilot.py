"""One pinned three-entry pilot, layered on the established finite batch runner.

No general production unlock. Preparation/diagnosis read only. Live writes and
SMTP require separate explicit scope; receipts never authorize a second send.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from email.utils import parseaddr
from pathlib import Path

from .cards import CardRenderer, google_sans_files, inspect_template
from .config import ConfigurationError, load_settings
from .controlled_render import LiveTestSheet, check_normal_reservations, verify_render
from .diagnostics import safe_diagnostic
from .header_init import EVENT_ID
from .locking import file_lock
from .mailer import SMTPMailer
from .production import (BatchJournal, BatchProcessor, details, digest, fingerprint, issued_identity,
                         private_json, reasons, recipient)
from .single_send import WRITE_FIELDS, checksum, require, safe_blockers
from .google_reads import pace_http

PLAN_RELATIVE = 'runtime-production/batch-reviews/official-pilot-20261101-134414.json'
PLAN_SHA256 = '1a7f01a7e49542ff82a5c1d2f036b6b97d8f430ac912819826ca6fff757094a9'
ENTRY_DIGESTS = (
    'f70eb1e516ffe6185b6142a1ea09b49b0c381ffe4693e3ba6dcca77b328ed616',
    '049ae719b97ff31bfd3b600a3631eb49ae348a420f412f546b691f8616a90e5e',
    'a745f80dc96b48b1c7fa21c1a784f331cd6a4a39ab005188cce407c3b218c45c',
)
SENDER = 'awscc.upmindanao@gmail.com'
TEMPLATE_SHA256 = '7e3c7247d68dd490d2987ae616d40cf344805a38d6a36023a9176d97327830d4'


def policy_path(settings):
    return settings.root / 'config/pilot-batch.approved.json'


def load_bound(settings, path):
    require(settings.profile == 'production' and settings.event_id == EVENT_ID, 'Only the approved production event is authorized.')
    path = Path(path).resolve()
    require(path == (settings.root / PLAN_RELATIVE).resolve(), 'Only the official pilot path is authorized.')
    encoded = path.read_bytes()
    require(hashlib.sha256(encoded).hexdigest() == PLAN_SHA256,
            'Only the unchanged official pilot plan is authorized.')
    plan = json.loads(encoded)  # Validate and parse the very same immutable bytes.
    require(plan['version'] == 2 and plan['live'] is True and plan['profile'] == 'production'
            and plan['event_id'] == EVENT_ID and plan['action'] == 'send' and plan['regenerate'] is False
            and type(plan['limit']) is int and plan['limit'] == plan['requested_limit'] == 3
            and tuple(digest(e) for e in plan['entries']) == ENTRY_DIGESTS
            and plan['tracking_scope'] == {'policy': 'selected_entries_only', 'targets': [e['target'] for e in plan['entries']]},
            'Pilot selection or tracking scope changed; new review is required.')
    policy = json.loads(policy_path(settings).read_text())
    require(type(policy.get('enabled')) is bool and policy.get('version') == 1
            and policy.get('event_id') == EVENT_ID and policy.get('plan_sha256') == PLAN_SHA256
            and policy.get('entry_sha256') == list(ENTRY_DIGESTS) and policy.get('sender') == SENDER
            and policy.get('manifest_sha256') == checksum(settings.sources_file)
            and policy.get('headers_sha256') == checksum(settings.root / 'config/production-header-init.approved.json')
            and policy.get('template_sha256') == TEMPLATE_SHA256 == checksum(settings.template)
            and (settings.frame, settings.name_field, settings.tier_field, settings.qr_x, settings.qr_y, settings.qr_size)
            == ('front', 'displayName', 'ticketTier', 378, 228, 444),
            'Pilot authorization, reviewed schema or badge configuration changed.')
    return plan, policy


def existing(settings, plan, entry):
    """Query existing state without creating, migrating or chmodding a journal."""
    path = settings.runtime / 'production-batches.sqlite3'
    if not path.exists():
        return None, None, None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        action = db.execute('SELECT record_id,message_id,done FROM actions WHERE plan_id=? AND target=?',
                            (plan['plan_id'], entry['target'])).fetchone()
        if action is None:
            return None, None, None
        require(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='pilot_bindings'").fetchone()[0] == 1,
                'Existing execution was not authorized through this pilot; stop for review.')
        require(db.execute('SELECT digest FROM pilot_bindings WHERE plan_id=?', (plan['plan_id'],)).fetchone() == (digest(plan),),
                'Existing pilot journal binding differs from the saved plan.')
        receipt = db.execute('SELECT status,identity_hash,accepted_at FROM pilot_delivery WHERE plan_id=? AND target=?',
                             (plan['plan_id'], entry['target'])).fetchone()
        claim = db.execute('SELECT * FROM claims WHERE record_id=?', (action[0],)).fetchone()
        return action, receipt, claim


def bound_row(settings, plan, entry, rows):
    matches = [r for r in rows if r.source_id == entry['source'] and r.number == entry['snapshot_row']]
    require(len(matches) == 1 and fingerprint(matches[0]) == entry['fingerprint'],
            'A pilot registration changed or moved; no replacement is permitted.')
    row = matches[0]
    require(row.get('event_id') in ('', settings.event_id), 'Pilot registration belongs to another event.')
    action, receipt, claim = existing(settings, plan, entry)
    require(row.get('record_id') in ('', entry['record_id'], action[0] if action else None),
            'A pilot row acquired an unrelated registration identity.')
    if action:
        require(row.get('message_id') in ('', action[1]), 'Pilot Message-ID differs from its durable action.')
    if claim:
        require(action and claim[:3] == (settings.event_id, recipient(row), action[0])
                and claim[5:] == (*details(row), row.source_id) and row.get('record_id') == action[0],
                'Reserved pilot identity differs from the bound registration; review required.')
    if row.get('ticket_token') or row.get('participant_id'):
        expected = (settings.event_id, recipient(row), row.get('record_id'), row.get('participant_id'),
                    row.get('ticket_token'), *details(row), row.source_id)
        require(action and claim == expected and row.get('record_id') == action[0],
                'Pilot cannot adopt or replace an unrelated issued identity.')
        require((row.get('normalized_email'), row.get('ticket_name'), row.get('ticket_tier'),
                 row.get('price_category'), row.get('source_id')) == (recipient(row), *details(row), row.source_id),
                'Frozen pilot ticket details changed; review required.')
    if receipt:
        require(receipt[1] == issued_identity(row), 'Pilot receipt and frozen identity differ.')
    return row, action, receipt


def classify_recovery(settings, plan, entry, rows):
    """Use persisted job/identity/delivery evidence, never an action alone.

    Action reservations allocate a stable record ID and Message-ID for *every*
    selection before processing starts. They are not failed or recoverable jobs.
    Both diagnosis and execution use this classification and the same eligibility
    action; receipt and Sheet uncertainty always outrank ordinary resume/retry.
    """
    row, action, receipt = bound_row(settings, plan, entry, rows)
    claim = existing(settings, plan, entry)[2]
    issued = bool(row.get('participant_id') and row.get('ticket_token'))
    extra = []
    if issued:
        phase = 'ISSUED'
    else:
        phase = ('IDENTITY_RESERVED' if claim else 'INITIALIZED' if row.get('record_id')
                 else 'RESERVED' if action else 'UNSTARTED')
        if action:
            # Permit this action's own initialization/reservation, but never adopt
            # a claim or processing action owned by another registration/workflow.
            with closing(sqlite3.connect((settings.runtime / 'production-batches.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                owners = db.execute('SELECT record_id FROM claims WHERE event_id=? AND email=?',
                                    (settings.event_id, recipient(row))).fetchall()
                require(all(owner == (action[0],) for owner in owners), 'Another local identity reservation requires review.')
                owners = db.execute('SELECT plan_id,target FROM actions WHERE record_id=?', (action[0],)).fetchall()
                require(owners == [(plan['plan_id'], entry['target'])], 'Another processing action owns this registration.')
            legacy = settings.runtime / 'issuance.sqlite3'
            if legacy.exists():
                with closing(sqlite3.connect(legacy.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    db.execute('PRAGMA query_only=ON')
                    require(not db.execute('SELECT COUNT(*) FROM identities WHERE event_id=? AND email=?',
                                           (settings.event_id, recipient(row))).fetchone()[0],
                            'Existing legacy identity reservation requires review.')
        else:
            check_normal_reservations(settings, row)
        if row.get('record_id'):
            require(action and row.get('record_id') == action[0]
                    and (row.get('event_id'), row.get('source_id'), row.get('price_category'))
                    == (settings.event_id, row.source_id, details(row)[2]),
                    'Partial registration initialization does not match its reserved action.')
        if (row.get('state') not in ('', 'PENDING', 'WAITING_APPROVAL') or receipt
                or any(row.get(k) not in ('', None) for k in (
                    'participant_id', 'ticket_token', 'normalized_email', 'ticket_name', 'ticket_tier',
                    'render_status', 'card_path', 'render_attempts', 'email_status', 'email_attempts',
                    'message_id', 'email_sent_at', 'ticket_status', 'checked_in_at', 'checkin_request_id', 'checked_in_by'))):
            extra.append('Unissued registration has unverified processing history; staff review required.')
        if [row.get(k) for k in ('email_status', 'message_id', 'email_sent_at', 'email_attempts')] != entry['delivery']:
            extra.append('Pilot delivery history changed without a verified issued identity.')

    require(not receipt or receipt[0] in ('INTENT', 'UNCERTAIN', 'UNSENT', 'RETRY_AUTHORIZED', 'ACCEPTED'),
            'Unknown pilot delivery receipt requires review.')
    accepted = bool(receipt and receipt[0] == 'ACCEPTED')
    uncertain = (bool(receipt and receipt[0] in ('INTENT', 'UNCERTAIN'))
                 or row.get('email_status') in ('SENDING', 'AMBIGUOUS') or row.get('state') == 'EMAIL_REVIEW')
    failed = (bool(receipt and receipt[0] in ('UNSENT', 'RETRY_AUTHORIZED'))
              or row.get('state') in ('RENDER_ERROR', 'EMAIL_ERROR')
              or row.get('render_status') == 'FAILED' or row.get('email_status') == 'FAILED')
    retry_required = issued and failed and not accepted and not uncertain
    action_kind = 'retry' if retry_required else 'send'
    blocked = reasons(row, rows, settings.event_id, action_kind) + extra
    if uncertain and 'uncertain delivery requires staff review' not in blocked:
        blocked.append('uncertain delivery requires staff review')
    if (row.get('state') == 'SENT' or row.get('email_status') == 'SENT' or row.get('email_sent_at')) and not accepted:
        blocked.append('Sent history lacks its pilot receipt; review required.')
    if action and action[2] and not accepted:
        blocked.append('Completed action lacks its pilot acceptance receipt; review required.')
    if issued and row.get('state') not in ('PENDING', 'PROCESSING', 'READY', 'RENDER_ERROR', 'EMAIL_ERROR', 'EMAIL_REVIEW', 'SENT', 'CANCELLED'):
        blocked.append('Issued pilot processing state requires staff review.')
    if row.get('render_status') not in ('', 'RENDERING', 'RENDERED', 'FAILED') or row.get('email_status') not in (
            '', 'DISABLED', 'RETRY', 'SENDING', 'SENT', 'FAILED', 'AMBIGUOUS'):
        blocked.append('Unknown pilot processing status requires staff review.')
    recovery = ('COMPLETED' if accepted and action[2] else 'RECONCILE_ACCEPTANCE' if accepted
                else 'PROVIDER_REVIEW' if uncertain else 'BLOCKED' if blocked
                else 'EXPLICIT_RETRY' if retry_required else 'RESUME')
    return row, action, receipt, {'registration_phase': phase, 'eligibility_action': action_kind,
                                  'retry_required': bool(retry_required), 'recovery': recovery,
                                  'identity_reserved': claim is not None, 'issued': issued, 'blockers': blocked}


class PilotReader(LiveTestSheet):
    def __init__(self, settings, *, read_scheduler=None):
        # Only the read adapter uses a disabled-email clone. The mailer always
        # receives the actual scoped settings and rechecks the private flag.
        super().__init__(replace(settings, email_enabled=False), read_scheduler=read_scheduler, reviewed_reads=True)
        self.dataset = self.reader.dataset

    def rows(self):
        return self.reader.rows()  # Fresh schema gates + batched data for ALL sources.

    def read_for_update(self, source, number):
        return self.reader.read_for_update(source, number)


class PilotSettings:
    """Only this batch's metadata writes/email are scoped; the global lock stays."""
    def __init__(self, base, path, policy, mode='execute', reload_settings=None):
        self.base, self.path, self.policy, self.mode = base, path, policy, mode
        self.reload = reload_settings or (lambda: load_settings('production', base.root, pilot=True))

    def __getattr__(self, key):
        return getattr(self.base, key)

    def current(self):
        current = self.reload()
        _, policy = load_bound(current, self.path)
        require(policy == self.policy, 'Pilot authorization changed during execution.')
        return current

    def require_email(self):
        require(self.mode == 'execute' and self.base.email_enabled, 'EMAIL_ENABLED=false prohibits SMTP connection and sending.')
        current = self.current()
        require(current.email_enabled and self.policy['enabled'], 'Email or this exact pilot authorization is disabled.')
        require(parseaddr(current.smtp_from)[1] == SENDER and current.smtp_user == SENDER,
                'Pilot SMTP sender and authenticated account must match the approved organization address.')
        require(current.smtp_host and current.smtp_password and current.smtp_security in ('starttls', 'ssl'),
                'Complete the approved private SMTP settings.')
        require(all(getattr(current, key) == getattr(self.base, key) for key in
                    ('smtp_host', 'smtp_port', 'smtp_security', 'smtp_user', 'smtp_password', 'smtp_from')),
                'SMTP settings changed during the pilot.')

    def require_write(self):
        if self.mode == 'resolve':
            self.current()  # Explicit fact recording needs no SMTP or enabled email.
        else:
            self.require_email()


class PilotSheet:
    """All-source reads and exact selected-row metadata writes only."""
    def __init__(self, settings, plan, reader, write_transport=None):
        self.settings, self.plan, self.reader, self.transport = settings, plan, reader, write_transport
        self.live, self.dataset = reader.live, reader.dataset
        self.http = None

    def require_write(self):
        self.settings.require_write()

    def rows(self):
        rows = self.reader.rows()
        for entry in self.plan['entries']:
            bound_row(self.settings, self.plan, entry, rows)
        return rows

    def update(self, row, changes):
        self.require_write()
        entries = [e for e in self.plan['entries'] if (e['source'], e['snapshot_row']) == (row.source_id, row.number)]
        require(len(entries) == 1 and changes and set(changes) <= WRITE_FIELDS,
                'Pilot may write only its three selected registrations and approved tracking fields.')
        read_for_update = getattr(self.reader, 'read_for_update', None)
        if callable(read_for_update):
            rows, snapshot = read_for_update(row.source_id, row.number)
            for entry in self.plan['entries']:
                bound_row(self.settings, self.plan, entry, rows)
            fresh, _, _ = bound_row(self.settings, self.plan, entries[0], rows)
            require(fresh.data == row.data, 'Pilot registration changed before its tracking update.')
        else:
            # Explicit injected/fixture readers retain their original contract.
            fresh, _, _ = bound_row(self.settings, self.plan, entries[0], self.rows())
            require(fresh.data == row.data, 'Pilot registration changed before its tracking update.')
            snapshot = self.reader.capture(row.source_id, row.number)
            require(bound_row(self.settings, self.plan, entries[0], self.rows())[0].data == fresh.data,
                    'Pilot registration changed during typed-cell capture.')
        self.require_write()
        if self.transport:
            self.transport(snapshot, changes)
        else:
            if self.http is None:
                import gspread
                client = gspread.service_account(filename=str(self.settings.service_account),
                                                scopes=['https://www.googleapis.com/auth/spreadsheets'])
                client.set_timeout((10, 30))
                self.http = client.http_client
                pace_http(self.http, self.reader.read_scheduler)
            if not callable(read_for_update):
                require(self.reader.capture(row.source_id, row.number) == snapshot, 'Pilot cells changed immediately before write.')
            entry = next(e for e in self.reader.plan['sources'] if e['source']['id'] == row.source_id)
            requests = [{'updateCells': {'range': {'sheetId': entry['source']['tab_id'],
                         'startRowIndex': row.number - 1, 'endRowIndex': row.number,
                         'startColumnIndex': snapshot['cells'][key] - 1, 'endColumnIndex': snapshot['cells'][key]},
                         'rows': [{'values': [{'userEnteredValue': {'stringValue': str(value)}}]}],
                         'fields': 'userEnteredValue'}} for key, value in changes.items()]
            self.http.batch_update(entry['source']['spreadsheet_id'], {'requests': requests})
        row.data.update({key: str(value) for key, value in changes.items()})


class PilotJournal(BatchJournal):
    def __init__(self, settings):
        super().__init__(settings)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pilot_bindings (plan_id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS pilot_delivery (
                plan_id TEXT, target TEXT, status TEXT NOT NULL, identity_hash TEXT NOT NULL,
                accepted_at TEXT NOT NULL, attempts INTEGER NOT NULL, PRIMARY KEY(plan_id,target))''')
            db.execute('''CREATE TABLE IF NOT EXISTS pilot_delivery_events (
                id INTEGER PRIMARY KEY, plan_id TEXT, target TEXT, status TEXT, recorded_at TEXT, evidence TEXT)''')

    def db(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute('PRAGMA synchronous=FULL')
        return closing(db)

    def bind(self, plan):
        with self.db() as db:
            prior = db.execute('SELECT digest FROM pilot_bindings WHERE plan_id=?', (plan['plan_id'],)).fetchone()
            require(prior == (digest(plan),) or (prior is None and not db.execute(
                    'SELECT COUNT(*) FROM actions WHERE plan_id=?', (plan['plan_id'],)).fetchone()[0]),
                    'Pilot cannot adopt another workflow or a changed plan.')
        super().bind(plan)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO pilot_bindings VALUES (?,?)', (plan['plan_id'], digest(plan)))

    def claim(self, settings, row):
        require(bool(row.get('record_id')), 'Pilot registration ID persistence must be confirmed before reserving an identity.')
        return super().claim(settings, row)

    def record(self, settings, plan, entry, row, status, *, retry=False, evidence=''):
        key = (plan['plan_id'], entry['target'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT status,identity_hash,accepted_at,attempts FROM pilot_delivery WHERE plan_id=? AND target=?', key).fetchone()
            require(not old or old[1] == issued_identity(row), 'Receipt identity changed.')
            require(not old or old[0] != 'ACCEPTED' or status == 'ACCEPTED', 'SMTP acceptance can never be cleared or retried.')
            if status == 'INTENT':
                require(not old or retry and old[0] in ('UNSENT', 'RETRY_AUTHORIZED'), 'Uncertain delivery cannot automatically retry.')
            else:
                require(old is not None, 'No durable pilot delivery intent exists to resolve.')
            accepted = old[2] if old and old[2] else settings.now() if status == 'ACCEPTED' else ''
            attempts = (old[3] if old else 0) + int(status == 'INTENT')
            db.execute('INSERT OR REPLACE INTO pilot_delivery VALUES (?,?,?,?,?,?)',
                       (*key, status, issued_identity(row), accepted, attempts))
            db.execute('INSERT INTO pilot_delivery_events(plan_id,target,status,recorded_at,evidence) VALUES (?,?,?,?,?)',
                       (*key, status, settings.now(), evidence))
            db.execute('COMMIT')
        require(existing(settings, plan, entry)[1] == (status, issued_identity(row), accepted), 'Durable pilot receipt readback failed.')


class PilotProcessor(BatchProcessor):
    journal_class = PilotJournal

    def __init__(self, *args, plan, retry_target=None, first_sends_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan, self.retry_target, self.active = plan, retry_target, None
        self.first_sends_only = first_sends_only

    def execute(self, plan):
        require(plan == self.plan, 'Pilot processor cannot execute a different or modified plan.')
        return super().execute(plan)

    def save(self, row, **changes):
        status = changes.get('email_status')
        if status == 'SENDING':
            action = existing(self.s, self.plan, self.active)[0]
            require(action and changes['message_id'] == action[1], 'Message-ID must match the original durable pilot action.')
            self.journal.record(self.s, self.plan, self.active, row, 'INTENT', retry=self.retry_target == self.active['target'])
        elif status in ('FAILED', 'AMBIGUOUS'):
            receipt = existing(self.s, self.plan, self.active)[1]
            if receipt and receipt[0] != 'ACCEPTED':
                self.journal.record(self.s, self.plan, self.active, row, 'UNSENT' if status == 'FAILED' else 'UNCERTAIN')
        elif status == 'SENT':
            receipt = existing(self.s, self.plan, self.active)[1]
            require(receipt and receipt[0] == 'ACCEPTED', 'Sheet completion requires a durable SMTP acceptance receipt.')
            changes.update(email_sent_at=receipt[2], last_error='SMTP accepted; inbox delivery not confirmed.')
        super().save(row, **changes)
        if status == 'SENT':
            fresh, _, _ = bound_row(self.s, self.plan, self.active, self.sheet.rows())
            require(all(fresh.get(key) == str(value) for key, value in changes.items()),
                    'Final Sheet completion was not confirmed; durable SMTP acceptance is retained.')

    def process(self, plan, entry, rid, mid):
        self.active = entry
        if self.first_sends_only and entry['target'] not in [e['target'] for e in self.plan['entries'][1:]]:
            return 'outside remaining first sends; original attempt retained'
        if self.retry_target and entry['target'] != self.retry_target:
            return 'outside explicit retry'
        rows = self.sheet.rows()
        row, _, receipt, status = classify_recovery(self.s, self.plan, entry, rows)
        if receipt and receipt[0] == 'ACCEPTED':
            self.finish_accepted(row, entry, receipt)
            return 'sent'
        if status['recovery'] == 'PROVIDER_REVIEW':
            return 'review; no automatic resend'
        if row.get('state') == 'SENT' or row.get('email_status') == 'SENT':
            require(receipt and receipt[0] == 'ACCEPTED', 'Sent history lacks its pilot receipt; review required.')
            return 'already sent'
        if status['retry_required']:
            if self.retry_target != entry['target']:
                return 'explicit retry required'
        elif self.retry_target == entry['target']:
            return 'retry requires a verified failed pilot job'
        effective = deepcopy(plan)
        effective['action'] = status['eligibility_action']
        if status['blockers']:
            return 'blocked; no substitution'
        return super().process(effective, entry, rid, mid)

    def finish_accepted(self, row, entry, receipt):
        require(receipt[0] == 'ACCEPTED' and receipt[1] == issued_identity(row), 'Verified SMTP acceptance is required.')
        # Record an irreversible fact, retaining a later cancellation and any
        # original acceptance timestamp. This never means inbox delivery.
        require(row.get('email_sent_at') in ('', receipt[2]), 'Existing delivery timestamp differs; review required.')
        state = ('CANCELLED' if row.get('state') == 'CANCELLED' or row.get('ticket_status') == 'CANCELLED'
                 else row.get('state') if row.get('ticket_status') != 'ACTIVE' else 'SENT')
        self.save(row, state=state,
                  email_status='SENT', email_sent_at=receipt[2], last_error='SMTP accepted; inbox delivery not confirmed.')

    def accepted(self):
        # Do not put a network read between SMTP acceptance and its durable
        # receipt. The exact frozen snapshot was confirmed immediately before DATA.
        row = self.send_row
        receipt = existing(self.s, self.plan, self.active)[1]
        require(receipt and receipt[0] == 'INTENT', 'SMTP acceptance has no matching durable intent.')
        self.journal.record(self.s, self.plan, self.active, row, 'ACCEPTED')


def message_signature(message):
    return ([str(message.get(k, '')) for k in ('From', 'To', 'Subject', 'Message-ID')],
            [(p.get_content_type(), p.get_content_disposition(), str(p.get('Content-ID', '')), p.get_filename(),
              p.get_payload(decode=True)) for p in message.walk() if not p.is_multipart()])


def verify_ticket_message(settings, row, mailer, message, address, name, pid, card_path, mid, tier):
    """Shared frozen-ticket/badge/MIME verification; never issues or redraws a badge."""
    require((address, name, pid, tier) == (row.get('normalized_email'), row.get('ticket_name'),
            row.get('participant_id'), row.get('ticket_tier')), 'Frozen ticket message details changed.')
    path = Path(card_path).resolve()
    require(path == (settings.generated / pid / 'card.png').resolve() and row.get('render_status') == 'RENDERED'
            and Path(row.get('card_path')).resolve() == path, 'Pilot badge path or render status changed.')
    verify_render(settings, path, name, tier, row.get('ticket_token'))
    expected = mailer.build_message(address, name, pid, path, mid, ticket_tier=tier)
    require(message_signature(message) == message_signature(expected), 'Pilot MIME content differs from frozen ticket data.')


def inspect_pilot(settings, path, *, reader=None):
    plan, policy = load_bound(settings, path)
    inspect_template(settings)
    google_sans_files()
    reader = reader or PilotReader(settings)
    require(reader.live is True and reader.dataset == plan['dataset'], 'Pilot source dataset differs from its saved plan.')
    rows = reader.rows()
    entries = []
    for entry in plan['entries']:
        row, action, receipt, status = classify_recovery(settings, plan, entry, rows)
        blocked = status['blockers'] + (['failed job requires explicit retry'] if status['retry_required'] else [])
        entries.append({'target': entry['target'], 'completed': bool(action and action[2]),
                        'delivery': receipt[0] if receipt else 'NO_INTENT',
                        **status,
                        'blockers': safe_blockers(blocked)})
    return {'plan_sha256': PLAN_SHA256, 'selected_count': 3, 'sources_revalidated': True,
            'authorization_enabled': policy['enabled'], 'email_enabled': settings.email_enabled,
            'smtp_connected': False, 'participant_writes': 0, 'entries': entries,
            'smtp_acceptance_is_inbox_confirmation': False, 'general_production': 'LOCKED'}


def execute(settings, path, *, retry_target=None, first_sends_only=False, reader=None, renderer=None, write_transport=None,
            reload_settings=None, mailer_factory=SMTPMailer):
    plan, policy = load_bound(settings, path)
    scoped = PilotSettings(settings, path, policy, reload_settings=reload_settings)
    scoped.require_write()  # BEFORE Google, journal creation, issuance or rendering.
    require(not first_sends_only or not retry_target, 'Remaining first sends cannot be combined with a targeted retry.')
    require(not retry_target or retry_target in [e['target'] for e in plan['entries']], 'Retry must name one original pilot target.')
    reader = reader or PilotReader(settings)
    report = inspect_pilot(settings, path, reader=reader)
    require(all(not e['blockers'] for e in report['entries'] if not e['issued']),
            'Eligibility or unverified initialization history blocks the selected pilot registrations.')
    if retry_target:
        selected = next(e for e in report['entries'] if e['target'] == retry_target)
        require(selected['retry_required'], 'Explicit retry requires a verified failed pilot job, never a reservation or uncertain delivery.')
    sheet = PilotSheet(scoped, plan, reader, write_transport)
    require(sheet.live is True and sheet.dataset == plan['dataset'], 'Pilot adapter differs from the saved plan.')
    processor = PilotProcessor(scoped, sheet, renderer or CardRenderer(settings), None, plan=plan,
                               retry_target=retry_target, first_sends_only=first_sends_only)

    def before_submit(message, address, name, pid, card_path, mid, tier):
        scoped.require_email()
        entry = processor.active
        rows = sheet.rows()
        row, action, receipt = bound_row(scoped, plan, entry, rows)
        require(action and mid == action[1] and receipt and receipt[0] == 'INTENT'
                and (row.get('state'), row.get('email_status'), row.get('message_id')) == ('PROCESSING', 'SENDING', mid),
                'Persisted pilot send intent was not confirmed.')
        blocked = [b for b in reasons(row, rows, settings.event_id, 'retry') if b != 'uncertain delivery requires staff review']
        require(not blocked and (address, name, pid, tier) == (row.get('normalized_email'), row.get('ticket_name'),
                row.get('participant_id'), row.get('ticket_tier')), 'Final pilot eligibility or frozen details changed.')
        processor.journal.claim(scoped, row)
        verify_ticket_message(settings, row, processor.mailer, message, address, name, pid, card_path, mid, tier)
        # Badge verification can take time. Re-read all schemas, approvals,
        # cancellation, duplicates and delivery history immediately before DATA.
        fresh_rows = sheet.rows()
        fresh, _, fresh_receipt = bound_row(scoped, plan, entry, fresh_rows)
        require(fresh.data == row.data and fresh_receipt == receipt, 'Pilot changed during badge/message verification.')
        require(not [b for b in reasons(fresh, fresh_rows, settings.event_id, 'retry')
                     if b != 'uncertain delivery requires staff review'],
                'Approvals, cancellation or duplicates changed immediately before SMTP submission.')
        scoped.require_email()
        processor.send_row = fresh

    processor.mailer = mailer_factory(scoped, before_submit=before_submit, on_accept=processor.accepted)
    return processor.execute(plan)


def resolve(settings, path, target, outcome, evidence, *, reader=None, write_transport=None, reload_settings=None):
    plan, policy = load_bound(settings, path)
    require(outcome in ('accepted', 'not-sent') and isinstance(evidence, str) and 0 < len(evidence.strip()) <= 256,
            'Explicit provider review and a private evidence reference are required.')
    entries = [e for e in plan['entries'] if e['target'] == target]
    require(len(entries) == 1, 'Resolve only one original pilot target.')
    scoped = PilotSettings(settings, path, policy, mode='resolve', reload_settings=reload_settings)
    scoped.require_write()
    reader = reader or PilotReader(settings)
    require(reader.live is True and reader.dataset == plan['dataset'], 'Pilot recovery dataset differs.')
    sheet = PilotSheet(scoped, plan, reader, write_transport)
    entry = entries[0]
    with file_lock(settings.runtime / 'worker.lock', blocking=False), file_lock(settings.runtime / 'sheets.lock'):
        rows = sheet.rows()
        row, action, receipt = bound_row(scoped, plan, entry, rows)
        require(action and receipt and row.get('ticket_token'), 'Only an existing pilot delivery intent can be resolved.')
        journal = PilotJournal(scoped)
        processor = PilotProcessor(scoped, sheet, None, None, plan=plan)
        processor.journal, processor.active = journal, entry
        if outcome == 'accepted':
            require(receipt[0] in ('INTENT', 'UNCERTAIN', 'ACCEPTED'), 'A known rejection cannot be recorded as accepted.')
            journal.record(scoped, plan, entry, row, 'ACCEPTED', evidence=evidence)
            processor.finish_accepted(row, entry, existing(scoped, plan, entry)[1])
            journal.complete(plan, entry)
        else:
            require(not action[2] and receipt[0] in ('INTENT', 'UNCERTAIN', 'UNSENT'), 'Completed or accepted deliveries cannot be released for retry.')
            blocked = [b for b in reasons(row, rows, settings.event_id, 'retry')
                       if b not in ('uncertain delivery requires staff review', 'retry requires an existing recoverable job')]
            require(not blocked, 'Current eligibility blocks retry authorization.')
            # Record the Sheet failure first. If its response is lost, the old
            # receipt remains uncertain and cannot permit a second submission.
            processor.save(row, state='EMAIL_ERROR', email_status='FAILED', last_error='Provider review confirms not accepted; explicit pilot retry required.')
            journal.record(scoped, plan, entry, row, 'RETRY_AUTHORIZED', evidence=evidence)
    return {'outcome': outcome, 'smtp_connected': False, 'inbox_delivery_confirmed': False}


def run_command(settings, args):
    try:
        from .pilot_resend import OPERATIONS, run_command as run_resend
        if any(getattr(args, key, None) for key in OPERATIONS):
            return run_resend(settings, args)
        require(not getattr(args, 'acknowledge_duplicate', False), 'Duplicate acknowledgement applies only to the separate deliberate resend.')
        if args.deactivate:
            # Emergency closure remains possible even after a schema/template
            # drift that correctly prevents every execution/activation command.
            require(Path(args.plan).resolve() == (settings.root / PLAN_RELATIVE).resolve(), 'Deactivate only the official pilot path.')
            policy = json.loads(policy_path(settings).read_text())
            policy['enabled'] = False
            private_json(policy_path(settings), policy)
            print(json.dumps({'authorization_enabled': False, 'smtp_connected': False, 'general_production': 'LOCKED'}))
            return 0
        plan, policy = load_bound(settings, args.plan)
        require(not args.outcome and not args.evidence or args.resolve, 'Outcome/evidence require --resolve.')
        if args.preflight or args.diagnose:
            if args.preflight:
                require(not settings.email_enabled and not policy['enabled'], 'Disable pilot authorization and email before read-only preflight.')
            print(json.dumps(inspect_pilot(settings, args.plan), indent=2))
            return 0
        if args.activate:
            require(args.authorize_pilot and not settings.email_enabled,
                    'Activation requires --authorize-pilot with email still disabled.')
            policy['enabled'] = bool(args.activate)
            private_json(policy_path(settings), policy)
            print(json.dumps({'authorization_enabled': policy['enabled'], 'email_enabled': settings.email_enabled,
                              'smtp_connected': False, 'general_production': 'LOCKED'}))
            return 0
        require(args.authorize_pilot, 'Explicit --authorize-pilot is required for this exact plan and its three registrations.')
        if args.resolve:
            result = resolve(settings, args.plan, args.resolve, args.outcome, args.evidence)
        else:
            result = {'outcomes': execute(settings, args.plan, retry_target=args.retry,
                       first_sends_only=getattr(args, 'remaining_first_sends', False)), 'inbox_delivery_confirmed': False}
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'STOPPED', 'diagnostic': safe_diagnostic(exc, 'pilot_batch'), 'general_production': 'LOCKED'}))
        return 1
