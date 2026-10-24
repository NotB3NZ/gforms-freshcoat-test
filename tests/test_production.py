import io
import itertools
import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from event_checkin.config import Settings, ROOT, ConfigurationError
from event_checkin.production import BatchProcessor, preview, approved, target
from event_checkin.production_sheets import (
    APPROVAL_HEADERS, APPROVAL_COLUMNS, PRODUCTION_METADATA, ProductionSheets,
    ProductionTable, FixtureSheets, source_mapping, column_number,
)
from event_checkin.sheets import SheetIntegrityError
from event_checkin.mailer import EmailNotSent, EmailAmbiguous
from tests.fakes import FakeMailer


def record(**changes):
    data = dict(name="Synthetic Participant", email="synthetic@example.test", tier="Regular", _price_category="regular")
    data.update({key: "TRUE" for key in APPROVAL_HEADERS})
    data.update(changes)
    return data


class Sheet(FixtureSheets):
    def __init__(self, regular=None, discount_15=None, discount_10=None):
        super().__init__({"synthetic": True, "fixture_id": "tests", "sources": {
            "regular": regular or [], "discount_15": discount_15 or [], "discount_10": discount_10 or []}})
        self.attempts, self.fail_before, self.fail_after, self.unavailable = [], None, None, False

    def rows(self):
        if self.unavailable:
            raise SheetIntegrityError("A source is unavailable")
        return super().rows()

    def update(self, row, changes):
        self.attempts.append(deepcopy(changes))
        if self.fail_before and self.fail_before(changes):
            self.fail_before = None
            raise ConnectionError("before commit")
        super().update(row, changes)
        if self.fail_after and self.fail_after(changes):
            self.fail_after = None
            raise ConnectionError("after commit")


