import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

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

    def require_sheet(self):
        if not self.spreadsheet_id or not self.tab_name or not self.event_id:
            raise ConfigurationError("Set SPREADSHEET_ID, SHEET_TAB and EVENT_ID privately in .env.")
        if not self.service_account.is_file():
            raise ConfigurationError("The configured service-account file is missing.")

    def require_staff(self):
        if len(self.staff_key) < 32 or self.staff_key.startswith("REPLACE"):
            raise ConfigurationError("Set STAFF_API_KEY privately to at least 32 random characters.")

    def require_email(self):
        if not self.email_enabled:
            raise ConfigurationError("Email is disabled. Enable it only after authorizing a test recipient.")
        if not all((self.smtp_host, self.smtp_user, self.smtp_password, self.smtp_from)):
            raise ConfigurationError("Complete the SMTP settings privately in .env.")
        if self.smtp_security not in ("starttls", "ssl"):
            raise ConfigurationError("SMTP_SECURITY must be starttls or ssl.")


def load_settings():
    load_dotenv(ROOT / ".env", override=False)
    def path(key, default):
        p = Path(os.getenv(key, default)).expanduser()
        return p if p.is_absolute() else ROOT / p
    headers = dict(DEFAULT_HEADERS)
    header_file = os.getenv("HEADERS_FILE", "")
    if header_file:
        mapping = json.loads(path("HEADERS_FILE", "").read_text())
        if set(mapping) - set(headers):
            raise ConfigurationError("HEADERS_FILE contains unknown logical header names.")
        headers.update(mapping)
    headers["name"] = os.getenv("NAME_HEADER", headers["name"])
    headers["email"] = os.getenv("EMAIL_HEADER", headers["email"])
    if any(not isinstance(v, str) or not v.strip() for v in headers.values()) or len(set(headers.values())) != len(headers):
        raise ConfigurationError("Configured headers must be nonempty and unique.")
    bun_default = shutil.which("bun") or str(Path.home() / ".bun/bin/bun")
    s = Settings(
        spreadsheet_id=os.getenv("SPREADSHEET_ID", ""), tab_name=os.getenv("SHEET_TAB", ""),
        event_id=os.getenv("EVENT_ID", ""), timezone=os.getenv("EVENT_TIMEZONE", "Asia/Manila"),
        service_account=path("GOOGLE_SERVICE_ACCOUNT_FILE", "secrets/google-service-account.json"),
        headers=headers, runtime=path("RUNTIME_DIR", "runtime"), generated=path("GENERATED_DIR", "generated"),
        bun=os.getenv("BUN_BIN", bun_default), freshcoat=path("FRESHCOAT_DIR", "freshcoat"),
        template=path("CARD_TEMPLATE", "templates/card.coat"), name_field=os.getenv("CARD_NAME_FIELD", "displayName"),
        frame=os.getenv("CARD_FRAME", "front"), qr_x=int(os.getenv("QR_X", "400")),
        qr_y=int(os.getenv("QR_Y", "450")), qr_size=int(os.getenv("QR_SIZE", "400")),
        staff_key=os.getenv("STAFF_API_KEY", ""), server_url=os.getenv("CHECKIN_SERVER_URL", "http://127.0.0.1:8000"),
        camera_index=int(os.getenv("CAMERA_INDEX", "0")), poll_seconds=int(os.getenv("WORKER_POLL_SECONDS", "30")),
        station_name=os.getenv("STAFF_STATION_NAME", "Local staff station"),
        worker_batch_size=int(os.getenv("WORKER_BATCH_SIZE", "1")),
        smtp_host=os.getenv("SMTP_HOST", ""), smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USERNAME", ""), smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", ""), smtp_security=os.getenv("SMTP_SECURITY", "starttls"),
        email_enabled=os.getenv("EMAIL_ENABLED", "false").lower() == "true",
    )
    ZoneInfo(s.timezone)
    if s.poll_seconds < 5:
        raise ConfigurationError("WORKER_POLL_SECONDS must be at least 5.")
    if s.worker_batch_size < 1:
        raise ConfigurationError("WORKER_BATCH_SIZE must be at least 1.")
    if not s.station_name.strip() or len(s.station_name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in s.station_name):
        raise ConfigurationError("STAFF_STATION_NAME must contain 1 to 80 characters without control characters.")
    return s
