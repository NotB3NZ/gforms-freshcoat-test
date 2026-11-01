"""Read-only delivery exclusions; evidence never authorizes issuance or a resend.

SMTP acceptance belongs to its own attempt. A linked resend can establish that a
registration was delivered without changing the original uncertain receipt.
"""
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime

from .sheets import SheetIntegrityError


@dataclass(frozen=True)
class Delivery:
    sent: bool = False
    accepted: bool = False
    blockers: tuple = ()


class DeliveryEvidence:
    def __init__(self, settings):
        self.settings = settings
        self.tables = {}
        path = settings.runtime / 'production-batches.sqlite3'
        try:
            if path.exists():
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    db.execute('PRAGMA query_only=ON')
                    db.row_factory = sqlite3.Row
                    db.execute('BEGIN')  # One coherent snapshot, including linked receipts/events.
                    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for name in ('plans', 'actions', 'claims', 'pilot_bindings', 'pilot_delivery',
                                 'pilot_delivery_events', 'pilot_resends', 'pilot_resend_events', 'authorized_plan_bindings'):
                        self.tables[name] = [dict(r) for r in db.execute('SELECT * FROM ' + name)] if name in names else []
            self.single = []
            for path in settings.runtime.rglob('*.smtp-evidence-*.json') if settings.runtime.exists() else ():
                evidence = json.loads(path.read_text())
                if not isinstance(evidence, dict):
                    raise ValueError('Invalid checkpoint')
                if evidence.get('accepted') is True:
                    selection_path = path.with_name(path.name.split('.smtp-evidence-')[0] + '.json')
                    self.single.append((evidence, selection_path))
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise SheetIntegrityError('Delivery evidence is unreadable; staff review required.') from None

    def rows(self, table, **keys):
        return [r for r in self.tables.get(table, ()) if all(r.get(k) == v for k, v in keys.items())]

    def action(self, plan, entry):
        from .production import digest
        bindings = self.rows('plans', id=plan['plan_id'])
        if bindings and bindings != [{'id': plan['plan_id'], 'digest': digest(plan)}]:
            raise SheetIntegrityError('A saved batch was edited after execution began; review required.')
        actions = self.rows('actions', plan_id=plan['plan_id'], target=entry['target'])
        if len(actions) > 1:
            raise SheetIntegrityError('Ambiguous delivery action; staff review required.')
        return actions[0] if actions else None

    def inspect(self, row, *, own_intent=None):
        from .production import details, digest, fingerprint, issued_identity, recipient, target
        sent = row.get('email_status') == 'SENT' or row.get('state') == 'SENT'
        accepted, unresolved = False, False
        rid = row.get('record_id')
        identity = issued_identity(row)
        locators = {target(row), row.source_id + ':row:' + str(row.number)}

        def require(condition):
            if not condition:
                raise ValueError('binding')

        def claim_binding():
            require(bool(rid and row.get('participant_id') and row.get('ticket_token')))
            require(row.get('event_id') == self.settings.event_id)
            require((row.get('normalized_email'), row.get('ticket_name'), row.get('ticket_tier'),
                     row.get('price_category'), row.get('source_id')) == (recipient(row), *details(row), row.source_id))
            expected = dict(event_id=self.settings.event_id, email=row.get('normalized_email'), record_id=rid,
                            participant_id=row.get('participant_id'), token=row.get('ticket_token'),
                            name=row.get('ticket_name'), tier=row.get('ticket_tier'),
                            price=row.get('price_category'), source=row.source_id)
            require(self.rows('claims', record_id=rid) == [expected])

        def parent_binding(plan_id, locator, record_id):
            claim_binding()
            actions = self.rows('actions', plan_id=plan_id, target=locator)
            require(len(actions) == 1 and actions[0]['record_id'] == record_id == rid)
            plans = self.rows('plans', id=plan_id)
            bindings = self.rows('pilot_bindings', plan_id=plan_id)
            require(len(plans) == len(bindings) == 1 and plans[0]['digest'] == bindings[0]['digest'])
            from .pilot import PLAN_RELATIVE
            scopes = self.rows('authorized_plan_bindings', plan_id=plan_id)
            if scopes:
                require(len(scopes) == 1 and scopes[0]['event_id'] == self.settings.event_id)
                scope = scopes[0]
                from .authorized_batch import authorization_path, immutable_policy, private_plan_path
                plan_path = private_plan_path(self.settings, self.settings.root/scope['plan_path'])
                policy_path = authorization_path(self.settings, plan_id)
                require(str(policy_path.relative_to(self.settings.root)) == scope['authorization_path'])
                policy = json.loads(policy_path.read_text())
                require(digest(immutable_policy(policy)) == scope['authorization_digest']
                        and policy['plan_id'] == plan_id and policy['event_id'] == self.settings.event_id
                        and policy['plan_path'] == scope['plan_path'])
                encoded = plan_path.read_bytes()
                require(hashlib.sha256(encoded).hexdigest() == scope['plan_sha256'] == policy['plan_sha256'])
            else:
                encoded = (self.settings.root / PLAN_RELATIVE).read_bytes()
            plan = json.loads(encoded)
            entries = [e for e in plan['entries'] if e['target'] == locator]
            require(plan['plan_id'] == plan_id and plan['event_id'] == self.settings.event_id
                    and plan['profile'] == self.settings.profile and digest(plan) == plans[0]['digest'])
            require(len(entries) == 1 and entries[0]['source'] == row.source_id
                    and entries[0]['fingerprint'] == fingerprint(row))
            if scopes:
                require(policy['entry_sha256'] == [digest(e) for e in plan['entries']])
            return actions[0], hashlib.sha256(encoded).hexdigest(), digest(entries[0])

        def related(locator, record_id, receipt_identity):
            return (locator in locators or bool(rid and rid == record_id)
                    or bool(row.get('ticket_token') and identity == receipt_identity))

        def acceptance_event(table, accepted_at, **keys):
            stamp = datetime.fromisoformat(accepted_at)
            require(stamp.tzinfo is not None)
            events = self.rows(table, status='ACCEPTED', **keys)
            return any(datetime.fromisoformat(event['recorded_at']) >= stamp for event in events)

        try:
            for event in self.tables.get('pilot_delivery_events', ()):
                actions = self.rows('actions', plan_id=event['plan_id'], target=event['target'])
                record_id = actions[0]['record_id'] if len(actions) == 1 else None
                if related(event['target'], record_id, None):
                    require(len(self.rows('pilot_delivery', plan_id=event['plan_id'], target=event['target'])) == 1)
            for receipt in self.tables.get('pilot_delivery', ()):
                actions = self.rows('actions', plan_id=receipt['plan_id'], target=receipt['target'])
                record_id = actions[0]['record_id'] if len(actions) == 1 else None
                if not related(receipt['target'], record_id, receipt['identity_hash']):
                    continue
                parent_binding(receipt['plan_id'], receipt['target'], record_id)
                require(receipt['identity_hash'] == identity and receipt['attempts'] >= 1)
                status = receipt['status']
                require(status in ('INTENT', 'UNCERTAIN', 'UNSENT', 'RETRY_AUTHORIZED', 'ACCEPTED'))
                if status == 'ACCEPTED':
                    require(bool(receipt['accepted_at']) and acceptance_event('pilot_delivery_events',
                        receipt['accepted_at'], plan_id=receipt['plan_id'], target=receipt['target']))
                    accepted = True
                else:
                    require(not receipt['accepted_at'] and not self.rows('pilot_delivery_events',
                        plan_id=receipt['plan_id'], target=receipt['target'], status='ACCEPTED'))
                    unresolved |= status in ('INTENT', 'UNCERTAIN') and not (
                        status == 'INTENT' and own_intent == (receipt['plan_id'], receipt['target']))

            for receipt in self.tables.get('pilot_resends', ()):
                if not related(receipt['target'], receipt['parent_record_id'], receipt['identity_hash']):
                    continue
                action, plan_hash, entry_hash = parent_binding(receipt['parent_plan_id'], receipt['target'], receipt['parent_record_id'])
                parents = self.rows('pilot_delivery', plan_id=receipt['parent_plan_id'], target=receipt['target'])
                require(len(parents) == 1)
                parent = parents[0]
                require(parent['status'] in ('INTENT', 'UNCERTAIN') and not action['done'])
                require(json.loads(receipt['parent_receipt']) == [parent['status'], parent['identity_hash'], parent['accepted_at']])
                require(receipt['parent_message_id'] == action['message_id'])
                require(receipt['identity_hash'] == identity == parent['identity_hash'])
                expected_id = str(uuid.uuid5(uuid.UUID(receipt['parent_plan_id']),
                    'acknowledged-uncertain-resend:' + receipt['target'] + ':1'))
                expected_mid = '<' + str(uuid.uuid5(uuid.UUID(expected_id), 'message-id')) + '@event-checkin.local>'
                require(receipt['id'] == expected_id and receipt['message_id'] == expected_mid != action['message_id'])
                # Authorization can be disabled after sending. Its immutable binding
                # remains evidence; its enabled flag never grants normal-send scope.
                policy_path = self.settings.root / 'config/pilot-resend.approved.json'
                policy = json.loads(policy_path.read_text())
                require(receipt['binding_hash'] == digest({k: v for k, v in policy.items() if k != 'enabled'}))
                require(policy['event_id'] == self.settings.event_id and policy['target'] == receipt['target']
                        and policy['plan_sha256'] == plan_hash and policy['entry_sha256'] == entry_hash
                        and policy['resend_id'] == receipt['id'] and policy['identity_sha256'] == identity
                        and policy['badge_sha256'] == receipt['badge_hash']
                        and policy['parent_action_sha256'] == digest([action[k] for k in ('record_id', 'message_id', 'done')])
                        and policy['parent_receipt_sha256'] == digest(json.loads(receipt['parent_receipt'])))
                require(receipt['status'] in ('RESERVED', 'INTENT', 'UNCERTAIN', 'UNSENT', 'RETRY_AUTHORIZED', 'ACCEPTED'))
                if receipt['status'] == 'ACCEPTED':
                    require(receipt['attempts'] >= 1 and bool(receipt['accepted_at']) and acceptance_event(
                        'pilot_resend_events', receipt['accepted_at'], resend_id=receipt['id']))
                    accepted = True
                else:
                    require(not receipt['accepted_at'] and not self.rows('pilot_resend_events',
                        resend_id=receipt['id'], status='ACCEPTED'))
                    unresolved |= receipt['status'] in ('INTENT', 'UNCERTAIN')

            for evidence, path in self.single:
                selection_bytes = path.read_bytes()
                selection = json.loads(selection_bytes)
                plan = selection['plan']
                entry = plan['entries'][0]
                actions = self.rows('actions', plan_id=plan['plan_id'], target=entry['target'])
                if not related(entry['target'], actions[0]['record_id'] if len(actions) == 1 else None, entry.get('issued_identity')):
                    continue
                require(evidence['selection_sha256'] == hashlib.sha256(selection_bytes).hexdigest())
                require(selection['event_id'] == plan['event_id'] == self.settings.event_id
                        and entry['source'] == row.source_id and entry['fingerprint'] == fingerprint(row))
                claim_binding()
                require(len(actions) == 1 and actions[0]['record_id'] == rid)
                require(self.rows('plans', id=plan['plan_id']) == [{'id': plan['plan_id'], 'digest': digest(plan)}])
                # Legacy checkpoints bind a selection, not an individual retry.
                # Multiple actions cannot be attributed safely from that evidence.
                require(len(self.rows('actions', record_id=rid)) == 1)
                accepted = True
        except (KeyError, IndexError, AttributeError, ValueError, TypeError, OSError):
            return Delivery(sent, False, ('delivery receipt binding mismatch or ambiguity; staff review required',))
        blockers = ('uncertain delivery requires staff review',) if unresolved and not accepted else ()
        return Delivery(sent or accepted, accepted, blockers)
