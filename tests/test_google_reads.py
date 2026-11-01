"""Fake time and instrumented real gspread request paths; never Google or SMTP."""
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch
import requests

from event_checkin import pilot
from event_checkin.config import ConfigurationError
from event_checkin.google_reads import ReadScheduler, pace_http, _schedulers
from event_checkin.header_init import HeaderTransport
from event_checkin.production_sheets import ProductionSheets
from event_checkin.sheets import SheetIntegrityError
from tests.sheet_transport import FakeClock, SheetsAPI, quota_error
from tests import test_pilot


class ReadSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.progress = FakeClock(), []
        self.scheduler = ReadScheduler(clock=self.clock.monotonic, sleep=self.clock.sleep,
                                       jitter=lambda a,b: 0.5, progress=self.progress.append)

    def test_shared_clients_and_sources_include_failed_attempts_in_pacing(self):
        attempts = []
        def get(method, *args, **kwargs):
            attempts.append(self.clock.monotonic())
            if len(attempts) == 2: raise quota_error()
            return 'ok'
        clients = [pace_http(type('HTTP', (), {'request': staticmethod(get)})(), self.scheduler) for _ in range(3)]
        for client in clients: self.assertEqual(client.request('get', 'synthetic'), 'ok')
        self.assertEqual(len(attempts), 4)
        self.assertTrue(all(b-a >= 1.25 for a,b in zip(attempts, attempts[1:])))
        self.assertEqual(self.scheduler.requests, 4)
        self.assertEqual(self.scheduler.retries, 1)

    def test_quota_backoff_is_bounded_exponential_with_jitter_and_safe_progress(self):
        request = Mock(side_effect=quota_error())
        with self.assertRaises(Exception): self.scheduler.read(request)
        self.assertEqual(request.call_count, 5)
        self.assertAlmostEqual(sum(self.clock.sleeps), 32.0)  # 2.5 + 4.5 + 8.5 + 16.5.
        self.assertEqual(self.scheduler.generation, 4)
        self.assertTrue(any('quota' in p for p in self.progress))
        self.assertNotIn('PRIVATE', '\n'.join(self.progress))
        self.assertIn('exhausted', self.progress[-1])

    def test_retry_after_and_total_wait_budget(self):
        error = quota_error(); error.response.headers['Retry-After'] = '12'
        request = Mock(side_effect=[error, 'ok'])
        self.assertEqual(self.scheduler.read(request), 'ok')
        self.assertEqual(self.clock.sleeps, [10.0, 2.0])
        error.response.headers['Retry-After'] = '200'
        with self.assertRaises(Exception): self.scheduler.read(Mock(side_effect=error))
        self.assertIn('budget exhausted', self.progress[-1])

    def test_only_retryable_reads_retry_never_write_or_permission_failures(self):
        for error in (quota_error(403, 'forbidden'), quota_error(400, 'badRequest')):
            request = Mock(side_effect=error)
            with self.assertRaises(Exception): self.scheduler.read(request)
            self.assertEqual(request.call_count, 1)
        for error in (quota_error(403, 'userRateLimitExceeded'), quota_error(503, 'backendError'), requests.ReadTimeout('PRIVATE')):
            request = Mock(side_effect=[error, 'ok'])
            self.assertEqual(self.scheduler.read(request), 'ok')
            self.assertEqual(request.call_count, 2)
        request = Mock(side_effect=quota_error())
        client = pace_http(type('HTTP', (), {'request': request})(), self.scheduler)
        with self.assertRaises(Exception): client.request('post', 'synthetic-write')
        self.assertEqual(request.call_count, 1)


