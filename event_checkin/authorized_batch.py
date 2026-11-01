"""Private reusable plan policies over the shared, finite pilot execution engine."""
import json
import os
import re
import uuid
from dataclasses import replace
from email.utils import parseaddr
from pathlib import Path

from . import pilot
from .cards import google_sans_files, inspect_template
from .confirmation_email import TEMPLATE as EMAIL_TEMPLATE
from .diagnostics import safe_diagnostic
from .header_init import load_plan as load_header_plan
from .production import SOURCES, digest, private_json
from .single_send import checksum, require


def private_plan_path(settings, path):
    path = Path(path).resolve()
    require(path.is_relative_to((settings.runtime/'batch-reviews').resolve())
            and path.is_relative_to(settings.root.resolve()) and path.suffix == '.json',
            'Use a saved production plan inside runtime-production/batch-reviews.')
    require(path != (settings.root/pilot.PLAN_RELATIVE).resolve(), 'Use the legacy --pilot controls for the original pilot.')
    return path


def read_plan(settings, path):
    path = private_plan_path(settings, path)
    plan = json.loads(path.read_bytes())
    require(settings.profile == 'production' and settings.event_id and
            plan.get('profile') == 'production' and plan.get('live') is True
            and plan.get('event_id') == settings.event_id and plan.get('version') == 2
            and plan.get('action') == 'send' and plan.get('regenerate') is False,
            'Authorize only an unchanged version-2 production normal-send plan for this event.')
    require(str(uuid.UUID(plan['plan_id'])) == plan['plan_id'], 'Invalid saved plan identifier.')
    entries = plan.get('entries')
    require(isinstance(entries, list) and entries and type(plan.get('limit')) is int and
            0 < len(entries) <= plan['limit'], 'Authorize a nonempty finite selected-entry plan.')
    requested = plan.get('requested_limit')
    require((requested == 'all' and plan['limit'] == len(entries)) or
            (type(requested) is int and requested == plan['limit']), 'Saved plan limit changed.')
    require(len({e['target'] for e in entries}) == len(entries) and
            len({(e['source'], e['snapshot_row']) for e in entries}) == len(entries) and
            all(e['process'] is True and e['source'] in SOURCES and
                type(e['snapshot_row']) is int and e['snapshot_row'] >= 2 and
                re.fullmatch(r'[0-9a-f]{64}', e['fingerprint']) and
                e['target'] == e['source'] + ':' + (e['record_id'] or 'row:' + str(e['snapshot_row']))
                for e in entries), 'Selected registrations, locators or processing scope are invalid.')
    require(plan.get('tracking_scope') == {'policy': 'selected_entries_only', 'targets': [e['target'] for e in entries]},
            'Tracking scope must contain exactly the saved selected entries.')
    return plan


def authorization_path(settings, plan_id):
    require(str(uuid.UUID(plan_id)) == plan_id, 'Invalid authorization identifier.')
    return settings.root/'config/batch-authorizations'/ (plan_id + '.json')


def immutable_policy(policy):
    return {k: v for k, v in policy.items() if k != 'enabled'}


def binding_fields(settings, path):
    path = private_plan_path(settings, path)
    plan = read_plan(settings, path)
    # The header loader is a local validator whose original initialization scope
    # requires disabled email. A read-only clone preserves that boundary.
    headers = load_header_plan(replace(settings, email_enabled=False), settings.root/'config/production-header-init.approved.json')
    require(plan['dataset'] == digest([entry['source'] for entry in headers['sources']]),
            'Saved plan source dataset differs from the reviewed source configuration.')
    require(plan.get('source_order') == [entry['source']['id'] for entry in headers['sources']],
            'Saved source ordering differs from the reviewed configuration.')
    inspect_template(settings)
    fonts = google_sans_files()
    require(checksum(settings.template) == pilot.TEMPLATE_SHA256 and
            (settings.frame, settings.name_field, settings.tier_field, settings.qr_x, settings.qr_y, settings.qr_size)
            == ('front', 'displayName', 'ticketTier', 378, 228, 444), 'Use the reviewed badge template and bindings.')
    require(settings.smtp_user == pilot.SENDER and parseaddr(settings.smtp_from)[1] == pilot.SENDER,
            'Both SMTP sender fields must match the organization sender.')
    require(settings.smtp_host and type(settings.smtp_port) is int and settings.smtp_port > 0
            and settings.smtp_security in ('starttls', 'ssl'), 'Configure the organization TLS transport privately.')
    return {'version': 1, 'enabled': False, 'plan_id': plan['plan_id'],
        'event_id': settings.event_id, 'plan_path': str(path.relative_to(settings.root.resolve())),
        'plan_sha256': checksum(path), 'entry_sha256': [digest(e) for e in plan['entries']],
        'sender': pilot.SENDER, 'manifest_sha256': checksum(settings.sources_file),
        'headers_sha256': checksum(settings.root/'config/production-header-init.approved.json'),
        'template_sha256': checksum(settings.template), 'email_template_sha256': checksum(EMAIL_TEMPLATE),
        'fonts_sha256': {str(weight): checksum(font) for weight, font in fonts.items()},
        'configuration': {'runtime': str(settings.runtime.resolve()), 'generated': str(settings.generated.resolve()),
            'template': str(settings.template.resolve()), 'service_account': str(settings.service_account.resolve()),
            'freshcoat': str(settings.freshcoat.resolve()), 'bun': settings.bun, 'timezone': settings.timezone,
            'frame': settings.frame, 'name_field': settings.name_field, 'tier_field': settings.tier_field,
            'qr': [settings.qr_x, settings.qr_y, settings.qr_size]},
        'transport': {'host': settings.smtp_host, 'port': settings.smtp_port, 'security': settings.smtp_security,
                      'username': settings.smtp_user, 'from': settings.smtp_from}}


