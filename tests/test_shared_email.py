"""Offline tests for explicitly approved shared-email attendee bookings."""
import json
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from event_checkin.config import Settings
from event_checkin.production import BatchJournal, BatchProcessor, preview, reasons
from event_checkin.shared_email import (
    apply_approved_groups, claims_schema, migrate_claims_database,
    review_members, save_approval,
)
from event_checkin.sheets import SheetIntegrityError
from event_checkin.mailer import EmailAmbiguous
from tests.fakes import FakeMailer
from tests.test_production import Renderer, Sheet, record


class SharedEmailBookingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(root=root, profile="production", event_id="shared-email-event",
                                 runtime=root / "runtime-production",
                                 generated=root / "generated-production", email_enabled=False)

    def tearDown(self):
        self.temp.cleanup()

    def pair(self, *, third=False):
        rows = [record(name="Attendee One", email="shared@example.invalid", tier="Regular"),
                record(name="Attendee Two", email="shared@example.invalid", tier="Regular")]
        if third:
            rows.append(record(name="Later Attendee", email="shared@example.invalid", tier="Regular",
                               confirmation=False, reference_matched=False,
                               price_matched=False, payment_received=False))
        return Sheet(regular=rows)

    def approve_pair(self, sheet, rows=(2, 3)):
        review, review_hash, members = review_members(self.settings, sheet.rows(), "regular", list(rows))
        group_id = save_approval(self.settings, review, review_hash,
                                  confirm_distinct_attendees=True, confirm_payment_coverage=True)
        return group_id, apply_approved_groups(self.settings, sheet.rows())

    def test_approved_pair_has_independent_ticket_claims_and_unapproved_third_is_blocked(self):
        sheet = self.pair(third=True)
        group_id, rows = self.approve_pair(sheet)
        self.assertEqual(rows[0].get("_shared_email_group_id"), group_id)
        self.assertEqual(rows[1].get("_shared_email_group_id"), group_id)
        self.assertFalse(any(reasons(rows[0], rows, self.settings.event_id)))
        self.assertFalse(any(reasons(rows[1], rows, self.settings.event_id)))

        journal = BatchJournal(self.settings)
        issued = []
        for row in rows[:2]:
            row.data["record_id"] = "R-shared-" + str(row.number)
            # Refresh the attested member IDs after the durable record IDs exist.
        rows = apply_approved_groups(self.settings, sheet.rows())
        for row in rows[:2]:
            row.data["record_id"] = "R-shared-" + str(row.number)
            row.data["_shared_email_member_record_ids"] = [r.get("record_id") or "" for r in rows[:2]]
            issued.append(journal.claim(self.settings, row))
        self.assertEqual(len({pair[0] for pair in issued}), 2)
        self.assertEqual(len({pair[1] for pair in issued}), 2)
        with journal.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM claims WHERE event_id=? AND email=?",
                                         (self.settings.event_id, "shared@example.invalid")).fetchone()[0], 2)

        third = rows[2]
        third_blockers = reasons(third, rows, self.settings.event_id)
        self.assertTrue(any("duplicate of issued canonical registration" in reason
                            or "conflicting issued tickets" in reason
                            or "conflicting approved registrations" in reason
                            for reason in third_blockers))
        third.data["record_id"] = "R-unapproved-third"
        with self.assertRaisesRegex(SheetIntegrityError, "explicit shared-email approval"):
            journal.claim(self.settings, third)

    def test_previous_ticket_plus_new_attendee_preview_selects_only_new_record(self):
        sheet = self.pair()
        first = sheet.rows()[0]
        first.data.update(record_id="R-existing", event_id=self.settings.event_id,
                          source_id="regular", price_category="regular")
        journal = BatchJournal(self.settings)
        participant_id, token = journal.claim(self.settings, first)
        sheet.data["sources"]["regular"][0].update(
            record_id="R-existing", event_id=self.settings.event_id, source_id="regular",
            price_category="regular", participant_id=participant_id, ticket_token=token,
            normalized_email="shared@example.invalid", ticket_name="Attendee One",
            ticket_tier="Regular", ticket_status="ACTIVE", state="SENT", email_status="SENT")
        self.approve_pair(sheet)

        report, plan, summary = preview(self.settings, sheet, source="regular", limit="all",
                                        selection_version=2, include_summary=True)

        self.assertEqual([r["row"] for r in report if r["in_batch"]], [3])
        self.assertEqual(len(plan["entries"]), 1)
        self.assertEqual(summary["regular"]["already_sent_registrations"], 1)
        self.assertEqual(summary["regular"]["selected_eligible_registrations"], 1)

    def test_changed_details_invalidate_whole_group_and_require_review(self):
        sheet = self.pair()
        self.approve_pair(sheet)
        sheet.data["sources"]["regular"][1]["name"] = "Changed Attendee"
        rows = apply_approved_groups(self.settings, sheet.rows())

        self.assertFalse(rows[0].get("_shared_email_group_id"))
        self.assertTrue(rows[0].get("_shared_email_review_required"))
        self.assertTrue(any("shared-email attendee details changed" in reason
                            for reason in reasons(rows[0], rows, self.settings.event_id)))
        self.assertTrue(any("conflicting approved registrations" in reason
                            for reason in reasons(rows[1], rows, self.settings.event_id)))

    def test_shared_email_approval_does_not_bypass_any_payment_checkbox(self):
        sheet = self.pair()
        sheet.data["sources"]["regular"][0]["confirmation"] = False
        self.approve_pair(sheet)

        report, plan = preview(self.settings, sheet, all_sources=True, limit="all",
                               selection_version=2)

        self.assertEqual([entry["snapshot_row"] for entry in plan["entries"]], [3])
        self.assertTrue(any("waiting for approval: confirmation" in reason
                            for reason in report[0]["blocking_reasons"]))

    def test_interrupted_delivery_is_not_retried_and_does_not_mark_peer_sent(self):
        sheet = self.pair()
        self.approve_pair(sheet)
        synthetic = replace(self.settings, email_enabled=True)  # Synthetic fixture only; no live config or SMTP.
        renderer, mailer = Renderer(synthetic), FakeMailer()
        mailer.failure = EmailAmbiguous
        _, plan = preview(synthetic, sheet, all_sources=True, limit="all", selection_version=2)

        with patch.object(synthetic, "require_email"):
            outcomes = BatchProcessor(synthetic, sheet, renderer, mailer).execute(plan)

        self.assertEqual(outcomes, ["review", "sent"])
        self.assertEqual(len(mailer.calls), 2)
        current = sheet.rows()
        self.assertEqual(current[0].get("email_status"), "AMBIGUOUS")
        self.assertEqual(current[1].get("email_status"), "SENT")
        self.assertNotEqual(current[0].get("participant_id"), current[1].get("participant_id"))
        self.assertNotEqual(current[0].get("ticket_token"), current[1].get("ticket_token"))
        self.assertNotEqual(current[0].get("message_id"), current[1].get("message_id"))

        report, next_plan = preview(synthetic, sheet, all_sources=True, limit="all",
                                    selection_version=2)
        self.assertFalse(next_plan["entries"])
        self.assertFalse(any(entry["in_batch"] for entry in report))
        self.assertEqual(len(mailer.calls), 2)
        self.assertFalse(self.settings.email_enabled)

    def test_database_migration_runs_on_copy_and_preserves_legacy_claims(self):
        root = self.settings.root
        original = root / "legacy.sqlite3"
        copied = root / "journal-copy.sqlite3"
        backup = root / "journal-copy.before.sqlite3"
        with sqlite3.connect(original) as db:
            db.execute("""CREATE TABLE claims (event_id TEXT, email TEXT, record_id TEXT UNIQUE,
                       participant_id TEXT UNIQUE, token TEXT UNIQUE, name TEXT, tier TEXT, price TEXT,
                       source TEXT, PRIMARY KEY(event_id,email))""")
            db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?)",
                       ("event", "shared@example.invalid", "R-existing", "P-existing",
                        "token-existing", "Attendee One", "Regular", "regular", "regular"))
            db.execute("CREATE TABLE plans (id TEXT PRIMARY KEY, digest TEXT)")
            db.execute("INSERT INTO plans VALUES ('plan-safe','digest-safe')")
            db.execute("CREATE TABLE actions (plan_id TEXT, target TEXT, record_id TEXT, message_id TEXT, done INTEGER)")
            db.execute("INSERT INTO actions VALUES ('plan-safe','regular:R-existing','R-existing','<mid@test>',1)")
            db.execute("CREATE TABLE pilot_delivery (plan_id TEXT, target TEXT, status TEXT)")
            db.execute("INSERT INTO pilot_delivery VALUES ('plan-safe','regular:R-existing','ACCEPTED')")
        shutil.copy2(original, copied)

        result = migrate_claims_database(copied, backup)

        self.assertEqual(result["claims_preserved"], 1)
        self.assertEqual(claims_schema(original), "legacy")
        self.assertEqual(claims_schema(copied), "shared_email")
        with sqlite3.connect(copied) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT * FROM plans").fetchall(), [("plan-safe", "digest-safe")])
            self.assertEqual(db.execute("SELECT * FROM actions").fetchall(),
                             [("plan-safe", "regular:R-existing", "R-existing", "<mid@test>", 1)])
            self.assertEqual(db.execute("SELECT * FROM pilot_delivery").fetchall(),
                             [("plan-safe", "regular:R-existing", "ACCEPTED")])
            db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?)",
                       ("event", "shared@example.invalid", "R-new", "P-new",
                        "token-new", "Attendee Two", "Regular", "regular", "regular"))
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
        self.assertEqual(claims_schema(backup), "legacy")


if __name__ == "__main__":
    unittest.main()
