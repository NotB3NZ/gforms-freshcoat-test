import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
METADATA = (
    "event_id", "participant_id", "ticket_token", "normalized_email", "ticket_name",
    "state", "render_status", "email_status", "duplicate_of", "card_path",
    "render_attempts", "email_attempts", "message_id", "last_error", "updated_at",
    "email_sent_at", "checked_in_at", "checkin_request_id", "ticket_status", "checked_in_by",
)
DEFAULT_HEADERS = {"name": "Full Name", "email": "Email Address"}
DEFAULT_HEADERS.update({key: key for key in METADATA})


class ConfigurationError(ValueError):
    pass


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


@dataclass
class Settings:
    root: Path = ROOT
    profile: str = "test"
    env_file: Path = None
    sources_file: Path = None
    tier_field: str = "ticketTier"
    spreadsheet_id: str = ""
    tab_name: str = ""
    event_id: str = ""
    timezone: str = "Asia/Manila"
    service_account: Path = ROOT / "secrets/google-service-account.json"
    headers: dict = field(default_factory=lambda: dict(DEFAULT_HEADERS))
    runtime: Path = ROOT / "runtime"
    generated: Path = ROOT / "generated"
    bun: str = "bun"
    freshcoat: Path = ROOT / "freshcoat"
    template: Path = ROOT / "templates/card.coat"
    name_field: str = "displayName"
    frame: str = "front"
    qr_x: int = 400
    qr_y: int = 450
    qr_size: int = 400
    staff_key: str = field(default="", repr=False)
    server_url: str = "http://127.0.0.1:8000"
    camera_index: int = 0
    station_name: str = "Local staff station"
    poll_seconds: int = 30
    worker_batch_size: int = 1
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    smtp_security: str = "starttls"
    email_enabled: bool = False

    def now(self):
        from datetime import datetime
        return datetime.now(ZoneInfo(self.timezone)).isoformat(timespec="seconds")

    def require_operational(self):
        if self.profile == "production":
            raise ConfigurationError("Production live operations are locked pending the human schema/event-ID checkpoint and separate email authorization. Use local fixture batches for verification.")

    def require_sheet(self):
        self.require_operational()
        if not self.spreadsheet_id or not self.tab_name or not self.event_id:
            raise ConfigurationError("Set SPREADSHEET_ID, SHEET_TAB and EVENT_ID privately in the selected profile settings file.")
        if not self.service_account.is_file():
            raise ConfigurationError("The configured service-account file is missing.")

    def require_staff(self):
        self.require_operational()
        if len(self.staff_key) < 32 or self.staff_key.startswith("REPLACE"):
            raise ConfigurationError("Set STAFF_API_KEY privately to at least 32 random characters in the selected profile settings file.")

    def require_email(self):
        self.require_operational()
        if not self.email_enabled:
            raise ConfigurationError("Email is disabled. Enable it only after authorizing a test recipient.")
        if not all((self.smtp_host, self.smtp_user, self.smtp_password, self.smtp_from)):
            raise ConfigurationError("Complete the SMTP settings privately in the selected profile settings file.")
        if self.smtp_security not in ("starttls", "ssl"):
            raise ConfigurationError("SMTP_SECURITY must be starttls or ssl.")


def load_settings(profile=None, root=None, *, single_entry=False, inbox_test=False):
    root = Path(root or ROOT).resolve()
    profile = profile or os.environ.get("EVENT_CHECKIN_PROFILE", "test")
    if profile not in ("test", "production"):
        raise ConfigurationError("Choose profile test or production.")
    env_file = root / (".env.test" if profile == "test" else ".env.production")
    if profile == "test" and not env_file.is_file():
        env_file = root / ".env"  # Existing installations retain their test configuration.
    if profile == "production" and not env_file.is_file():
        raise ConfigurationError("Create the private .env.production file from .env.production.example first.")
    # Never mutate os.environ or inherit generic SMTP/Sheet values from another profile.
    # Disabling interpolation also prevents ${SMTP_PASSWORD} reaching across profiles.
    config = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}
    def value(key, default=""):
        result = config.get(key, default)
        return default if result is None else result
    def path(key, default):
        p = Path(value(key, default)).expanduser()
        return p if p.is_absolute() else root / p
    headers = dict(DEFAULT_HEADERS)
    header_file = value("HEADERS_FILE", "")
    if header_file:
        mapping = json.loads(path("HEADERS_FILE", "").read_text())
        if set(mapping) - set(headers):
            raise ConfigurationError("HEADERS_FILE contains unknown logical header names.")
        headers.update(mapping)
    headers["name"] = value("NAME_HEADER", headers["name"])
    headers["email"] = value("EMAIL_HEADER", headers["email"])
    if any(not isinstance(v, str) or not v.strip() for v in headers.values()) or len(set(headers.values())) != len(headers):
        raise ConfigurationError("Configured headers must be nonempty and unique.")
    bun_default = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
    s = Settings(
        root=root, profile=profile, env_file=env_file,
        sources_file=path("SOURCES_FILE", "config/sources.production.json") if profile == "production" else None,
        tier_field=value("CARD_TIER_FIELD", "ticketTier"),
        spreadsheet_id=value("SPREADSHEET_ID", ""), tab_name=value("SHEET_TAB", ""),
        event_id=value("EVENT_ID", ""), timezone=value("EVENT_TIMEZONE", "Asia/Manila"),
        service_account=path("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/google-service-account.json"),
        headers=headers, runtime=path("RUNTIME_DIR", "runtime-production" if profile == "production" else "runtime"), generated=path("GENERATED_DIR", "generated-production" if profile == "production" else "generated"),
        bun=value("BUN_BIN", bun_default), freshcoat=path("FRESHCOAT_DIR", "freshcoat"),
        template=path("CARD_TEMPLATE", "templates/card.coat"), name_field=value("CARD_NAME_FIELD", "displayName"),
        frame=value("CARD_FRAME", "front"), qr_x=int(value("QR_X", "400")),
        qr_y=int(value("QR_Y", "450")), qr_size=int(value("QR_SIZE", "400")),
        staff_key=value("STAFF_API_KEY", ""), server_url=value("CHECKIN_SERVER_URL", "http://127.0.0.1:8000"),
        camera_index=int(value("CAMERA_INDEX", "0")), poll_seconds=int(value("WORKER_POLL_SECONDS", "30")),
        station_name=value("STAFF_STATION_NAME", "Local staff station"),
        worker_batch_size=int(value("WORKER_BATCH_SIZE", "1")),
        smtp_host=value("SMTP_HOST", ""), smtp_port=int(value("SMTP_PORT", "587")),
        smtp_user=value("SMTP_USERNAME", ""), smtp_password=value("SMTP_PASSWORD", ""),
        smtp_from=value("SMTP_FROM", ""), smtp_security=value("SMTP_SECURITY", "starttls"),
        email_enabled=value("EMAIL_ENABLED", "false").lower() == "true",
    )
    ZoneInfo(s.timezone)
    if s.poll_seconds < 5:
        raise ConfigurationError("WORKER_POLL_SECONDS must be at least 5.")
    if s.worker_batch_size < 1:
        raise ConfigurationError("WORKER_BATCH_SIZE must be at least 1.")
    if not s.station_name.strip() or len(s.station_name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in s.station_name):
        raise ConfigurationError("STAFF_STATION_NAME must contain 1 to 80 characters without control characters.")
    validate_profile_isolation(s, single_entry=single_entry, inbox_test=inbox_test)
    return s


