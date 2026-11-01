"""One acknowledged duplicate-risk delivery, linked to the unresolved row-2 attempt.

Never writes the participant Sheet, allocates a ticket, redraws a badge, completes
the original action, or changes its receipt. Only new resend tables/events change.
"""
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from . import pilot
from .locking import file_lock
from .mailer import SMTPMailer, EmailNotSent
from .production import digest, issued_identity
from .production_sheets import PRODUCTION_METADATA

TARGET = 'discount_15:row:2'
OPERATIONS = ('prepare_resend', 'resend_preflight', 'activate_resend', 'deactivate_resend',
              'resend', 'resend_retry', 'resend_diagnose', 'resend_resolve')


def policy_path(settings):
    return settings.root/'config/pilot-resend.approved.json'


def operation_id(plan):
    return str(uuid.uuid5(uuid.UUID(plan['plan_id']), 'acknowledged-uncertain-resend:'+TARGET+':1'))


def parent_binding(settings, path):
    plan, policy = pilot.load_bound(settings, path)
    entry = next(e for e in plan['entries'] if e['target'] == TARGET)
    action, receipt, claim = pilot.existing(settings, plan, entry)
    pilot.require(action and not action[2] and receipt and receipt[0] in ('INTENT','UNCERTAIN') and claim,
                  'Only the existing unresolved, issued row-2 pilot attempt may have this deliberate resend.')
    pilot.require(claim[0] == settings.event_id and claim[2] == action[0] and claim[8] == entry['source'],
                  'Original identity reservation does not match this pilot target.')
    badge = (settings.generated/claim[3]/'card.png').resolve()
    pilot.require(badge.is_relative_to(settings.generated.resolve()) and badge.is_file(),
                  'The original retained badge must exist; no identity or badge may be regenerated.')
    return plan, policy, entry, action, receipt, badge


def binding_fields(settings, path):
    plan, base, entry, action, receipt, badge = parent_binding(settings, path)
    return {'version': 1, 'enabled': False, 'event_id': settings.event_id, 'target': TARGET,
            'plan_sha256': pilot.PLAN_SHA256, 'entry_sha256': digest(entry),
            'base_authorization_sha256': digest({k:v for k,v in base.items() if k != 'enabled'}),
            'parent_action_sha256': digest(action), 'parent_receipt_sha256': digest(receipt),
            'identity_sha256': receipt[1], 'badge_sha256': pilot.checksum(badge),
            'resend_id': operation_id(plan), 'sender': pilot.SENDER,
            'duplicate_risk_acknowledged': True}


def prepare(settings, path):
    _, base = pilot.load_bound(settings,path)
    pilot.require(not settings.email_enabled and not base['enabled'], 'Disable email and pilot authorization before local resend preparation.')
    policy = binding_fields(settings,path)
    destination = policy_path(settings)
    if destination.exists():
        pilot.require(json.loads(destination.read_text()) == policy, 'Existing resend authorization is preserved; stop for review.')
    else:
        fd = os.open(destination, os.O_CREAT|os.O_EXCL|os.O_WRONLY, 0o600)
        with os.fdopen(fd,'w') as output:
            json.dump(policy,output,indent=2); output.write('\n'); output.flush(); os.fsync(output.fileno())
    return policy


def load_policy(settings, path):
    policy = json.loads(policy_path(settings).read_text())
    expected = binding_fields(settings,path)
    pilot.require(type(policy.get('enabled')) is bool and {**policy,'enabled':False} == expected,
                  'Deliberate resend binding, original attempt, frozen identity or retained badge changed.')
    return policy


def policy_digest(policy): return digest({k:v for k,v in policy.items() if k != 'enabled'})


def read_attempt(settings, plan):
    path = settings.runtime/'production-batches.sqlite3'
    if not path.exists(): return None
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('PRAGMA query_only=ON'); db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pilot_resends'").fetchone(): return None
        row = db.execute('SELECT * FROM pilot_resends WHERE id=?',(operation_id(plan),)).fetchone()
        return dict(row) if row else None


class ResendSettings(pilot.PilotSettings):
    def __init__(self, base, path, original_policy, resend_policy, *, review=False, reload_settings=None):
        super().__init__(base,path,original_policy,mode='resolve' if review else 'execute',reload_settings=reload_settings)
        self.resend_policy = resend_policy

    def require_email(self):
        super().require_email()  # EMAIL_ENABLED, original pilot lock and pinned sender.
        current = self.current()
        policy = load_policy(current,self.path)
        pilot.require(policy == self.resend_policy and policy['enabled'], 'The separate deliberate resend authorization is disabled or changed.')


