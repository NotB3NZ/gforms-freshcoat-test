import asyncio
import json
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from event_checkin.checkin import CheckinService, RequestConflict
from event_checkin.config import Settings, ROOT
from event_checkin.worker import Worker
from event_checkin.mailer import EmailNotSent, EmailAmbiguous
from event_checkin.scanner import VisibilityGate, ScanClient
from event_checkin.sheets import SheetIntegrityError
from tests.fakes import FakeSheet, FakeRenderer, FakeMailer, registration, ticket


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.s = Settings(root=root, runtime=root / "runtime", generated=root / "generated", event_id="test-event",
                          staff_key="test-staff-key-" + "x" * 32, smtp_host="fake", smtp_user="fake",
                          smtp_password="fake", smtp_from="test@example.test", email_enabled=True, worker_batch_size=100)

    def tearDown(self):
        self.temp.cleanup()


class CheckinTests(Base):
    def service(self, **changes):
        sheet = FakeSheet([ticket(**changes)])
        return sheet, CheckinService(self.s, sheet)

    def test_first_duplicate_invalid(self):
        sheet, service = self.service()
        accepted = service.check_in("a" * 43, str(uuid.uuid4()))
        self.assertEqual(accepted["status"], "accepted")
        self.assertTrue(accepted["checked_in_at"].endswith("+08:00"))
        duplicate = service.check_in("a" * 43, str(uuid.uuid4()))
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["checked_in_at"], accepted["checked_in_at"])
        self.assertEqual(service.check_in("b" * 43, str(uuid.uuid4()))["status"], "invalid")
        self.assertEqual(sheet.writes, 1)

    def test_simultaneous_scans_one_ticket(self):
        sheet, service = self.service()
        # Separate service objects also coordinate through the same process/file lock.
        services = [service, CheckinService(self.s, sheet)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: services[i % 2].check_in("a" * 43, str(uuid.uuid4())), range(16)))
        self.assertEqual(sum(r["status"] == "accepted" for r in results), 1)
        self.assertEqual(sum(r["status"] == "duplicate" for r in results), 15)
        self.assertEqual(sheet.writes, 1)

    def test_request_retry_after_restart_and_other_scan(self):
        sheet, service = self.service()
        request_id = str(uuid.uuid4())
        result = service.check_in("a" * 43, request_id)
        service.check_in("a" * 43, str(uuid.uuid4()))
        restarted = CheckinService(self.s, sheet)
        self.assertEqual(restarted.check_in("a" * 43, request_id), result)
        self.assertEqual(sheet.writes, 1)
        with self.assertRaises(RequestConflict):
            restarted.check_in("b" * 43, request_id)

    def test_simultaneous_same_request(self):
        sheet, service = self.service()
        request_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: service.check_in("a" * 43, request_id), range(8)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(sheet.writes, 1)

    def test_lost_sheet_response_reconciles(self):
        sheet, service = self.service()
        sheet.fail_after = lambda c: "checked_in_at" in c
        request_id = str(uuid.uuid4())
        with self.assertRaises(ConnectionError):
            service.check_in("a" * 43, request_id)
        original_timestamp = sheet.data[0]["checked_in_at"]
        retry = CheckinService(self.s, sheet).check_in("a" * 43, request_id)
        self.assertEqual(retry["status"], "accepted")
        self.assertEqual(retry["checked_in_at"], original_timestamp)
        self.assertEqual(sheet.writes, 1)

    def test_failed_write_before_commit_can_retry(self):
        sheet, service = self.service()
        sheet.fail_before = lambda c: "checked_in_at" in c
        request_id = str(uuid.uuid4())
        with self.assertRaises(ConnectionError):
            service.check_in("a" * 43, request_id)
        self.assertNotIn("checked_in_at", sheet.data[0])
        self.assertEqual(service.check_in("a" * 43, request_id)["status"], "accepted")

    def test_wrong_event_unrendered_and_conflicting_email(self):
        sheet, service = self.service(event_id="another-event")
        self.assertEqual(service.check_in("a" * 43, str(uuid.uuid4()))["status"], "invalid")
        sheet.data[0] = ticket(render_status="RENDERING", state="PROCESSING")
        self.assertEqual(service.check_in("a" * 43, str(uuid.uuid4()))["status"], "invalid")
        sheet.data = [ticket(), ticket(participant_id="P-other", ticket_token="b" * 43)]
        with self.assertRaises(SheetIntegrityError):
            service.check_in("a" * 43, str(uuid.uuid4()))

    def test_lookup_and_update_after_sort(self):
        sheet, service = self.service()
        sheet.data.insert(0, ticket(participant_id="P-other", ticket_token="b" * 43, normalized_email="other@example.test"))
        service.check_in("a" * 43, str(uuid.uuid4()))
        self.assertNotIn("checked_in_at", sheet.data[0])
        self.assertIn("checked_in_at", sheet.data[1])

    def test_cancelled_and_unknown_ticket_status_never_admit(self):
        for changes in ({"ticket_status": "CANCELLED"}, {"ticket_status": "UNKNOWN"}, {"state": "CANCELLED"}):
            with self.subTest(changes=changes):
                sheet, service = self.service(**changes)
                self.assertEqual(service.check_in("a" * 43, str(uuid.uuid4()))["status"], "invalid")
                self.assertEqual(sheet.writes, 0)

    def test_station_survives_failed_write_restart_and_other_station_retry(self):
        sheet, service = self.service()
        sheet.fail_before = lambda c: "checked_in_at" in c
        request_id = str(uuid.uuid4())
        with self.assertRaises(ConnectionError):
            service.check_in("a" * 43, request_id, "Entrance A")
        restarted = CheckinService(self.s, sheet)
        result = restarted.check_in("a" * 43, request_id, "Entrance B")
        self.assertEqual(result["checked_in_by"], "Entrance A")
        self.assertEqual(sheet.data[0]["checked_in_by"], "Entrance A")
        duplicate = restarted.check_in("a" * 43, str(uuid.uuid4()), "Entrance B")
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["checked_in_by"], "Entrance A")
        self.assertEqual(sheet.writes, 1)

    def test_existing_ticket_without_new_status_still_works(self):
        sheet, service = self.service(ticket_status="")
        self.assertEqual(service.check_in("a" * 43, str(uuid.uuid4()))["status"], "accepted")