def prepare(settings, path):
    require(not settings.email_enabled, 'Prepare authorization only with EMAIL_ENABLED=false.')
    policy = binding_fields(settings, path)
    destination = authorization_path(settings, policy['plan_id'])
    if destination.exists():
        require(json.loads(destination.read_text()) == policy, 'Existing authorization is preserved; deactivate/review it first.')
        return destination
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(policy, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    return destination


class AuthorizedJournal(pilot.PilotJournal):
    def __init__(self, settings):
        super().__init__(settings)
        self.settings = settings
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS authorized_plan_bindings (
                plan_id TEXT PRIMARY KEY, event_id TEXT, plan_path TEXT, plan_sha256 TEXT,
                authorization_path TEXT, authorization_digest TEXT)''')

    def bind(self, plan):
        s = self.settings
        policy = s.policy
        values = (plan['plan_id'], s.event_id, policy['plan_path'], policy['plan_sha256'],
                  str(authorization_path(s, plan['plan_id']).relative_to(s.root)), digest(immutable_policy(policy)))
        with self.db() as db:
            previous = db.execute('SELECT * FROM authorized_plan_bindings WHERE plan_id=?', (plan['plan_id'],)).fetchone()
            require(previous == values or previous is None and not db.execute(
                'SELECT 1 FROM actions WHERE plan_id=?', (plan['plan_id'],)).fetchone(),
                'Cannot adopt a legacy action or changed authorization into this plan scope.')
        super().bind(plan)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO authorized_plan_bindings VALUES (?,?,?,?,?,?)', values)
            require(db.execute('SELECT * FROM authorized_plan_bindings WHERE plan_id=?',
                               (plan['plan_id'],)).fetchone() == values, 'Authorization binding readback failed.')


class PlanAuthorization:
    journal_class = AuthorizedJournal

    def load_bound(self, settings, path):
        plan = read_plan(settings, path)
        policy = json.loads(authorization_path(settings, plan['plan_id']).read_text())
        require(type(policy.get('enabled')) is bool and
                immutable_policy(policy) == immutable_policy(binding_fields(settings, path)),
                'Plan authorization, selected entries, sender, configuration or assets changed.')
        return plan, policy


def deactivate(settings, path):
    # Remains available if plan bytes/configuration/assets have drifted.
    path = private_plan_path(settings, path)
    relative = str(path.relative_to(settings.root.resolve()))
    matches = []
    for candidate in (settings.root/'config/batch-authorizations').glob('*.json'):
        policy = json.loads(candidate.read_text())
        if policy.get('plan_path') == relative:
            matches.append((candidate, policy))
    require(len(matches) == 1, 'Exactly one existing plan authorization must match this path.')
    destination, policy = matches[0]
    policy['enabled'] = False
    private_json(destination, policy)


def run_command(settings, args):
    try:
        scope = PlanAuthorization()
        require(not args.authorize_pilot and not args.acknowledge_duplicate and not args.remaining_first_sends
                and not any(getattr(args, k, None) for k in ('prepare_resend', 'resend_preflight', 'activate_resend',
                    'deactivate_resend', 'resend', 'resend_retry', 'resend_diagnose', 'resend_resolve')),
                'Legacy pilot/resend controls cannot authorize a reusable batch.')
        require(not args.outcome and not args.evidence or args.resolve, 'Outcome/evidence require --resolve.')
        if args.prepare_authorization:
            require(not args.authorize_plan, 'Preparation binds a disabled policy; it cannot authorize execution.')
            destination = prepare(settings, args.plan)
            print(json.dumps({'status': 'PREPARED', 'authorization': str(destination), 'authorization_enabled': False,
                              'smtp_connected': False, 'participant_writes': 0}))
            return 0
        if args.deactivate:
            deactivate(settings, args.plan)
            print(json.dumps({'authorization_enabled': False, 'smtp_connected': False}))
            return 0
        plan, policy = scope.load_bound(settings, args.plan)
        if args.preflight or args.diagnose:
            require(not settings.email_enabled and not policy['enabled'], 'Disable plan authorization and email before read-only review.')
            print(json.dumps(pilot.inspect_pilot(settings, args.plan, authorization=scope), indent=2))
            return 0
        if args.activate:
            require(args.authorize_plan and not settings.email_enabled, 'Activation requires --authorize-plan with email disabled.')
            policy['enabled'] = True
            private_json(authorization_path(settings, plan['plan_id']), policy)
            print(json.dumps({'authorization_enabled': True, 'email_enabled': False, 'smtp_connected': False}))
            return 0
        require(args.authorize_plan, 'Explicit --authorize-plan is required for this exact saved selection.')
        if args.resolve:
            require(not settings.email_enabled and not policy['enabled'], 'Disable email and authorization before provider review.')
            result = pilot.resolve(settings, args.plan, args.resolve, args.outcome, args.evidence, authorization=scope)
        else:
            result = {'outcomes': pilot.execute(settings, args.plan, retry_target=args.retry, authorization=scope),
                      'inbox_delivery_confirmed': False}
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'STOPPED', 'diagnostic': safe_diagnostic(exc, 'pilot_batch'),
                          'general_production': 'LOCKED'}))
        return 1
