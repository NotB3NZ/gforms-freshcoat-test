"""Create an isolated local preview configuration without contacting Google."""
import json
import os
import socket
from pathlib import Path

from dotenv import dotenv_values

from ..config import load_settings
from .security import verify_password


def failure_message(error):
    """Explain connection failures without exposing URLs, credentials or responses."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, socket.gaierror) or type(error).__name__ == "NameResolutionError":
            return "Google could not be reached because DNS name resolution failed. Retry from a terminal with network access."
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in (401, 403):
            return "Google denied access. Check the existing service-account credentials and sharing permissions for every source."
        if status == 404:
            return "A Google source was not found or is inaccessible. Check the reviewed spreadsheet and worksheet mappings."
        error = error.__cause__ or error.__context__
    return "Could not read and validate every registration source. Check the existing configuration, source mappings and Google access."


def prepare_preview(root, source_root):
    root, source_root = Path(root).resolve(), Path(source_root).resolve()
    legacy = load_settings(profile="production", root=source_root)
    if not legacy.event_id or legacy.event_id == "freshcoat-synthetic":
        raise ValueError("The production configuration must contain its actual EVENT_ID.")
    # Reuse the staff login hash, never a plaintext password or SMTP credentials.
    existing = dotenv_values(source_root / ".env.web", interpolate=False)
    password_hash = existing.get("WEB_PASSWORD_HASH", "")
    if not password_hash or not password_hash.startswith("pbkdf2_sha256$"):
        raise ValueError("Set up the local staff password in .env.web first.")
    verify_password("configuration-check", password_hash)
    target = root / ".env.web.preview"
    values = {
        "WEB_PASSWORD_HASH": password_hash,
        "WEB_ORIGIN": "http://localhost:8081",
        "WEB_EVENT_ID": legacy.event_id,
        "WEB_TIMEZONE": legacy.timezone,
        "WEB_SYNTHETIC": "false",
        "WEB_READ_ONLY_PREVIEW": "true",
        "WEB_LIVE_EXPORT": "false",
        "WEB_LIVE_CONFIG": str(source_root),
        "WEB_DATABASE_URL": "sqlite:///" + str(root / ".web-runtime/preview/registrations.sqlite3"),
        "WEB_SYNC_SECONDS": "60",
    }
    # Exclusive creation protects both an existing preview and the running demo.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write("# Private read-only registration preview; never commit.\n")
        for key, value in values.items():
            file.write(key + "=" + json.dumps(value, ensure_ascii=False) + "\n")
    return target