class WorkerTests(Base):
    def worker(self, rows=None):
        self.sheet = FakeSheet(rows if rows is not None else [registration()])
        self.renderer = FakeRenderer(self.s)
        self.mailer = FakeMailer()
        self.worker_service = Worker(self.s, self.sheet, self.renderer, self.mailer)
        return self.worker_service

    def test_identity_saved_before_render(self):
        worker = self.worker()
        original = self.renderer.render
        def render(pid, name, token):
            self.assertEqual(self.sheet.data[0]["participant_id"], pid)
            self.assertEqual(self.sheet.data[0]["ticket_token"], token)
            return original(pid, name, token)
        self.renderer.render = render
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "SENT")
        self.assertEqual(len(self.sheet.data[0]["ticket_token"]), 43)

    def test_render_failure_retry_reuses_ticket(self):
        worker = self.worker()
        self.renderer.fail = True
        worker.run_once()
        row = self.sheet.data[0]
        self.assertEqual(row["state"], "RENDER_ERROR")
        self.assertFalse(self.mailer.calls)
        pid, token = row["participant_id"], row["ticket_token"]
        worker.retry(pid)
        worker.run_once()
        self.assertEqual(row["state"], "SENT")
        self.assertEqual((row["participant_id"], row["ticket_token"]), (pid, token))
        self.assertEqual(row["render_attempts"], "2")

    def test_email_failure_retry_skips_render(self):
        worker = self.worker()
        self.mailer.failure = EmailNotSent
        worker.run_once()
        row = self.sheet.data[0]
        self.assertEqual((row["state"], row["render_status"]), ("EMAIL_ERROR", "RENDERED"))
        worker.retry(row["participant_id"])
        worker.run_once()
        self.assertEqual(row["state"], "SENT")
        self.assertEqual(len(self.renderer.calls), 1)
        self.assertEqual(self.mailer.calls[0][-1], self.mailer.calls[1][-1])

    def test_ambiguous_email_requires_review(self):
        worker = self.worker()
        self.mailer.failure = EmailAmbiguous
        worker.run_once()
        row = self.sheet.data[0]
        self.assertEqual(row["state"], "EMAIL_REVIEW")
        worker.run_once()
        self.assertEqual(len(self.mailer.calls), 1)
        with self.assertRaises(ValueError):
            worker.retry(row["participant_id"])
        worker.retry(row["participant_id"], resolution="sent")
        self.assertEqual(row["state"], "SENT")
        self.assertEqual(len(self.mailer.calls), 1)

    def test_interrupted_render_and_email(self):
        worker = self.worker([ticket(state="PROCESSING", render_status="RENDERING", email_status="")])
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "SENT")
        worker = self.worker([ticket(state="PROCESSING", email_status="SENDING")])
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "EMAIL_REVIEW")
        self.assertFalse(self.mailer.calls)

    def test_lost_identity_save_response_never_mints_another_ticket(self):
        worker = self.worker()
        self.sheet.fail_after = lambda c: "ticket_token" in c
        with self.assertRaises(ConnectionError):
            worker.run_once()
        token = self.sheet.data[0]["ticket_token"]
        self.assertFalse(self.renderer.calls)
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["ticket_token"], token)
        self.assertEqual(self.sheet.data[0]["state"], "SENT")

    def test_failed_identity_save_does_not_render_or_send(self):
        worker = self.worker()
        self.sheet.fail_before = lambda c: "ticket_token" in c
        with self.assertRaises(ConnectionError):
            worker.run_once()
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.mailer.calls)
        self.assertNotIn("ticket_token", self.sheet.data[0])
        attempted = self.sheet.attempts[0]
        # Reconstruct the worker as if it crashed after an uncertain request.
        worker = Worker(self.s, self.sheet, self.renderer, self.mailer)
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "SENT")
        self.assertEqual(self.sheet.data[0]["participant_id"], attempted["participant_id"])
        self.assertEqual(self.sheet.data[0]["ticket_token"], attempted["ticket_token"])

    def test_lost_sent_save_response_does_not_resend(self):
        worker = self.worker()
        self.sheet.fail_before = lambda c: c.get("state") == "SENT"
        with self.assertRaises(ConnectionError):
            worker.run_once()
        self.assertEqual(len(self.mailer.calls), 1)
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "EMAIL_REVIEW")
        self.assertEqual(len(self.mailer.calls), 1)

    def test_repeated_registration_and_other_event(self):
        worker = self.worker([registration(email="  SAMPLE@EXAMPLE.TEST "), registration(), registration(event_id="another-event")])
        worker.run_once()
        first, second, third = self.sheet.data
        self.assertEqual(first["state"], "SENT")
        self.assertEqual(second["state"], "DUPLICATE")
        self.assertEqual(second["duplicate_of"], first["participant_id"])
        self.assertNotIn("ticket_token", second)
        self.assertEqual(third["state"], "PENDING")
        self.assertEqual(len(self.mailer.calls), 1)

    def test_issued_ticket_wins_after_sort_and_resend_keeps_token(self):
        worker = self.worker([registration(), ticket()])
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "DUPLICATE")
        worker.retry("P-test", resend=True)
        worker.run_once()
        self.assertEqual(self.sheet.data[1]["ticket_token"], "a" * 43)
        self.assertEqual(self.sheet.data[1]["state"], "SENT")

    def test_email_disabled_and_invalid_form_data(self):
        self.s.email_enabled = False
        worker = self.worker([registration(), registration(email="bad-address")])
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "READY")
        self.assertEqual(self.sheet.data[1]["state"], "INVALID")
        self.assertFalse(self.mailer.calls)

    def test_explicit_review_retry_keeps_original_ticket(self):
        worker = self.worker()
        self.mailer.failure = EmailAmbiguous
        worker.run_once()
        row = self.sheet.data[0]
        token = row["ticket_token"]
        worker.retry(row["participant_id"], resolution="retry")
        worker.run_once()
        self.assertEqual(row["state"], "SENT")
        self.assertEqual(row["ticket_token"], token)
        self.assertEqual(len(self.renderer.calls), 1)

    def test_conservative_batch_limit_leaves_other_rows_pending(self):
        self.s.worker_batch_size = 1
        worker = self.worker([registration(), registration(email="other@example.test")])
        worker.run_once()
        self.assertEqual(self.sheet.data[0]["state"], "SENT")
        self.assertEqual(self.sheet.data[1]["state"], "PENDING")

    def test_another_local_worker_is_refused_before_work(self):
        from event_checkin.locking import file_lock
        worker = self.worker()
        with file_lock(self.s.runtime / "worker.lock"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(worker.run_once)
                with self.assertRaises(RuntimeError):
                    result.result()
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.mailer.calls)

    def test_cancel_preserves_identity_attendance_and_blocks_new_admissions(self):
        worker = self.worker([ticket(checked_in_at="2026-10-18T12:00:00+08:00",
                                     checked_in_by="Entrance A", checkin_request_id=str(uuid.uuid4()))])
        before = deepcopy(self.sheet.data[0])
        worker.cancel("P-test")
        row = self.sheet.data[0]
        for key, value in before.items():
            self.assertEqual(row[key], value)
        self.assertEqual(row["ticket_status"], "CANCELLED")
        service = CheckinService(self.s, self.sheet)
        self.assertEqual(service.check_in("a" * 43, str(uuid.uuid4()))["status"], "invalid")
        with self.assertRaises(SheetIntegrityError):
            worker.retry("P-test", resend=True)
        self.assertFalse(self.mailer.calls)

    def test_cancelled_pending_job_does_not_block_batch_or_issue_replacement(self):
        self.s.worker_batch_size = 1
        worker = self.worker([ticket(state="PENDING", ticket_status="CANCELLED"), registration()])
        self.assertEqual(worker.run_once(), 1)
        self.assertEqual(self.sheet.data[0]["ticket_status"], "CANCELLED")
        self.assertEqual(self.sheet.data[1]["state"], "DUPLICATE")
        self.assertNotIn("ticket_token", self.sheet.data[1])
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.mailer.calls)

    def test_cancelled_email_review_cannot_be_requeued(self):
        worker = self.worker([ticket(state="EMAIL_REVIEW", email_status="AMBIGUOUS")])
        worker.cancel("P-test")
        with self.assertRaises(SheetIntegrityError):
            worker.retry("P-test", resolution="retry")
        self.assertEqual(self.sheet.data[0]["state"], "EMAIL_REVIEW")
        self.assertFalse(self.mailer.calls)