class InstrumentedReadsTests(unittest.TestCase):
    def setUp(self):
        self.f = test_pilot.PilotTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups); self.addCleanup(self.f.tearDown)
        self.clock, self.progress = FakeClock(), []
        self.scheduler = ReadScheduler(clock=self.clock.monotonic, sleep=self.clock.sleep,
                                       jitter=lambda a,b: 0.5, progress=self.progress.append)
        self.api = SheetsAPI(self.f, self.clock)
        for target, kwargs in (
            ('event_checkin.pilot.PLAN_SHA256', {'new': self.f.policy['plan_sha256']}),
            ('gspread.service_account', {'side_effect': self.api.client}),
        ):
            p = patch(target, **kwargs); p.start(); self.addCleanup(p.stop)
        self.reader = pilot.PilotReader(self.f.s, read_scheduler=self.scheduler)

    def run_pilot(self):
        return pilot.execute(self.f.s, self.f.path, reader=self.reader, renderer=self.f.renderer,
                             reload_settings=lambda: self.f.s)

    def test_measured_http_reduction_and_central_pacing_including_client_initialization(self):
        self.assertEqual(self.api.count(), 0)  # Previous measured initialization: 6.
        rows = self.reader.rows()
        self.assertEqual(self.api.count(), 6)  # Previous measured all-source sweep: 21.
        self.f.enable()
        scoped = pilot.PilotSettings(self.f.s, self.f.path, self.f.policy, reload_settings=lambda:self.f.s)
        sheet = pilot.PilotSheet(scoped, self.f.plan, self.reader)
        selected = next(r for r in rows if (r.source_id,r.number) == ('discount_15',2))
        count = self.api.count(); sheet.update(selected, {'state': 'PENDING'})
        self.assertEqual(self.api.count()-count, 7)  # Previous measured tracking update: 52.
        self.assertEqual(sum(c['method'] != 'get' for c in self.api.calls), 1)
        key = str(self.f.s.service_account.resolve())
        with patch.dict(_schedulers, {key: self.scheduler}):
            ProductionSheets(self.f.s)
            transport = HeaderTransport(self.f.s, self.api.plan)
            transport.read(self.api.entries[0])
        from event_checkin.controlled_render import LiveTestSheet
        legacy = LiveTestSheet(replace(self.f.s,email_enabled=False),read_scheduler=self.scheduler)
        count = self.api.count(); legacy.capture('discount_15',2)
        self.assertEqual(self.api.count()-count,5)  # Instrumented original typed-capture path.
        times = [c['time'] for c in self.api.calls if c['method'] == 'get']
        self.assertTrue(all(b-a >= 1.25 for a,b in zip(times,times[1:])))
        self.assertEqual(len(times), self.scheduler.requests)

    def test_target_capture_is_one_request_preserves_types_and_reads_only_approved_columns(self):
        self.f.row()['updated_at'] = {'userEnteredValue': {'formulaValue': '="kept"'},
                                    'effectiveValue': {'stringValue': 'kept'}}
        count = self.api.count()
        result = self.reader.capture('discount_15', 2)
        self.assertEqual(self.api.count()-count, 1)
        self.assertEqual(result['values']['confirmation'], {'stringValue': 'TRUE'})
        self.assertEqual(result['values']['ticket_token'], {})
        self.assertEqual(result['values']['updated_at'], {'formulaValue': '="kept"'})
        call = self.api.calls[-1]
        self.assertEqual(len(call['params']['ranges']), 5)  # Row one plus four approved column groups.
        self.assertNotIn('formattedValue', call['params']['fields'])
        self.assertEqual(sum(c['method'] != 'get' for c in self.api.calls), 0)

    def test_incomplete_typed_capture_fails_closed_without_retrying_or_writing(self):
        original = self.reader.headers.http.fetch_sheet_metadata
        def partial(*args, **kwargs):
            result = original(*args, **kwargs)
            result['sheets'][0]['data'].pop()
            return result
        with patch.object(self.reader.headers.http, 'fetch_sheet_metadata', side_effect=partial):
            with self.assertRaises(SheetIntegrityError): self.reader.capture('discount_15',2)
        self.assertEqual(self.api.count(),1)
        self.assertFalse(any(c['method']!='get' for c in self.api.calls))

    def test_bad_source_schema_stops_before_any_participant_data_request(self):
        self.api.entries[-1]['original_headers'][2] = 'Synthetic changed header'
        with self.assertRaises(SheetIntegrityError): self.reader.rows()
        self.assertEqual(self.api.count(), 3)
        self.assertTrue(all(len(c['params']['ranges']) == 1 for c in self.api.calls))

    def test_quota_wait_discards_pre_wait_approvals_and_refreshes_every_source(self):
        failed = False
        self.f.edited_fields = {'confirmation'}
        def quota(call):
            nonlocal failed
            if not failed and call['book'].endswith('discount_10') and len(call['params'].get('ranges', [])) > 1:
                failed = True
                self.f.row()['confirmation'] = False
                raise quota_error()
        self.api.hook = quota
        rows = self.reader.rows()
        selected = next(r for r in rows if (r.source_id,r.number) == ('discount_15',2))
        self.assertIs(selected.get('confirmation'), False)
        self.assertEqual(self.api.count(), 13)  # Six-read sweep + one failed retry + complete fresh sweep.
        self.assertTrue(any('refreshing all source checks' in p for p in self.progress))

    def test_shared_retry_layer_refreshes_legacy_all_source_reads_too(self):
        reader = ProductionSheets(self.f.s,read_scheduler=self.scheduler)
        self.f.edited_fields = {'confirmation'}
        failed = False
        def quota(call):
            nonlocal failed
            if not failed and call['book'].endswith('discount_10') and '/values:batchGet' in call['path']:
                failed = True; self.f.row()['confirmation'] = False; raise quota_error()
        self.api.hook = quota
        count = self.api.count(); rows = reader.rows()
        selected = next(r for r in rows if (r.source_id,r.number)==('discount_15',2))
        self.assertIs(selected.get('confirmation'),False)
        self.assertEqual(self.api.count()-count,19)

    def test_shared_retry_layer_refreshes_legacy_typed_capture_after_post_capture_wait(self):
        from event_checkin.controlled_render import LiveTestSheet
        reader = LiveTestSheet(self.f.s,read_scheduler=self.scheduler)
        self.f.edited_fields = {'confirmation'}
        start = self.api.count(); failed = False
        def quota(call):
            nonlocal failed
            if not failed and self.api.count()==start+5:
                failed = True; self.f.row()['confirmation'] = False; raise quota_error()
        self.api.hook = quota
        result = reader.capture('discount_15',2)
        self.assertEqual(result['values']['confirmation'],{'boolValue':False})
        self.assertEqual(self.api.count()-start,11)

    def test_tracking_capture_quota_recovery_refreshes_all_sources_before_single_write(self):
        rows = self.reader.rows(); self.f.enable()
        scoped = pilot.PilotSettings(self.f.s, self.f.path, self.f.policy, reload_settings=lambda:self.f.s)
        sheet = pilot.PilotSheet(scoped, self.f.plan, self.reader)
        selected = next(r for r in rows if (r.source_id,r.number)==('discount_15',2))
        failed = False
        def quota(call):
            nonlocal failed
            if not failed and any(span.endswith(':AS2') for span in call['params'].get('ranges', [])):
                failed = True; raise quota_error()
        self.api.hook = quota
        count = self.api.count(); sheet.update(selected, {'state':'PENDING'})
        self.assertEqual(self.api.count()-count, 15)  # 7-read update + retry + entirely fresh 7-read update.
        self.assertEqual(sum(c['method'] != 'get' for c in self.api.calls), 1)

    def test_exhausted_read_before_later_entries_diagnoses_and_resumes_with_stable_ids(self):
        self.f.enable()
        failed = False
        def quota(call):
            nonlocal failed
            if failed or self.f.row().get('render_status') == 'RENDERING':
                failed = True; raise quota_error()
        self.api.hook = quota
        with self.assertRaises(Exception): self.run_pilot()
        self.assertTrue(failed)
        self.assertFalse(self.f.smtp.messages)
        actions = [pilot.existing(self.f.s,self.f.plan,e)[0] for e in self.f.plan['entries']]
        claim = pilot.existing(self.f.s,self.f.plan,self.f.plan['entries'][0])[2]
        self.api.hook = None
        report = pilot.inspect_pilot(self.f.s,self.f.path,reader=self.reader)
        self.assertEqual([e['registration_phase'] for e in report['entries']], ['ISSUED','RESERVED','RESERVED'])
        self.run_pilot(); self.run_pilot()
        self.assertEqual((self.f.row()['participant_id'],self.f.row()['ticket_token']), claim[3:5])
        self.assertEqual([str(m['Message-ID']) for m in self.f.smtp.messages], [a[1] for a in actions])
        self.assertEqual(len(self.f.smtp.messages), 3)

    def test_exact_original_cell_capture_quota_exhaustion_preserves_unpersisted_claim(self):
        from event_checkin.diagnostics import safe_diagnostic
        self.f.enable()
        def quota(call):
            if (self.f.row().get('record_id') and not self.f.row().get('participant_id')
                    and any(span.endswith(':AS2') for span in call['params'].get('ranges',[]))):
                raise quota_error()
        self.api.hook = quota
        with self.assertRaises(Exception) as captured: self.run_pilot()
        diagnostic = safe_diagnostic(captured.exception)
        self.assertEqual(diagnostic['stage'],'read_selected_original_cell_values')
        self.assertEqual(diagnostic['http_status'],429)
        self.assertEqual(diagnostic['google_api_reason'],'read_quota_exceeded')
        action,receipt,claim = pilot.existing(self.f.s,self.f.plan,self.f.plan['entries'][0])
        self.assertIsNone(receipt)
        self.assertTrue(claim)
        self.assertFalse(self.f.row().get('participant_id'))
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(sum(c['method']!='get' for c in self.api.calls),1)
        self.api.hook = None
        report = pilot.inspect_pilot(self.f.s,self.f.path,reader=self.reader)
        self.assertEqual(report['entries'][0]['registration_phase'],'IDENTITY_RESERVED')
        self.run_pilot(); self.run_pilot()
        self.assertEqual((self.f.row()['participant_id'],self.f.row()['ticket_token']),claim[3:5])
        self.assertEqual(str(self.f.smtp.messages[0]['Message-ID']),action[1])
        self.assertEqual(len(self.f.smtp.messages),3)

    def test_quota_exhaustion_after_durable_intent_never_automatically_resubmits(self):
        self.f.enable()
        def quota(call):
            if (self.f.s.runtime/'production-batches.sqlite3').exists():
                receipt = pilot.existing(self.f.s,self.f.plan,self.f.plan['entries'][0])[1]
                if receipt and receipt[0]=='INTENT': raise quota_error()
        self.api.hook = quota
        with self.assertRaises(Exception): self.run_pilot()
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(pilot.existing(self.f.s,self.f.plan,self.f.plan['entries'][0])[1][0],'INTENT')
        self.api.hook = None
        report = pilot.inspect_pilot(self.f.s,self.f.path,reader=self.reader)
        self.assertEqual(report['entries'][0]['recovery'],'PROVIDER_REVIEW')
        self.run_pilot(); self.run_pilot()
        self.assertEqual(len(self.f.smtp.messages),2)
        self.assertFalse(any(str(m['To'])==self.f.row()['email'] for m in self.f.smtp.messages))

    def test_exhaustion_after_acceptance_preserves_receipt_and_resume_does_not_resend(self):
        self.f.enable()
        def start_quota(): self.api.hook = lambda call: (_ for _ in ()).throw(quota_error())
        self.f.smtp.on_send = start_quota
        with self.assertRaises(Exception): self.run_pilot()
        self.assertEqual(pilot.existing(self.f.s,self.f.plan,self.f.plan['entries'][0])[1][0], 'ACCEPTED')
        self.assertEqual(len(self.f.smtp.messages), 1)
        self.api.hook = None; self.f.smtp.on_send = None
        self.run_pilot(); self.run_pilot()
        self.assertEqual(len(self.f.smtp.messages), 3)

    def test_quota_after_auth_never_uses_stale_approvals_or_retries_smtp(self):
        self.f.enable(); self.f.edited_fields = {'confirmation'}
        def quota_after_auth():
            failed = False
            def hook(call):
                nonlocal failed
                if not failed and call['book'].endswith('discount_10') and len(call['params'].get('ranges',[])) > 1:
                    failed = True; self.f.row()['confirmation'] = False; raise quota_error()
            self.api.hook = hook
        self.f.smtp.on_login = quota_after_auth
        with self.assertRaises(Exception): self.run_pilot()
        self.assertFalse(self.f.smtp.messages)
        self.assertEqual(self.f.smtp.connections, 1)

    def test_write_quota_is_not_retried_or_replayed(self):
        rows = self.reader.rows(); self.f.enable()
        scoped = pilot.PilotSettings(self.f.s,self.f.path,self.f.policy,reload_settings=lambda:self.f.s)
        sheet = pilot.PilotSheet(scoped,self.f.plan,self.reader)
        selected = next(r for r in rows if (r.source_id,r.number)==('discount_15',2))
        def quota(call):
            if call['method'] != 'get': raise quota_error()
        self.api.hook = quota
        with self.assertRaises(Exception): sheet.update(selected, {'state':'PENDING'})
        self.assertEqual(sum(c['method']!='get' for c in self.api.calls),1)


if __name__ == '__main__': unittest.main()