class ResendJournal(pilot.PilotJournal):
    def __init__(self, settings):
        super().__init__(settings)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS pilot_resends (
                id TEXT PRIMARY KEY, parent_plan_id TEXT, target TEXT, parent_record_id TEXT,
                parent_message_id TEXT, parent_receipt TEXT, identity_hash TEXT, badge_hash TEXT,
                original_tracking_hash TEXT, binding_hash TEXT, message_id TEXT UNIQUE,
                status TEXT, attempts INTEGER, accepted_at TEXT,
                UNIQUE(parent_plan_id,target))''')
            db.execute('''CREATE TABLE IF NOT EXISTS pilot_resend_events (
                id INTEGER PRIMARY KEY, resend_id TEXT, status TEXT, recorded_at TEXT, evidence TEXT)''')

    def reserve(self, settings, plan, policy, row, action, receipt):
        resend_id = operation_id(plan)
        mid = '<'+str(uuid.uuid5(uuid.UUID(resend_id),'message-id'))+'@event-checkin.local>'
        pilot.require(mid != action[1], 'Resend must have its own Message-ID.')
        tracking = digest([row.get(k) for k in PRODUCTION_METADATA])
        values = (resend_id,plan['plan_id'],TARGET,action[0],action[1],json.dumps(receipt),
                  issued_identity(row),policy['badge_sha256'],tracking,policy_digest(policy),mid,'RESERVED',0,'')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id FROM pilot_resends WHERE id=?',(resend_id,)).fetchone()
            if not old:
                db.execute('INSERT INTO pilot_resends VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
                db.execute('INSERT INTO pilot_resend_events(resend_id,status,recorded_at,evidence) VALUES (?,?,?,?)',
                           (resend_id,'RESERVED',settings.now(),'Explicit duplicate-risk acknowledgement; original outcome remains unresolved.'))
            db.execute('COMMIT')
        saved = read_attempt(settings,plan)
        pilot.require(saved and all(saved[k] == v for k,v in zip((
            'id','parent_plan_id','target','parent_record_id','parent_message_id','parent_receipt','identity_hash',
            'badge_hash','original_tracking_hash','binding_hash','message_id'),values[:11])),
            'Existing linked resend binding or original tracking changed; no replacement attempt permitted.')
        return saved

    def record(self, settings, plan, status, *, retry=False, evidence=''):
        old = read_attempt(settings,plan)
        pilot.require(old is not None, 'No linked resend exists to update.')
        allowed = {'RESERVED':('INTENT',), 'INTENT':('ACCEPTED','UNSENT','UNCERTAIN'),
                   'UNSENT':('INTENT','RETRY_AUTHORIZED'),
                   'UNCERTAIN':('ACCEPTED','RETRY_AUTHORIZED'), 'RETRY_AUTHORIZED':('INTENT',),
                   'ACCEPTED':('ACCEPTED',)}
        pilot.require(status in allowed.get(old['status'],()), 'Resend outcome cannot be cleared, downgraded or automatically repeated.')
        if status == 'INTENT' and old['status'] != 'RESERVED':
            pilot.require(retry, 'Known-unsent resend requires an explicit targeted retry.')
        if status == 'RETRY_AUTHORIZED' or status == 'ACCEPTED' and old['status'] not in ('INTENT','ACCEPTED'):
            pilot.require(bool(evidence.strip()), 'Provider evidence is required to resolve an uncertain resend.')
        accepted = old['accepted_at'] or (settings.now() if status == 'ACCEPTED' else '')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute('UPDATE pilot_resends SET status=?,attempts=attempts+?,accepted_at=? WHERE id=? AND status=?',
                                (status,int(status=='INTENT'),accepted,old['id'],old['status']))
            pilot.require(cursor.rowcount == 1, 'Resend state changed concurrently; stop for review.')
            db.execute('INSERT INTO pilot_resend_events(resend_id,status,recorded_at,evidence) VALUES (?,?,?,?)',
                       (old['id'],status,settings.now(),evidence))
            db.execute('COMMIT')
        saved = read_attempt(settings,plan)
        pilot.require(saved['status'] == status and saved['accepted_at'] == accepted, 'Durable resend outcome readback failed.')
        return saved


def validated_row(settings, path, plan, policy, reader, attempt=None):
    pilot.require(reader.live is True and reader.dataset == plan['dataset'], 'Resend source dataset changed.')
    rows = reader.rows()
    entry = next(e for e in plan['entries'] if e['target'] == TARGET)
    row,action,receipt,status = pilot.classify_recovery(settings,plan,entry,rows)
    pilot.require(status['issued'] and receipt and receipt[0] in ('INTENT','UNCERTAIN')
                  and digest(action) == policy['parent_action_sha256'] and digest(receipt) == policy['parent_receipt_sha256'],
                  'Only the acknowledged original unresolved delivery can be superseded by this separate attempt.')
    # This single exception is the user's acknowledged duplicate risk. Every
    # approval, duplicate, cancellation, identity and other blocker stays active.
    blocked = [b for b in status['blockers'] if b != 'uncertain delivery requires staff review']
    pilot.require(not blocked, 'Current approvals, duplicates, cancellation or frozen-ticket safeguards block the resend.')
    badge = Path(row.get('card_path')).resolve() if row.get('card_path') else None
    pilot.require(row.get('render_status') == 'RENDERED' and badge == (settings.generated/row.get('participant_id')/'card.png').resolve()
                  and badge.is_relative_to(settings.generated.resolve()) and badge.is_file()
                  and pilot.checksum(badge) == policy['badge_sha256'] and issued_identity(row) == policy['identity_sha256'],
                  'The existing frozen identity and exact retained badge must be reused; no redraw or replacement permitted.')
    if attempt:
        pilot.require(attempt['binding_hash'] == policy_digest(policy) and attempt['identity_hash'] == issued_identity(row)
                      and attempt['original_tracking_hash'] == digest([row.get(k) for k in PRODUCTION_METADATA]),
                      'Original tracking, delivery history or linked resend binding changed.')
    return row,action,receipt,badge


def execute(settings, path, *, acknowledged=False, retry=False, reader=None, reload_settings=None, mailer_factory=SMTPMailer):
    pilot.require(acknowledged, 'Explicit duplicate-risk acknowledgement is required for this deliberate resend.')
    plan,base = pilot.load_bound(settings,path); policy = load_policy(settings,path)
    scoped = ResendSettings(settings,path,base,policy,reload_settings=reload_settings)
    scoped.require_email()  # Before Google, journal creation or SMTP.
    old = read_attempt(settings,plan)
    if old and old['status'] == 'ACCEPTED': return 'accepted previously; no resubmission'
    if old and old['status'] in ('INTENT','UNCERTAIN'): return 'resend provider review required; no resubmission'
    if old and old['status'] in ('UNSENT','RETRY_AUTHORIZED') and not retry: return 'explicit resend retry required'
    pilot.require(not retry or old and old['status'] in ('UNSENT','RETRY_AUTHORIZED'), 'Only a known-unsent linked resend can be explicitly retried.')
    reader = reader or pilot.PilotReader(settings)
    with file_lock(settings.runtime/'worker.lock',blocking=False), file_lock(settings.runtime/'sheets.lock'):
        row,action,receipt,badge = validated_row(settings,path,plan,policy,reader,old)
        journal = ResendJournal(scoped)
        attempt = journal.reserve(scoped,plan,policy,row,action,receipt)
        # Recheck under the shared lock: another invocation may already have sent.
        if attempt['status'] == 'ACCEPTED': return 'accepted previously; no resubmission'
        if attempt['status'] in ('INTENT','UNCERTAIN'): return 'resend provider review required; no resubmission'
        scoped.require_email()
        intent = journal.record(scoped,plan,'INTENT',retry=retry)

        def before_submit(message,address,name,pid,card_path,mid,tier):
            scoped.require_email()
            current = read_attempt(settings,plan)
            pilot.require(current['status'] == 'INTENT' and mid == current['message_id'] == intent['message_id'],
                          'Durable linked resend intent could not be confirmed.')
            frozen,_,_,_ = validated_row(settings,path,plan,policy,reader,current)
            pilot.verify_ticket_message(settings,frozen,mailer,message,address,name,pid,card_path,mid,tier)
            final,_,_,_ = validated_row(settings,path,plan,policy,reader,current)
            pilot.require(final.data == frozen.data and read_attempt(settings,plan) == current,
                          'Approvals, duplicate status, frozen data or resend intent changed during final verification.')
            scoped.require_email()

        def accepted(): journal.record(scoped,plan,'ACCEPTED')  # No Google read after SMTP acceptance.

        mailer = mailer_factory(scoped,before_submit=before_submit,on_accept=accepted)
        try:
            mailer.send(row.get('normalized_email'),row.get('ticket_name'),row.get('participant_id'),badge,
                        intent['message_id'],ticket_tier=row.get('ticket_tier'))
        except EmailNotSent:
            journal.record(scoped,plan,'UNSENT')
            return 'resend known unsent; explicit retry required'
        except Exception:
            if read_attempt(settings,plan)['status'] != 'ACCEPTED': journal.record(scoped,plan,'UNCERTAIN')
            return 'resend acceptance retained' if read_attempt(settings,plan)['status'] == 'ACCEPTED' else 'resend uncertain; provider review required'
        return 'resend SMTP accepted; inbox delivery not confirmed'


def diagnose(settings,path,*,reader=None):
    plan,_ = pilot.load_bound(settings,path); policy = load_policy(settings,path)
    row,_,parent,_ = validated_row(settings,path,plan,policy,reader or pilot.PilotReader(settings),read_attempt(settings,plan))
    attempt = read_attempt(settings,plan)
    return {'target':TARGET,'original_delivery':parent[0],'resend_delivery':attempt['status'] if attempt else 'NOT_STARTED',
            'resend_authorization_enabled':policy['enabled'],'email_enabled':settings.email_enabled,
            'original_tracking_preserved':True,'existing_badge_checksum_matches':True,'smtp_connected':False,
            'inbox_delivery_confirmed':False}


def resolve(settings,path,outcome,evidence,*,reader=None,reload_settings=None):
    plan,base = pilot.load_bound(settings,path); policy = load_policy(settings,path)
    pilot.require(outcome in ('accepted','not-sent') and isinstance(evidence,str) and 0<len(evidence.strip())<=256,
                  'Explicit provider evidence is required for the linked resend outcome.')
    scoped = ResendSettings(settings,path,base,policy,review=True,reload_settings=reload_settings)
    scoped.require_write()
    with file_lock(settings.runtime/'worker.lock',blocking=False),file_lock(settings.runtime/'sheets.lock'):
        attempt = read_attempt(settings,plan)
        pilot.require(attempt and attempt['status'] in ('INTENT','UNCERTAIN','UNSENT','ACCEPTED'), 'Only an existing linked delivery outcome can be reviewed.')
        pilot.require(attempt['binding_hash'] == policy_digest(policy) and attempt['identity_hash'] == policy['identity_sha256'],
                      'Provider review must refer to the exact linked resend and frozen identity.')
        # Recording provider-confirmed acceptance is a fact, independent of later
        # approval/cancellation changes. Releasing a retry still requires fresh
        # all-source eligibility, identity and unchanged original tracking.
        if outcome == 'not-sent':
            validated_row(settings,path,plan,policy,reader or pilot.PilotReader(settings),attempt)
        journal = ResendJournal(scoped)
        journal.record(scoped,plan,'ACCEPTED' if outcome=='accepted' else 'RETRY_AUTHORIZED',evidence=evidence)
    return {'resend_review_recorded':outcome,'original_delivery_unchanged':True,'smtp_connected':False}


def run_command(settings,args):
    selected = [(name,getattr(args,name,None)) for name in OPERATIONS if getattr(args,name,None)]
    pilot.require(len(selected)==1 and selected[0][1]==TARGET, 'Only discount_15 row 2 has an approved deliberate resend.')
    operation,_ = selected[0]
    pilot.require(not (args.outcome or args.evidence) or operation=='resend_resolve', 'Outcome/evidence apply only to resend resolution.')
    if operation in ('prepare_resend','activate_resend','resend','resend_retry'):
        pilot.require(args.authorize_pilot and args.acknowledge_duplicate, 'Use --authorize-pilot and --acknowledge-duplicate explicitly.')
    if operation=='prepare_resend':
        prepare(settings,args.plan); result={'resend_authorization_enabled':False,'smtp_connected':False}
    elif operation in ('activate_resend','deactivate_resend'):
        if operation=='deactivate_resend':
            pilot.require(Path(args.plan).resolve() == (settings.root/pilot.PLAN_RELATIVE).resolve(), 'Use the exact pilot path for deactivation.')
            policy = json.loads(policy_path(settings).read_text())
        else:
            policy = load_policy(settings,args.plan)
        pilot.require(operation=='deactivate_resend' or not settings.email_enabled, 'Keep email disabled while activating the separate resend gate.')
        policy['enabled'] = operation=='activate_resend'; pilot.private_json(policy_path(settings),policy)
        result={'resend_authorization_enabled':policy['enabled'],'smtp_connected':False}
    elif operation in ('resend_preflight','resend_diagnose'):
        result=diagnose(settings,args.plan)
    elif operation=='resend_resolve':
        pilot.require(args.authorize_pilot,'Explicit linked-resend review authorization is required.')
        result=resolve(settings,args.plan,args.outcome,args.evidence)
    else:
        result={'outcome':execute(settings,args.plan,acknowledged=True,retry=operation=='resend_retry'),
                'original_delivery_unchanged':True,'inbox_delivery_confirmed':False}
    print(json.dumps(result)); return 0