class ScannerTests(Base):
    def test_visible_qr_suppressed_until_absent(self):
        gate = VisibilityGate()
        self.assertEqual(gate.update({"a"}, 0), ["a"])
        self.assertEqual(gate.update({"a"}, 20), [])
        gate.update(set(), 21)
        self.assertEqual(gate.update({"a"}, 21.5), [])
        gate.update(set(), 24)
        self.assertEqual(gate.update({"a"}, 25), ["a"])

    def test_network_failure_persists_same_request_until_confirmed(self):
        import requests
        class Session:
            calls = []
            fail = True
            def post(inner, *args, **kwargs):
                inner.calls.append(deepcopy(kwargs))
                if inner.fail:
                    raise requests.Timeout()
                class Response:
                    status_code = 200
                    def json(_):
                        return {"status": "accepted", "checked_in_at": "2026-01-01T00:00:00+08:00", "request_id": kwargs["json"]["request_id"]}
                return Response()
        session = Session()
        client = ScanClient(self.s, session)
        self.assertTrue(client.begin("a" * 43))
        self.assertEqual(client.submit()["status"], "connection_error")
        self.assertFalse(client.begin("b" * 43))
        restarted = ScanClient(self.s, session)
        self.s.station_name = "Changed station after restart"
        session.fail = False
        self.assertEqual(restarted.submit()["status"], "accepted")
        self.assertEqual(session.calls[0]["json"], session.calls[1]["json"])
        self.assertEqual(session.calls[1]["json"]["station_name"], "Local staff station")
        self.assertFalse(restarted.path.exists())

    def test_error_response_never_admits(self):
        class Session:
            def post(inner, *args, **kwargs):
                class Response:
                    status_code = 503
                return Response()
        client = ScanClient(self.s, Session())
        client.begin("a" * 43)
        self.assertEqual(client.submit(), {"status": "connection_error"})
        self.assertTrue(client.pending)

    def test_malformed_success_response_never_admits(self):
        class Session:
            def post(inner, *args, **kwargs):
                class Response:
                    status_code = 200
                    def json(_):
                        return ["unexpected response"]
                return Response()
        client = ScanClient(self.s, Session())
        client.begin("a" * 43)
        self.assertEqual(client.submit(), {"status": "connection_error"})
        self.assertTrue(client.pending)


