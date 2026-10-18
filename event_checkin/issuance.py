"""Durable local identity reservations, including BEFORE an uncertain Sheets write."""
import os
import secrets
import sqlite3
import uuid
from contextlib import closing

from .config import private_directory
from .sheets import SheetIntegrityError


class IdentityJournal:
    def __init__(self, settings):
        self.path = private_directory(settings.runtime) / "issuance.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS identities (
                event_id TEXT NOT NULL, email TEXT NOT NULL,
                participant_id TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE,
                PRIMARY KEY(event_id, email))""")

    def db(self):
        return closing(sqlite3.connect(self.path, timeout=30, isolation_level=None))

    def reserve(self, event_id, email, participant_id="", token=""):
        # Called under worker.lock and sheets.lock. Email/event is the canonical
        # registration key by policy; a changing row number is never the key.
        with self.db() as db:
            existing = db.execute("SELECT participant_id,token FROM identities WHERE event_id=? AND email=?",
                                  (event_id, email)).fetchone()
            if existing:
                if participant_id and existing != (participant_id, token):
                    raise SheetIntegrityError("Local identity reservation conflicts with Sheets; manual review required.")
                return existing
            pid = participant_id or "P-" + str(uuid.uuid4())
            token = token or secrets.token_urlsafe(32)
            try:
                db.execute("INSERT INTO identities VALUES(?,?,?,?)", (event_id, email, pid, token))
            except sqlite3.IntegrityError:
                raise SheetIntegrityError("Identity reservation conflict; manual review required.") from None
            return pid, token