def _overlap(left, right):
    left, right = left.resolve(), right.resolve()
    return left == right or left in right.parents or right in left.parents


def validate_profile_isolation(settings, *, single_entry=False, inbox_test=False):
    if _overlap(settings.runtime, settings.generated):
        raise ConfigurationError("Runtime and generated-card directories must be separate.")
    other_profile = "production" if settings.profile == "test" else "test"
    other_file = settings.root / (".env.production" if other_profile == "production" else ".env.test")
    if other_profile == "test" and not other_file.is_file():
        other_file = settings.root / ".env"
    other = dotenv_values(other_file, interpolate=False) if other_file.is_file() else {}
    other_event = other.get("EVENT_ID", "")
    if settings.event_id and other_event and settings.event_id == other_event:
        raise ConfigurationError("Test and production must use different EVENT_ID values.")
    def other_path(key, default):
        p = Path(other.get(key) or default).expanduser()
        return p if p.is_absolute() else settings.root / p
    defaults = ("runtime-production", "generated-production") if other_profile == "production" else ("runtime", "generated")
    for current in (settings.runtime, settings.generated):
        for alternate in (other_path("RUNTIME_DIR", defaults[0]), other_path("GENERATED_DIR", defaults[1])):
            if _overlap(current, alternate):
                raise ConfigurationError("Test and production storage must not share or contain each other's directories.")
    if settings.profile == "production" and settings.email_enabled and not (single_entry or inbox_test):
        raise ConfigurationError("General production remains locked; email-enabled settings require an explicitly scoped sending command.")
    manifest = settings.sources_file or other_path("SOURCES_FILE", "config/sources.production.json")
    if settings.profile == "test" and settings.spreadsheet_id and manifest.is_file():
        sources = read_source_manifest(manifest)
        if any(source["spreadsheet_id"] == settings.spreadsheet_id for source in sources):
            raise ConfigurationError("A test profile cannot use a configured production spreadsheet.")


def read_source_manifest(path):
    try:
        data = json.loads(Path(path).read_text())
        sources = data["sources"]
        if data.get("version") != 1 or not isinstance(sources, list) or not sources:
            raise ValueError()
        ids, tables = set(), set()
        required_approvals = {"confirmation", "reference_matched", "price_matched", "payment_received"}
        for source in sources:
            key = source["id"]
            table = (source["spreadsheet_id"], source["tab_id"])
            if not isinstance(key, str) or not key or key in ids or table in tables:
                raise ValueError()
            if not all(isinstance(source.get(name), str) and source[name].strip() for name in ("spreadsheet_id", "tab_name", "price_category")):
                raise ValueError()
            if type(source["tab_id"]) is not int:
                raise ValueError()
            if set(source["approvals"]) != required_approvals or any(not isinstance(v, str) or not v for v in source["approvals"].values()):
                raise ValueError()
            if len(set(source["approvals"].values())) != 4 or set(source["fields"]) != {"email", "name", "tier"}:
                raise ValueError()
            for field_name in ("email", "name", "tier"):
                selector = source["fields"][field_name]
                if not all(isinstance(selector.get(k), str) and selector[k] for k in ("header", "column")):
                    raise ValueError()
            ids.add(key)
            tables.add(table)
        return sources
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("The production source manifest is missing or invalid; check source IDs, tabs, field mappings and all four approval mappings locally.") from None


def production_sources(settings):
    if settings.profile != "production" or not settings.event_id or not settings.sources_file:
        raise ConfigurationError("Production requires its own EVENT_ID and SOURCES_FILE.")
    if not settings.service_account.is_file():
        raise ConfigurationError("The configured service-account file is missing.")
    return read_source_manifest(settings.sources_file)
