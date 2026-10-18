import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import ConfigurationError, private_directory
from .locking import file_lock

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class VisibilityGate:
    """A decoded QR submits once until absent for a grace period."""
    def __init__(self, absent_seconds=2):
        self.last_seen = {}
        self.absent_seconds = absent_seconds

    def update(self, tokens, now):
        self.last_seen = {t: seen for t, seen in self.last_seen.items()
                          if t in tokens or now - seen < self.absent_seconds}
        fresh = [t for t in tokens if t not in self.last_seen]
        for token in tokens:
            self.last_seen[token] = now
        return fresh


class ScanClient:
    def __init__(self, settings, session=None):
        self.s = settings
        self.session = session or requests.Session()
        self.path = private_directory(settings.runtime) / "scanner-pending.json"
        self.pending = None
        if self.path.exists():
            self.pending = json.loads(self.path.read_text())
            if self.pending.get("server") != settings.server_url or self.pending.get("event") != settings.event_id:
                raise ConfigurationError("An unresolved scan belongs to a different server/event. Restore its settings and resolve it first.")

    def begin(self, token):
        if self.pending:
            return False
        self.pending = {"token": token, "request_id": str(uuid.uuid4()), "server": self.s.server_url,
                        "event": self.s.event_id, "station_name": self.s.station_name}
        stage = self.path.with_suffix(".tmp")
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.pending, f)
            f.flush()
            os.fsync(f.fileno())
        stage.replace(self.path)
        return True

    def submit(self):
        if not self.pending:
            return {"status": "idle"}
        try:
            response = self.session.post(self.s.server_url.rstrip("/") + "/check-in",
                                         headers={"X-Staff-Key": self.s.staff_key},
                                         json={**{k: self.pending[k] for k in ("token", "request_id")},
                                               "station_name": self.pending.get("station_name", self.s.station_name)},
                                         timeout=(3, 10), allow_redirects=False)
            if response.status_code != 200:
                return {"status": "connection_error"}
            result = response.json()
            if not isinstance(result, dict):
                return {"status": "connection_error"}
            if result.get("request_id") != self.pending["request_id"] or result.get("status") not in ("accepted", "duplicate", "invalid"):
                return {"status": "connection_error"}
            if result["status"] in ("accepted", "duplicate") and not result.get("checked_in_at"):
                return {"status": "connection_error"}
        except (requests.RequestException, ValueError, TypeError):
            return {"status": "connection_error"}
        self.path.unlink(missing_ok=True)
        self.pending = None
        return result


def run_camera(settings):
    import cv2
    settings.require_staff()
    parsed = urlparse(settings.server_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigurationError("CHECKIN_SERVER_URL must be an HTTP(S) URL without embedded credentials.")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigurationError("Use HTTPS for remote staff scans. Keep this initial version on localhost.")
    with file_lock(settings.runtime / "scanner.lock", blocking=False):
        client = ScanClient(settings)
        gate = VisibilityGate()
        # Suppress an unresolved scan's QR again when it is successfully recovered.
        if client.pending:
            gate.last_seen[client.pending["token"]] = time.monotonic()
        detector = cv2.QRCodeDetector()
        capture = cv2.VideoCapture(settings.camera_index)
        if not capture.isOpened():
            capture.release()
            raise RuntimeError("Camera unavailable. Check CAMERA_INDEX and operating-system camera permission.")
        label, color, next_retry = "Ready - show one QR. Q quits.", (255, 255, 255), 0.0
        colors = {"accepted": (0, 180, 0), "duplicate": (0, 165, 255), "invalid": (0, 0, 255), "connection_error": (0, 0, 255)}
        labels = {"accepted": "ACCEPTED - admission confirmed", "duplicate": "Already checked in - do not admit",
                  "invalid": "Invalid or cancelled ticket - do not admit",
                  "connection_error": "Unable to confirm - do not admit; retrying"}
        print("Scanner ready. Only ACCEPTED confirms admission. Q quits; R retries a pending scan.")
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    raise RuntimeError("Camera stopped returning frames. No admission confirmed.")
                decoded, _, _ = detector.detectAndDecode(frame)
                visible = {decoded} if decoded else set()
                # Do not consume a new person's QR while an earlier scan is unresolved.
                observed = visible if not client.pending else visible.intersection(gate.last_seen)
                fresh = gate.update(observed, time.monotonic())
                if not decoded and not client.pending:
                    label, color = "Ready - show one QR. Q quits.", (255, 255, 255)
                if fresh and not client.pending:
                    token = fresh[0]
                    if TOKEN_PATTERN.fullmatch(token):
                        client.begin(token)
                        next_retry = 0
                    else:
                        label, color = labels["invalid"], colors["invalid"]
                if client.pending and time.monotonic() >= next_retry:
                    label, color = "Checking attendance - please wait", (255, 255, 255)
                    cv2.putText(frame, label, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2)
                    cv2.imshow("Staff check-in", frame)
                    cv2.waitKey(1)
                    result = client.submit()
                    status = result["status"]
                    label, color = labels[status], colors[status]
                    print(label)  # Never print QR tokens, request bodies or staff keys.
                    next_retry = time.monotonic() + 3
                cv2.rectangle(frame, (0, 0), (frame.shape[1], 65), (20, 20, 20), -1)
                cv2.putText(frame, label, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.57, color, 2)
                cv2.imshow("Staff check-in", frame)
                key = cv2.waitKey(20) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("r"):
                    next_retry = 0
        finally:
            capture.release()
            cv2.destroyAllWindows()
