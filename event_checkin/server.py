import secrets
from contextlib import asynccontextmanager
from typing import Optional
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .checkin import CheckinService, RequestConflict
from .config import load_settings
from .locking import file_lock
from .sheets import GoogleSheet


class ScanRequest(BaseModel):
    token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$")
    request_id: UUID
    station_name: str = Field(default="Local staff station", min_length=1, max_length=80,
                              pattern=r"^[^\x00-\x1f\x7f]*[^\s\x00-\x1f\x7f][^\x00-\x1f\x7f]*$")


def create_app(settings=None, sheet=None):
    @asynccontextmanager
    async def lifespan(app):
        s = settings or load_settings()
        s.require_staff()
        # Reject even accidental --workers 2 on this host instead of weakening coordination.
        with file_lock(s.runtime / "server.lock", blocking=False):
            app.state.settings = s
            app.state.checkin = CheckinService(s, sheet if sheet is not None else GoogleSheet(s))
            yield

    app = FastAPI(title="Event check-in (staff only)", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health():
        return {"status": "running"}  # Does not claim Sheets is reachable.

    @app.post("/check-in")
    def check_in(scan: ScanRequest, x_staff_key: Optional[str] = Header(default=None)):
        if not x_staff_key or not secrets.compare_digest(x_staff_key.encode(), app.state.settings.staff_key.encode()):
            raise HTTPException(status_code=401, detail="Staff credential required.")
        try:
            return app.state.checkin.check_in(scan.token, str(scan.request_id), scan.station_name)
        except RequestConflict:
            raise HTTPException(status_code=409, detail="Request ID belongs to another ticket.") from None
        except Exception:
            # Google/HTTP exceptions may include response data. No raw exception in logs or UI.
            raise HTTPException(status_code=503, detail="Attendance cannot be confirmed. Retry the same request ID.") from None
    return app