class Renderer:
    def __init__(self, settings):
        self.s, self.calls, self.hook, self.fail = settings, [], None, False

    def render(self, pid, name, token, ticket_tier=None):
        self.calls.append((pid, name, token, ticket_tier))
        if self.hook:
            self.hook()
        if self.fail:
            self.fail = False
            raise RuntimeError("render failed")
        path = self.s.generated / pid / "card.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        return path


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.s = Settings(profile="production", event_id="synthetic-event", root=root,
                          runtime=root / "runtime-production", generated=root / "generated-production", email_enabled=True)
        # Fake SMTP only: real Settings.require_email retains its production hard lock.
        self.email_check = patch.object(self.s, "require_email")
        self.email_check.start()
        self.renderer, self.mailer = Renderer(self.s), FakeMailer()

    def tearDown(self):
        self.email_check.stop()
        self.temp.cleanup()

    def plan(self, sheet, **kwargs):
        return preview(self.s, sheet, **(kwargs or {"all_sources": True}))[1]

    def execute(self, sheet, plan):
        return BatchProcessor(self.s, sheet, self.renderer, self.mailer).execute(plan)

    def test_email_receives_same_frozen_name_recipient_tier_and_card_as_render(self):
        for tier in ("Lite", "Regular", "Pro"):
            sheet = Sheet(regular=[record(tier=tier, email=tier.lower() + "@example.invalid")])
            self.execute(sheet, self.plan(sheet))
            row = sheet.data["sources"]["regular"][0]
            args = self.mailer.calls[-1]
            self.assertEqual(args[:3], (row["normalized_email"], row["ticket_name"], row["participant_id"]))
            self.assertEqual(str(args[3]), row["card_path"])
            self.assertEqual(args[4], row["message_id"])
            self.assertEqual(self.mailer.tiers[-1], row["ticket_tier"])
            self.assertEqual(self.mailer.tiers[-1], self.renderer.calls[-1][-1])

    def test_all_16_approval_combinations_only_all_true_issues(self):
        for index, combo in enumerate(itertools.product(("TRUE", "FALSE"), repeat=4)):
            with self.subTest(combo=combo):
                sheet = Sheet([record(email="combination%d@example.test" % index, **dict(zip(APPROVAL_HEADERS, combo)))])
                self.execute(sheet, self.plan(sheet))
                row = sheet.data["sources"]["regular"][0]
                if all(value == "TRUE" for value in combo):
                    self.assertEqual(row["state"], "SENT")
                    self.assertTrue(row["ticket_token"])
                else:
                    self.assertEqual(row["state"], "WAITING_APPROVAL")
                    self.assertTrue(row["record_id"])
                    self.assertNotIn("ticket_token", row)
                    self.assertNotIn("participant_id", row)
        self.assertEqual(len(self.mailer.calls), 1)

    def test_strict_approval_values(self):
        for value in (None, "", "FALSE", "false", False, 0, 1, "yes", "checked", " true ", "True", [], {}):
            with self.subTest(value=value):
                self.assertFalse(approved(value))
                sheet = Sheet([record(confirmation=value)])
                self.execute(sheet, self.plan(sheet))
                self.assertNotIn("ticket_token", sheet.data["sources"]["regular"][0])
        self.assertTrue(approved(True))
        self.assertTrue(approved("TRUE"))
        sheet = Sheet([record()])
        del sheet.data["sources"]["regular"][0]["confirmation"]
        self.execute(sheet, self.plan(sheet))
        self.assertFalse(self.mailer.calls)

    def test_later_approvals_do_not_expand_saved_selection(self):
        sheet = Sheet([record(confirmation="FALSE")])
        old = self.plan(sheet)
        sheet.data["sources"]["regular"][0]["confirmation"] = "TRUE"
        self.execute(sheet, old)
        self.assertFalse(self.mailer.calls)
        self.execute(sheet, self.plan(sheet))
        self.assertEqual(len(self.mailer.calls), 1)

    def test_waiting_rows_do_not_starve_later_source_or_consume_limit(self):
        sheet = Sheet([record(email="wait%d@example.test" % i, confirmation=False) for i in range(20)],
                      [record(email="eligible@example.test", _price_category="15_percent_discount")])
        plan = self.plan(sheet, all_sources=True, limit=1)
        self.assertEqual(sum(e["process"] for e in plan["entries"]), 1)
        self.execute(sheet, plan)
        self.assertEqual(self.mailer.calls[0][0], "eligible@example.test")
        self.assertTrue(all(not r.get("ticket_token") for r in sheet.data["sources"]["regular"]))

    def test_no_preview_writes_and_private_details(self):
        sheet = Sheet([record()])
        report, _ = preview(self.s, sheet, all_sources=True)
        self.assertEqual(report[0]["email"], "synthetic@example.test")
        self.assertTrue(report[0]["in_batch"])
        self.assertFalse(sheet.attempts)
        self.assertFalse(self.s.runtime.exists())

    def test_exact_id_list_and_source_skip_unselected(self):
        sheet = Sheet([record(record_id="R-a", email="a@example.test"), record(record_id="R-b", email="b@example.test")],
                      [record(record_id="R-c", email="c@example.test")], [record(record_id="R-d", email="d@example.test")])
        plan = self.plan(sheet, ids=["regular:R-b", "R-c"])
        self.execute(sheet, plan)
        self.assertEqual([call[0] for call in self.mailer.calls], ["b@example.test", "c@example.test"])
        self.assertNotIn("state", sheet.data["sources"]["regular"][0])
        self.assertNotIn("state", sheet.data["sources"]["discount_10"][0])
        self.execute(sheet, self.plan(sheet, source="discount_10"))
        self.assertEqual(self.mailer.calls[-1][0], "d@example.test")

    def test_target_before_issuance_record_id_after_waiting_and_sort(self):
        sheet = Sheet([record(email="wait@example.test", confirmation=False), record(email="other@example.test")])
        self.execute(sheet, self.plan(sheet, ids=["regular:row:2"]))
        waiting = sheet.data["sources"]["regular"][0]
        rid = waiting["record_id"]
        self.assertNotIn("ticket_token", waiting)
        waiting["confirmation"] = True
        plan = self.plan(sheet, ids=["regular:" + rid])
        sheet.data["sources"]["regular"].reverse()
        self.execute(sheet, plan)
        self.assertEqual([c[0] for c in self.mailer.calls], ["wait@example.test"])

    def test_ambiguous_missing_repeated_targets_and_selectors_fail(self):
        sheet = Sheet([record(record_id="R-a")])
        for selection in ({"ids": ["missing"]}, {"ids": ["R-a", "regular:R-a"]},
                          {"ids": ["R-a"], "all_sources": True}, {"source": "wrong"}):
            with self.subTest(selection=selection), self.assertRaises(SheetIntegrityError):
                self.plan(sheet, **selection)
        other = Sheet([record(record_id="R-a")], [record(record_id="R-a")])
        with self.assertRaises(SheetIntegrityError):
            self.plan(other)

    def test_temporary_row_locator_invalidated_by_reordering(self):
        sheet = Sheet([record(email="first@example.test"), record(email="second@example.test")])
        plan = self.plan(sheet, ids=["regular:row:2"])
        sheet.data["sources"]["regular"].reverse()
        with self.assertRaises(SheetIntegrityError):
            self.execute(sheet, plan)
        self.assertFalse(sheet.attempts)
        self.assertFalse(self.mailer.calls)

    def test_approval_revoke_between_selection_and_execution(self):
        sheet = Sheet([record()])
        plan = self.plan(sheet)
        sheet.data["sources"]["regular"][0]["payment_received"] = "FALSE"
        self.execute(sheet, plan)
        self.assertFalse(self.mailer.calls)
        self.assertNotIn("ticket_token", sheet.data["sources"]["regular"][0])

    def test_recipient_name_tier_change_between_selection_execution(self):
        for key, value in (("email", "changed@example.test"), ("name", "Changed"), ("tier", "Pro")):
            with self.subTest(key=key):
                sheet = Sheet([record()])
                plan = self.plan(sheet)
                sheet.data["sources"]["regular"][0][key] = value
                with self.assertRaises(SheetIntegrityError):
                    self.execute(sheet, plan)
                self.assertFalse(self.mailer.calls)
                self.assertFalse(sheet.attempts)

    def test_recheck_approvals_after_render_and_after_sending_intent(self):
        for phase in ("render", "intent"):
            with self.subTest(phase=phase):
                sheet = Sheet([record(email=phase + "@example.test")])
                def revoke():
                    sheet.data["sources"]["regular"][0]["confirmation"] = "FALSE"
                if phase == "render":
                    self.renderer.hook = revoke
                else:
                    original = sheet.update
                    def update(row, changes):
                        original(row, changes)
                        if changes.get("email_status") == "SENDING":
                            revoke()
                    sheet.update = update
                self.execute(sheet, self.plan(sheet))
                self.assertFalse(self.mailer.calls)
                self.renderer.hook = None

    def test_recipient_or_tier_changed_during_render_never_sent(self):
        for key, value in (("email", "changed@example.test"), ("tier", "Pro")):
            sheet = Sheet([record(email=key + "@example.test")])
            self.renderer.hook = lambda: sheet.data["sources"]["regular"][0].update({key: value})
            with self.assertRaises(SheetIntegrityError):
                self.execute(sheet, self.plan(sheet))
            self.assertFalse(self.mailer.calls)

    def test_unknown_tier_is_reviewed_without_normalizing(self):
        for tier in ("regular", "Regular ticket", "Pro - discounted", "", " Regular "):
            sheet = Sheet([record(tier=tier)])
            report, plan = preview(self.s, sheet, all_sources=True)
            self.assertIn("unknown raw tier", str(report))
            self.execute(sheet, plan)
            self.assertFalse(self.mailer.calls)

    def test_unapproved_earlier_duplicate_does_not_block_approved_later(self):
        sheet = Sheet([record(confirmation=False)], [record()])
        self.execute(sheet, self.plan(sheet))
        self.assertEqual(len(self.mailer.calls), 1)
        self.assertNotIn("ticket_token", sheet.data["sources"]["regular"][0])
        self.assertTrue(sheet.data["sources"]["discount_15"][0]["ticket_token"])

    def test_conflicting_approved_cross_source_details_require_review(self):
        for changes in ({"tier": "Pro"}, {"name": "Someone Else"}, {"_price_category": "15_percent_discount"}):
            sheet = Sheet([record()], [record(**changes)])
            report, plan = preview(self.s, sheet, all_sources=True)
            self.assertTrue(all("conflicting approved" in str(r["blocking_reasons"]) for r in report))
            self.execute(sheet, plan)
            self.assertFalse(self.mailer.calls)

    def test_equivalent_approved_duplicates_issue_only_one(self):
        sheet = Sheet([record()], [record()])
        self.execute(sheet, self.plan(sheet))
        self.assertEqual(len(self.mailer.calls), 1)
        self.assertNotIn("ticket_token", sheet.data["sources"]["discount_15"][0])

    def test_existing_issued_ticket_wins_even_when_later_registration_approved(self):
        sheet = Sheet([record()])
        self.execute(sheet, self.plan(sheet))
        original = deepcopy(sheet.data["sources"]["regular"][0])
        sheet.data["sources"]["discount_10"].append(record(tier="Pro"))
        report, plan = preview(self.s, sheet, source="discount_10")
        self.assertIn("issued canonical", str(report))
        self.execute(sheet, plan)
        self.assertEqual(len(self.mailer.calls), 1)
        self.assertEqual(original, sheet.data["sources"]["regular"][0])

    def test_approved_conflict_added_during_render_blocks_send_without_second_ticket(self):
        sheet = Sheet([record()])
        self.renderer.hook = lambda: sheet.data["sources"]["discount_15"].append(record(tier="Pro"))
        self.execute(sheet, self.plan(sheet))
        self.assertEqual(len(self.mailer.calls), 0)
        self.assertEqual(sheet.data["sources"]["regular"][0]["state"], "REGISTRATION_REVIEW")
        self.assertNotIn("ticket_token", sheet.data["sources"]["discount_15"][0])

    def test_batch_is_finite_and_sent_skipped_on_repeat(self):
        sheet = Sheet([record(email="one@example.test"), record(email="two@example.test")])
        plan = self.plan(sheet, all_sources=True, limit=1)
        self.renderer.hook = lambda: sheet.data["sources"]["regular"].append(record(email="new@example.test"))
        self.execute(sheet, plan)
        self.execute(sheet, plan)
        self.assertEqual([c[0] for c in self.mailer.calls], ["one@example.test"])
        self.assertNotIn("state", sheet.data["sources"]["regular"][1])
        rid = sheet.data["sources"]["regular"][0]["record_id"]
        self.assertFalse(preview(self.s, sheet, ids=[rid])[0][0]["in_batch"])

    def test_render_failure_explicit_retry_and_regeneration_keep_name_tier_token(self):
        sheet = Sheet([record(tier="Pro")])
        self.renderer.fail = True
        self.execute(sheet, self.plan(sheet))
        row = sheet.data["sources"]["regular"][0]
        identity = row["participant_id"], row["ticket_token"], row["record_id"]
        self.execute(sheet, self.plan(sheet, ids=[row["record_id"]], action="retry", regenerate=True))
        self.assertEqual(identity, (row["participant_id"], row["ticket_token"], row["record_id"]))
        self.assertTrue(all(c[1] == "Synthetic Participant" and c[3] == "Pro" for c in self.renderer.calls))
        self.assertEqual(len(self.mailer.calls), 1)

    def test_explicit_resend_is_exact_gated_and_same_plan_never_resends_twice(self):
        sheet = Sheet([record(email="one@example.test"), record(email="two@example.test")])
        self.execute(sheet, self.plan(sheet))
        first = sheet.data["sources"]["regular"][0]
        identity = first["participant_id"], first["ticket_token"]
        plan = self.plan(sheet, ids=[first["record_id"]], action="resend", regenerate=True)
        self.execute(sheet, plan)
        self.execute(sheet, plan)
        self.assertEqual([c[0] for c in self.mailer.calls], ["one@example.test", "two@example.test", "one@example.test"])
        self.assertEqual(identity, (first["participant_id"], first["ticket_token"]))
        plan = self.plan(sheet, ids=[first["record_id"]], action="resend")
        first["reference_matched"] = False
        self.execute(sheet, plan)
        self.assertEqual(len(self.mailer.calls), 3)

    def test_stale_resend_plan_cannot_send_after_different_resend(self):
        sheet = Sheet([record()])
        self.execute(sheet, self.plan(sheet))
        first, stale = self.plan(sheet, all_sources=True, action="resend"), self.plan(sheet, all_sources=True, action="resend")
        self.execute(sheet, first)
        self.execute(sheet, stale)
        self.assertEqual(len(self.mailer.calls), 2)

    def test_email_failure_retry_reuses_identity_card_and_message_id(self):
        sheet = Sheet([record()])
        self.mailer.failure = EmailNotSent
        self.execute(sheet, self.plan(sheet))
        row = sheet.data["sources"]["regular"][0]
        before = row["participant_id"], row["ticket_token"], row["message_id"]
        plan = self.plan(sheet, all_sources=True, action="retry")
        self.execute(sheet, plan)
        self.assertEqual(before, (row["participant_id"], row["ticket_token"], row["message_id"]))
        self.assertEqual(len(self.renderer.calls), 1)
        self.assertEqual(len(self.mailer.calls), 2)

    def test_retry_gate_after_payment_unchecked(self):
        sheet = Sheet([record()])
        self.mailer.failure = EmailNotSent
        self.execute(sheet, self.plan(sheet))
        plan = self.plan(sheet, all_sources=True, action="retry")
        sheet.data["sources"]["regular"][0]["payment_received"] = False
        self.execute(sheet, plan)
        self.assertEqual(len(self.mailer.calls), 1)

    def test_ambiguous_delivery_remains_review_until_exact_resolution(self):
        sheet = Sheet([record()])
        self.mailer.failure = EmailAmbiguous
        self.execute(sheet, self.plan(sheet))
        row = sheet.data["sources"]["regular"][0]
        self.execute(sheet, self.plan(sheet, all_sources=True, action="retry"))
        self.assertEqual(len(self.mailer.calls), 1)
        service = BatchProcessor(self.s, sheet, self.renderer, self.mailer)
        row["confirmation"] = False
        with self.assertRaises(SheetIntegrityError):
            service.resolve_email(row["record_id"], "retry")
        row["confirmation"] = True
        service.resolve_email(row["record_id"], "retry")
        self.execute(sheet, self.plan(sheet, ids=[row["record_id"]], action="retry"))
        self.assertEqual(len(self.mailer.calls), 2)

    def test_lost_writes_recover_without_new_identity_or_duplicate_send(self):
        for index, (phase, after) in enumerate((("record_id", False), ("ticket_token", False), ("ticket_token", True), ("SENT", False), ("SENT", True))):
            with self.subTest(phase=phase, after=after):
                sheet = Sheet([record(email="lost%d@example.test" % index)])
                failure = lambda c: c.get("state") == "SENT" if phase == "SENT" else phase in c
                if after:
                    sheet.fail_after = failure
                else:
                    sheet.fail_before = failure
                plan = self.plan(sheet)
                before = len(self.mailer.calls)
                with self.assertRaises(ConnectionError):
                    self.execute(sheet, plan)
                self.execute(sheet, plan)
                self.assertLessEqual(len(self.mailer.calls) - before, 1)
                attempted = [c for c in sheet.attempts if "ticket_token" in c]
                self.assertLessEqual(len({c["ticket_token"] for c in attempted}), 1)
                self.assertLessEqual(len({c["participant_id"] for c in attempted}), 1)
                if phase == "SENT" and not after:
                    self.assertEqual(sheet.data["sources"]["regular"][0]["state"], "EMAIL_REVIEW")

    def test_missing_source_access_stops_preview_and_execution_before_writes(self):
        sheet = Sheet([record()])
        plan = self.plan(sheet)
        sheet.unavailable = True
        with self.assertRaises(SheetIntegrityError):
            self.plan(sheet, ids=["regular:row:2"])
        with self.assertRaises(SheetIntegrityError):
            self.execute(sheet, plan)
        self.assertFalse(sheet.attempts)
        self.assertFalse(self.mailer.calls)

    def test_wrong_event_mode_dataset_plan_and_edited_plan_fail(self):
        sheet = Sheet([record()])
        plan = self.plan(sheet)
        for key, value in (("event_id", "wrong"), ("live", True), ("dataset", "different"), ("profile", "test")):
            bad = deepcopy(plan)
            bad[key] = value
            with self.assertRaises(SheetIntegrityError):
                self.execute(sheet, bad)
        self.execute(sheet, plan)
        plan["regenerate"] = True
        with self.assertRaises(SheetIntegrityError):
            self.execute(sheet, plan)

    def test_cancelled_issued_and_frozen_data_changes_block_even_resends(self):
        sheet = Sheet([record()])
        self.execute(sheet, self.plan(sheet))
        row = sheet.data["sources"]["regular"][0]
        for key, value in (("ticket_status", "CANCELLED"), ("tier", "Pro"), ("email", "changed@example.test")):
            old = row.get(key)
            row[key] = value
            report, plan = preview(self.s, sheet, all_sources=True, action="resend")
            self.assertFalse(report[0]["in_batch"])
            self.execute(sheet, plan)
            row[key] = old
        self.assertEqual(len(self.mailer.calls), 1)

    def test_metadata_operations_never_change_original_fields_or_approvals(self):
        sheet = Sheet([record(confirmation=False)], [record(email="other@example.test")])
        before = deepcopy(sheet.data)
        self.execute(sheet, self.plan(sheet))
        for source, values in before["sources"].items():
            for old, new in zip(values, sheet.data["sources"][source]):
                for key, value in old.items():
                    self.assertEqual(new[key], value)
        self.assertTrue(all(set(c) <= set(PRODUCTION_METADATA) for c in sheet.attempts))


    def test_recipient_or_tier_change_after_email_intent_never_sends(self):
        for key, value in (("email", "changed@example.test"), ("tier", "Pro")):
            sheet = Sheet([record(email=key + "@example.test")])
            original = sheet.update
            def update(row, changes):
                original(row, changes)
                if changes.get("email_status") == "SENDING":
                    sheet.data["sources"]["regular"][0][key] = value
            sheet.update = update
            with self.assertRaises(SheetIntegrityError):
                self.execute(sheet, self.plan(sheet))
            self.assertFalse(self.mailer.calls)

    def test_durable_claim_blocks_changed_details_after_failed_issuance_write(self):
        sheet = Sheet([record()])
        sheet.fail_before = lambda changes: "ticket_token" in changes
        with self.assertRaises(ConnectionError):
            self.execute(sheet, self.plan(sheet))
        row = sheet.data["sources"]["regular"][0]
        row["tier"] = "Pro"
        with self.assertRaisesRegex(SheetIntegrityError, "reservation conflicts"):
            self.execute(sheet, self.plan(sheet))
        self.assertFalse(self.renderer.calls)
        self.assertFalse(self.mailer.calls)

    def test_event_changed_after_preview_does_not_overwrite_other_event(self):
        sheet = Sheet([record()])
        plan = self.plan(sheet)
        sheet.data["sources"]["regular"][0]["event_id"] = "other-event"
        self.execute(sheet, plan)
        self.assertFalse(sheet.attempts)
        self.assertFalse(self.mailer.calls)

    def test_identity_or_review_state_changed_during_processing_stops_delivery(self):
        sheet = Sheet([record()])
        self.renderer.hook = lambda: sheet.data["sources"]["regular"][0].update(ticket_token="z" * 43)
        with self.assertRaises(SheetIntegrityError):
            self.execute(sheet, self.plan(sheet))
        self.assertFalse(self.mailer.calls)

        self.renderer.hook = None
        sheet = Sheet([record(email="review@example.test")])
        original = sheet.update
        def update(row, changes):
            original(row, changes)
            if changes.get("email_status") == "SENDING":
                sheet.data["sources"]["regular"][0]["state"] = "EMAIL_REVIEW"
        sheet.update = update
        with self.assertRaises(SheetIntegrityError):
            self.execute(sheet, self.plan(sheet))
        self.assertFalse(self.mailer.calls)

    def test_interrupted_send_can_be_resolved_directly_without_replaying_old_plan(self):
        sheet = Sheet([record()])
        sheet.fail_before = lambda c: c.get("state") == "SENT"
        with self.assertRaises(ConnectionError):
            self.execute(sheet, self.plan(sheet))
        row = sheet.data["sources"]["regular"][0]
        self.assertEqual((row["state"], row["email_status"]), ("PROCESSING", "SENDING"))
        service = BatchProcessor(self.s, sheet, self.renderer, self.mailer)
        service.resolve_email(row["record_id"], "sent")
        self.assertEqual(row["state"], "SENT")
        self.assertEqual(len(self.mailer.calls), 1)


