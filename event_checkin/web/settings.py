import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def database_url(value):
    """Render supplies PostgreSQL URLs without a SQLAlchemy driver name."""
    for prefix in ("postgres://", "postgresql://"):
        if value.startswith(prefix):
            return "postgresql+psycopg://" + value[len(prefix):]
    return value


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
    api_origin: str = ""
    auto_open_admissions: bool = False

    @property
    def mode(self):
        return "preview" if self.read_only_preview else "synthetic" if self.synthetic else "live"

    @property
    def secure_cookie(self):
        return urlsplit(self.origin).scheme == "https"

    def validate(self):
        from .security import verify_password
        parts = urlsplit(self.origin)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError("WEB_ORIGIN must be an exact HTTP(S) origin, without path or credentials.")
        if not self.secure_cookie and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("HTTP cookie exception is restricted to localhost. Use HTTPS elsewhere.")
        if self.api_origin:
            api = urlsplit(self.api_origin)
            if (api.scheme != "https" or not api.hostname or api.path or api.query
                    or api.fragment or api.username or api.password):
                raise ValueError("WEB_API_ORIGIN must be an exact HTTPS origin without path or credentials.")
            if self.read_only_preview or not self.secure_cookie:
                raise ValueError("WEB_API_ORIGIN requires an HTTPS frontend outside local preview mode.")
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
        if self.mode == "live":
            from sqlalchemy.engine import make_url
            try:
                url = make_url(self.database_url)
                valid_database = bool(url.host and url.username and url.database and (url.port is None or 1 <= url.port <= 65535))
            except Exception:
                valid_database = False
            if not valid_database:
                raise ValueError("WEB_DATABASE_URL must specify a PostgreSQL host, user and database.") from None
            if (not self.event_id or self.event_id != self.event_id.strip() or len(self.event_id) > 200
                    or any(ord(c) < 32 for c in self.event_id) or self.event_id == "freshcoat-synthetic"
                    or self.event_id.upper().startswith("REPLACE")):
                raise ValueError("WEB_EVENT_ID must be the permanent production event ID from issued registrations.")
            if not self.live_config:
                raise ValueError("WEB_LIVE_CONFIG must point to the reviewed production registration configuration root.")
        if self.auto_open_admissions and self.mode != "live":
            raise ValueError("WEB_AUTO_OPEN_ADMISSIONS requires live mode; demo and preview retain their existing setup.")
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
        render = os.environ.get("RENDER") == "true"
        # Render must not silently inherit a developer's default demo file.
        # An explicitly selected WEB_ENV_FILE remains authoritative below env vars.
        values = dict(dotenv_values(config_path(), interpolate=False)) if not render or "WEB_ENV_FILE" in os.environ else {}
        values.update({k: v for k, v in os.environ.items() if k.startswith("WEB_")})
        def v(key, default=""):
            return values.get("WEB_" + key, default)
        def flag(key, default):
            value = v(key, default)
            if value not in ("true", "false"):
                raise ValueError("WEB_" + key + " must be exactly true or false.")
            return value == "true"
        preview = flag("READ_ONLY_PREVIEW", "false")
        synthetic = flag("SYNTHETIC", "false" if render or preview else "true")
        render_live = render and not synthetic and not preview
        db_default = os.environ.get("DATABASE_URL", "") if render_live else "sqlite:///" + str(ROOT / ".web-runtime/web.sqlite3")
        if render_live and not v("DATABASE_URL", db_default):
            raise ValueError("Set WEB_DATABASE_URL (or Render DATABASE_URL) to the production PostgreSQL connection.")
        return cls(database_url=database_url(v("DATABASE_URL", db_default)),
                   password_hash=v("PASSWORD_HASH"), origin=v("ORIGIN", os.environ.get("RENDER_EXTERNAL_URL", "") if render_live else "http://localhost:8080"),
                   event_id=v("EVENT_ID", "" if render_live else "freshcoat-synthetic"), timezone=v("TIMEZONE", "Asia/Manila"),
                   fixture=Path(v("FIXTURE", str(ROOT / ".web-runtime/sheets.json"))),
                   session_seconds=int(v("SESSION_SECONDS", "28800")), sync_seconds=int(v("SYNC_SECONDS", "45")),
                   synthetic=synthetic, live_config=v("LIVE_CONFIG", str(ROOT) if render_live else ""),
                   live_export=flag("LIVE_EXPORT", "true" if render_live else "false"),
                   auto_open_admissions=flag("AUTO_OPEN_ADMISSIONS", "true" if render_live else "false"),
                   read_only_preview=preview,
                   api_origin=v("API_ORIGIN")).validate()
