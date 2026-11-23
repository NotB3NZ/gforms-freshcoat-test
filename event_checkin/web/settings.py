import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def config_path():
    path = Path(os.environ.get("WEB_ENV_FILE", str(ROOT / ".env.web"))).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass
class WebSettings:
    database_url: str
    password_hash: str
    origin: str = "http://localhost:8080"
    event_id: str = "freshcoat-synthetic"
    timezone: str = "Asia/Manila"
    fixture: Path = ROOT / ".web-runtime/sheets.json"
    session_seconds: int = 28800
    sync_seconds: int = 45
    synthetic: bool = True
    live_config: str = ""
    live_export: bool = False
    read_only_preview: bool = False

    @property
    def mode(self):
        return "preview" if self.read_only_preview else "synthetic" if self.synthetic else "live"

    @property
    def secure_cookie(self):
        return urlsplit(self.origin).scheme == "https"

    def validate(self):
        from .security import verify_password
        parts = urlsplit(self.origin)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username:
            raise ValueError("WEB_ORIGIN must be an exact HTTP(S) origin, without path or credentials.")
        if not self.secure_cookie and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("HTTP cookie exception is restricted to localhost. Use HTTPS elsewhere.")
        if not self.database_url.startswith(("sqlite:///", "postgresql+psycopg://")):
            raise ValueError("Use a PostgreSQL psycopg URL or explicit local SQLite URL.")
        if self.read_only_preview:
            if self.synthetic or self.live_export:
                raise ValueError("Read-only preview requires WEB_SYNTHETIC=false and WEB_LIVE_EXPORT=false.")
            if parts.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Read-only preview is restricted to a localhost origin.")
            if not self.live_config or not self.event_id or self.event_id == "freshcoat-synthetic":
                raise ValueError("Preview requires the production configuration root and its actual event ID.")
        elif not self.synthetic and (not self.secure_cookie or not self.database_url.startswith("postgresql+psycopg://")):
            raise ValueError("Live mode requires HTTPS and PostgreSQL.")
        if not self.password_hash or not self.password_hash.startswith("pbkdf2_sha256$"):
            raise ValueError("Run python -m event_checkin.web.cli password; no password is built in.")
        verify_password("configuration-check", self.password_hash)
        ZoneInfo(self.timezone)
        if not 60 <= self.session_seconds <= 86400 or not 10 <= self.sync_seconds <= 3600:
            raise ValueError("Session must be 60..86400s; sync must be 10..3600s.")
        return self

    @classmethod
    def load(cls):
        # Intentionally never loads .env, .env.test or .env.production.
        values = dict(dotenv_values(config_path(), interpolate=False))
        values.update({k: v for k, v in os.environ.items() if k.startswith("WEB_")})
        def v(key, default=""):
            return values.get("WEB_" + key, default)
        def flag(key, default):
            value = v(key, default)
            if value not in ("true", "false"):
                raise ValueError("WEB_" + key + " must be exactly true or false.")
            return value == "true"
        return cls(database_url=v("DATABASE_URL", "sqlite:///" + str(ROOT / ".web-runtime/web.sqlite3")),
                   password_hash=v("PASSWORD_HASH"), origin=v("ORIGIN", "http://localhost:8080"),
                   event_id=v("EVENT_ID", "freshcoat-synthetic"), timezone=v("TIMEZONE", "Asia/Manila"),
                   fixture=Path(v("FIXTURE", str(ROOT / ".web-runtime/sheets.json"))),
                   session_seconds=int(v("SESSION_SECONDS", "28800")), sync_seconds=int(v("SYNC_SECONDS", "45")),
                   synthetic=flag("SYNTHETIC", "true"), live_config=v("LIVE_CONFIG"),
                   live_export=flag("LIVE_EXPORT", "false"),
                   read_only_preview=flag("READ_ONLY_PREVIEW", "false")).validate()