class EndpointTests(Base):
    def test_staff_auth_validation_and_connection_errors(self):
        from event_checkin.server import create_app
        sheet = FakeSheet([ticket()])
        app = create_app(self.s, sheet)
        async def post(body, key=None):
            sent, requested = [], False
            async def receive():
                nonlocal requested
                if not requested:
                    requested = True
                    return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
                return {"type": "http.disconnect"}
            async def send(message):
                sent.append(message)
            headers = [(b"content-type", b"application/json")]
            if key:
                headers.append((b"x-staff-key", key.encode()))
            await app({"type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
                       "path": "/check-in", "raw_path": b"/check-in", "query_string": b"", "headers": headers,
                       "client": ("127.0.0.1", 123), "server": ("127.0.0.1", 8000), "root_path": ""}, receive, send)
            status = next(m["status"] for m in sent if m["type"] == "http.response.start")
            payload = json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))
            return status, payload
        async def scenario():
            async with app.router.lifespan_context(app):
                body = {"token": "a" * 43, "request_id": str(uuid.uuid4()), "station_name": "Entrance A"}
                self.assertEqual((await post(body))[0], 401)
                self.assertEqual((await post(body, "wrong"))[0], 401)
                self.assertEqual((await post({"token": "short", "request_id": "bad"}, self.s.staff_key))[0], 422)
                for station in ("", " ", "x" * 81, "bad\nstation", "\x00", "Station\x7f"):
                    self.assertEqual((await post({**body, "station_name": station}, self.s.staff_key))[0], 422)
                status, result = await post(body, self.s.staff_key)
                self.assertEqual((status, result["status"]), (200, "accepted"))
                self.assertEqual(sheet.data[0]["checked_in_by"], "Entrance A")
                self.assertEqual(await post(body, self.s.staff_key), (status, result))
                body["token"] = "b" * 43
                self.assertEqual((await post(body, self.s.staff_key))[0], 409)
                with patch.object(sheet, "rows", side_effect=ConnectionError("fake offline")):
                    self.assertEqual((await post({"token": "a" * 43, "request_id": str(uuid.uuid4())}, self.s.staff_key))[0], 503)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
