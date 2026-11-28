from contextlib import contextmanager
from pathlib import Path
import unicodedata
import os

from sqlalchemy import create_engine, event, text


class Database:
    def __init__(self, url):
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            path = Path(url.removeprefix("sqlite:///"))
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create privately before SQLite opens WAL/SHM files (which inherit mode).
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(descriptor)
            path.chmod(0o600)
        self.engine = create_engine(url, pool_pre_ping=True,
                                    connect_args={"timeout": 30, "check_same_thread": False} if url.startswith("sqlite") else {"connect_timeout": 10})
        self.sqlite = self.engine.dialect.name == "sqlite"
        if self.sqlite:
            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.isolation_level = None
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.execute("PRAGMA journal_mode=WAL")
                dbapi.execute("PRAGMA synchronous=FULL")
            @event.listens_for(self.engine, "begin")
            def begin(conn):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
        if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
            # SQLite may create the file only on first connect.
            with self.engine.connect():
                pass
            Path(url.removeprefix("sqlite:///")).chmod(0o600)

    @contextmanager
    def transaction(self, event_id=None):
        with self.engine.begin() as conn:
            if event_id:
                conn.execute(text("SELECT event_id FROM events WHERE event_id=:e" +
                                  ("" if self.sqlite else " FOR UPDATE")), {"e": event_id}).first()
            yield conn

    def migrate(self, settings):
        with self.transaction() as conn:
            if not self.sqlite:
                # Serialize schema upgrades across overlapping Render instances.
                conn.execute(text("SELECT pg_advisory_xact_lock(734218605)"))
            # Bootstrap and all versioned DDL are transactionally applied. Run once before workers.
            conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)"))
            for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                if conn.execute(text("SELECT 1 FROM schema_migrations WHERE version=:v"), {"v": path.name}).first():
                    continue
                source = "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("--"))
                for statement in source.split(";"):
                    if statement.strip():
                        conn.execute(text(statement))
                conn.execute(text("INSERT INTO schema_migrations VALUES(:v)"), {"v": path.name})
            profile = one(conn, "SELECT mode FROM web_database_mode WHERE id=1")
            if profile and profile["mode"] != settings.mode:
                raise ValueError("Database belongs to a different data mode. Use a separate database; do not reuse demo or preview attendance.")
            if not profile:
                if settings.read_only_preview and any(one(conn, "SELECT COUNT(*) AS n FROM " + table)["n"]
                                                       for table in ("events", "participants", "attendance", "requests", "outbox", "staff_sessions")):
                    raise ValueError("Read-only preview requires a fresh, separate database.")
                conn.execute(text("INSERT INTO web_database_mode(id,mode) VALUES(1,:mode)"), {"mode": settings.mode})
            for row in many(conn, "SELECT event_id,participant_id,name,email FROM participants WHERE search_text=''"):
                conn.execute(text("UPDATE participants SET search_text=:q WHERE event_id=:e AND participant_id=:p"),
                             {"q": unicodedata.normalize("NFKC", row["name"] + " " + row["email"]).casefold(),
                              "e": row["event_id"], "p": row["participant_id"]})
            conn.execute(text("INSERT INTO events(event_id,timezone) VALUES(:e,:tz) ON CONFLICT(event_id) DO NOTHING"),
                         {"e": settings.event_id, "tz": settings.timezone})
            conn.execute(text("INSERT INTO sync_state(event_id) VALUES(:e) ON CONFLICT(event_id) DO NOTHING"), {"e": settings.event_id})


def one(conn, sql, **params):
    row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row else None


def many(conn, sql, **params):
    return [dict(r) for r in conn.execute(text(sql), params).mappings()]