class Worksheet:
    def __init__(self, header, records=None):
        self.values = [deepcopy(header)] + deepcopy(records or [])
        self.col_count = len(header)
        self.row_count = max(10, len(self.values))
        self.title = "unused"
        self.calls = []

    def row_values(self, number):
        self.calls.append(("row_values", number))
        return deepcopy(self.values[number - 1])

    def add_cols(self, count):
        self.calls.append(("add_cols", count))
        self.col_count += count

    def update(self, range_name, values, value_input_option):
        self.calls.append(("update", range_name, deepcopy(values), value_input_option))
        self.values[0].extend(values[0])

    def batch_get(self, ranges, value_render_option):
        import re
        self.calls.append(("batch_get", list(ranges), value_render_option))
        columns = [column_number(re.match(r"([A-Z]+)", span)[1]) - 1 for span in ranges]
        return [[[row[col] if col < len(row) else ""] for row in self.values[1:]] for col in columns]

    def batch_update(self, updates, value_input_option):
        import re
        self.calls.append(("batch_update", deepcopy(updates), value_input_option))
        for update in updates:
            match = re.fullmatch(r"([A-Z]+)([0-9]+)", update["range"])
            col, row = column_number(match[1]) - 1, int(match[2]) - 1
            self.values[row] += [""] * (col + 1 - len(self.values[row]))
            self.values[row][col] = update["values"][0][0]


class ProductionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.sources = json.loads((ROOT / "config/sources.production.example.json").read_text())["sources"]

    def table(self, source, tracking=False):
        header = ["Original field %d" % index for index in range(23 if source["id"] == "regular" else 21)]
        for label, col in source_mapping(source).values():
            header[column_number(col) - 1] = label
        if tracking:
            header += list(PRODUCTION_METADATA)
        row = [""] * len(header)
        row[2], row[3] = "synthetic@example.test", "Synthetic Participant"
        row[column_number(source["fields"]["tier"]["column"]) - 1] = "Pro"
        for col in APPROVAL_COLUMNS[source["id"]]:
            row[column_number(col) - 1] = True
        return ProductionTable(Settings(), source, Worksheet(header, [row]))

    def test_all_three_mappings_tiers_and_boolean_approvals(self):
        for source in self.sources:
            with self.subTest(source=source["id"]):
                table = self.table(source)
                rows = table.rows()
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row.source_id, source["id"])
                self.assertEqual(row.get("tier"), "Pro")
                self.assertEqual(row.get("_price_category"), source["price_category"])
                self.assertTrue(all(row.get(key) is True for key in APPROVAL_HEADERS))
                self.assertTrue(all(call[0] not in ("update", "batch_update", "add_cols") for call in table.ws.calls))
                batch = next(call for call in table.ws.calls if call[0] == "batch_get")
                self.assertEqual(batch[2], "UNFORMATTED_VALUE")
                self.assertEqual(len(batch[1]), 7)  # Read only 3 form fields and 4 approvals.

    def test_missing_moved_duplicate_approval_header_fails_before_participant_read(self):
        for kind in ("missing", "moved", "duplicate", "misspelling"):
            table = self.table(self.sources[0])
            if kind == "missing":
                table.ws.values[0][22] = "Renamed"
            elif kind == "moved":
                table.ws.values[0][20], table.ws.values[0][21] = table.ws.values[0][21], table.ws.values[0][20]
            elif kind == "duplicate":
                table.ws.values[0].append("Confirmation")
            else:
                table.ws.values[0][22] = "Confirm if payment is received in our account"
            with self.subTest(kind=kind), self.assertRaises(SheetIntegrityError):
                table.rows()
            self.assertFalse(any(call[0] == "batch_get" for call in table.ws.calls))

    def test_header_change_during_read_fails_closed(self):
        table = self.table(self.sources[0])
        original = table.ws.batch_get
        def batch_get(*args, **kwargs):
            result = original(*args, **kwargs)
            table.ws.values[0][0] = "Changed"
            return result
        table.ws.batch_get = batch_get
        with self.assertRaises(SheetIntegrityError):
            table.rows()

    def test_local_initialization_appends_24_only_missing_no_response_writes(self):
        for source in self.sources:
            table = self.table(source)
            original = deepcopy(table.ws.values)
            self.assertEqual(table.schema_plan()["append"], list(PRODUCTION_METADATA))
            table.initialize()
            table.initialize()
            self.assertEqual(table.ws.values[1:], original[1:])
            self.assertEqual(table.ws.values[0][:len(original[0])], original[0])
            self.assertEqual(len([call for call in table.ws.calls if call[0] == "update"]), 1)
            self.assertEqual(table.ws.values[0][len(original[0]):], list(PRODUCTION_METADATA))
            self.assertEqual(next(c for c in table.ws.calls if c[0] == "update")[-1], "RAW")

    def test_partial_schema_only_appends_missing(self):
        table = self.table(self.sources[0])
        table.ws.values[0] += ["record_id", "ticket_tier"]
        plan = table.schema_plan()
        self.assertNotIn("record_id", plan["append"])
        self.assertNotIn("ticket_tier", plan["append"])
        table.initialize()
        self.assertEqual(table.schema_plan()["append"], [])

    def test_metadata_updates_raw_never_form_staff_or_attendance_cells(self):
        table = self.table(self.sources[0], tracking=True)
        original = deepcopy(table.ws.values[1])
        row = table.rows(require_metadata=True)[0]
        table.update(row, {"record_id": "R-synthetic", "state": "WAITING_APPROVAL", "source_id": "regular"})
        protected = [column_number(col) - 1 for _, col in table.mapping.values()]
        protected += [table.ws.values[0].index(key) for key in ("checked_in_at", "checked_in_by", "checkin_request_id")]
        self.assertTrue(all(table.ws.values[1][index] == original[index] for index in protected))
        self.assertEqual(table.ws.calls[-1][-1], "RAW")
        for key in (*APPROVAL_HEADERS, "name", "email", "tier", "Confirmation"):
            with self.assertRaises(SheetIntegrityError):
                table.update(row, {key: "TRUE"})

    def test_changed_row_rejected_before_metadata_write(self):
        table = self.table(self.sources[0], tracking=True)
        row = table.rows()[0]
        table.ws.values[1][2] = "changed@example.test"
        with self.assertRaises(SheetIntegrityError):
            table.update(row, {"record_id": "R-synthetic"})
        self.assertFalse(any(c[0] == "batch_update" for c in table.ws.calls))

    def test_live_initialization_is_hard_locked_even_with_reviewed_headers(self):
        table = self.table(self.sources[0])
        table.settings = Settings(profile="production")
        with self.assertRaises(ConfigurationError):
            table.initialize()
        self.assertEqual(table.ws.calls, [])

    def test_missing_source_access_aborts_constructor_without_reading_rows(self):
        s = Settings(profile="production", sources_file=ROOT / "config/sources.production.example.json")
        with patch("event_checkin.production_sheets.production_sources", return_value=self.sources), \
                patch("gspread.service_account") as auth, patch("event_checkin.production_sheets.time.sleep"):
            client = auth.return_value
            book = unittest.mock.Mock()
            book.get_worksheet_by_id.return_value.title = self.sources[0]["tab_name"]
            client.open_by_key.side_effect = [book, ConnectionError("private provider message")]
            with self.assertRaisesRegex(SheetIntegrityError, "every production source"):
                ProductionSheets(s)
            book.get_worksheet_by_id.return_value.batch_get.assert_not_called()
            auth.assert_called_once()
            self.assertEqual(auth.call_args.kwargs["scopes"], ["https://www.googleapis.com/auth/spreadsheets.readonly"])

    def test_all_source_schemas_validated_before_initialization_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = ProductionSheets.__new__(ProductionSheets)
            adapter.settings = Settings(runtime=Path(temp))
            adapter.writable = True
            first, second = self.table(self.sources[0]), self.table(self.sources[1])
            second.ws.values[0][0] = "Missing approval"
            adapter.tables = {"regular": first, "discount_15": second}
            with self.assertRaises(SheetIntegrityError):
                adapter.initialize()
            self.assertFalse(any(c[0] in ("update", "add_cols", "batch_update") for c in first.ws.calls))


class ProductionCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.s = Settings(root=root, profile="production", event_id="fixture-event", runtime=root / "runtime-production",
                          generated=root / "generated-production")

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        from event_checkin.__main__ import main
        with patch("sys.argv", ["event_checkin", "--profile", "production", *map(str, args)]), \
                patch("event_checkin.__main__.load_settings", return_value=self.s), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            result = main()
        return result, out.getvalue(), err.getvalue()

    def test_fixture_preview_and_bounded_execution_never_build_google_or_smtp(self):
        fixture = ROOT / "examples/production-registrations.json"
        plan = self.s.runtime / "offline/plan.json"
        state = self.s.runtime / "offline/fixture-state.json"
        class FakeCardRenderer(Renderer):
            pass
        with patch("gspread.service_account") as google, patch("smtplib.SMTP") as smtp, \
                patch("event_checkin.production_cli.CardRenderer", FakeCardRenderer):
            self.assertIsNone(self.cli("batch-preview", "--fixture", fixture, "--all", "--limit", "1", "--plan-out", plan)[0])
            result, out, err = self.cli("batch-run", "--fixture", fixture, "--plan", plan)
            self.assertIsNone(result, err)
            self.assertIn("ready", out)
            google.assert_not_called()
            smtp.assert_not_called()
            data = json.loads(state.read_text())
            waiting, ready = data["sources"]["regular"]
            self.assertEqual(waiting["state"], "WAITING_APPROVAL")
            self.assertEqual(ready["ticket_tier"], "Regular")
            self.assertEqual(ready["email_status"], "DISABLED")
            self.assertNotIn("ticket_token", data["sources"]["discount_15"][0])
            self.assertEqual(plan.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(self.cli("batch-run", "--fixture", state, "--plan", plan)[0])
            # The initial fixture cannot silently replace a saved checkpoint.
            self.assertEqual(self.cli("batch-run", "--fixture", fixture, "--plan", plan)[0], 1)
            # A separate demonstration gets its own journal, even with the same fake emails.
            second_plan, second_state = plan.with_name("second-plan.json"), state.with_name("second-state.json")
            self.assertIsNone(self.cli("batch-preview", "--fixture", fixture, "--all", "--plan-out", second_plan)[0])
            self.assertIsNone(self.cli("batch-run", "--fixture", fixture, "--plan", second_plan, "--state-out", second_state)[0])
            self.assertNotEqual(data["session_id"], json.loads(second_state.read_text())["session_id"])
            google.assert_not_called()
            smtp.assert_not_called()

    def test_live_execution_and_resolution_stop_before_readers_or_side_effects(self):
        with patch("event_checkin.production_cli.ProductionSheets") as reader, \
                patch("event_checkin.production_cli.CardRenderer") as renderer:
            for command in (("batch-run", "--plan", "unused.json"),
                            ("batch-resolve-email", "R-example", "--outcome", "retry")):
                result, _, error = self.cli(*command)
                self.assertEqual(result, 1)
                self.assertIn("locked", error)
            reader.assert_not_called()
            renderer.assert_not_called()
        self.assertFalse(self.s.runtime.exists())

    def test_live_preview_is_explicit_read_only_and_can_save_plan(self):
        sheet = Sheet([record()])
        sheet.live = True
        with patch("event_checkin.production_cli.ProductionSheets", return_value=sheet), \
                patch("event_checkin.production_cli.CardRenderer") as renderer:
            result, output, _ = self.cli("batch-preview", "--live", "--all")
            self.assertIsNone(result)
            self.assertIn("synthetic@example.test", output)
            renderer.assert_not_called()
        self.assertFalse(sheet.attempts)
        self.assertFalse(self.s.runtime.exists())

    def test_header_command_never_requests_participants(self):
        with patch("event_checkin.production_cli.ProductionSheets") as reader:
            reader.return_value.schema_plan.return_value = [{"source": "regular", "append": list(PRODUCTION_METADATA)}]
            result, output, _ = self.cli("production-headers")
            self.assertIsNone(result)
            self.assertIn("record_id", output)
            reader.return_value.rows.assert_not_called()


