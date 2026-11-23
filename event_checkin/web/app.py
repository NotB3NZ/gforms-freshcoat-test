import hmac
import secrets
import time
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .adapters import adapter
from .db import Database, one
from .security import digest, verify_password
from .service import AdmissionService, Conflict, NotReady
from .settings import ROOT, WebSettings
from .sync import Synchronizer

COOKIE = "freshcoat_staff"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    password: str = Field(max_length=512)


class Nickname(Input):
    nickname: str = Field(min_length=1, max_length=80)


class Lookup(Input):
    token: str = Field(max_length=2048)


class Frame(Input):
    image: str = Field(max_length=380000)


class Confirm(Input):
    participant_id: str = Field(min_length=1, max_length=200)
    request_id: UUID
    method: Literal["qr", "manual"]


def create_app(settings=None, sheets=None, *, background=True):
    @asynccontextmanager
    async def lifespan(app):
        s = settings or WebSettings.load()
        s.validate()
        db = Database(s.database_url)
        db.migrate(s)
        service = AdmissionService(db, s)
        sync = Synchronizer(service, sheets or adapter(s))
        app.state.settings, app.state.db, app.state.service, app.state.sync = s, db, service, sync
        if background:
            stop, thread = sync.start()
        yield
        if background:
            stop.set()
            thread.join(timeout=1)
        db.engine.dispose()

    app = FastAPI(title="FreshCoat staff", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security(request, call_next):
        s = getattr(app.state, "settings", None)
        if s:
            if request.headers.get("host") != urlsplit(s.origin).netloc:
                return JSONResponse({"detail": "Use the configured WEB_ORIGIN address."}, status_code=400)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                if request.headers.get("origin") != s.origin or request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                    return JSONResponse({"detail": "Same-origin request required."}, status_code=403)
                if request.headers.get("content-type", "").split(";")[0] != "application/json":
                    return JSONResponse({"detail": "JSON request required."}, status_code=415)
                try:
                    if int(request.headers.get("content-length", "0")) > (400000 if request.url.path == "/api/decode" else 8192):
                        return JSONResponse({"detail": "Request too large."}, status_code=413)
                except ValueError:
                    return JSONResponse({"detail": "Invalid request size."}, status_code=400)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Permissions-Policy": "camera=(self), microphone=()",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"})
        if s and s.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def session(request: Request):
        cookie = request.cookies.get(COOKIE, "")
        if not cookie or len(cookie) > 128:
            raise HTTPException(401, "Sign in to continue.")
        with app.state.db.transaction() as conn:
            staff = one(conn, "SELECT * FROM staff_sessions WHERE cookie_digest=:h AND revoked=0 AND expires_at>:t", h=digest(cookie), t=time.time())
        if not staff:
            raise HTTPException(401, "Session expired. Sign in to continue.")
        if request.method not in ("GET", "HEAD") and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), staff["csrf"]):
            raise HTTPException(403, "Session security check failed. Refresh the page.")
        return staff

    def operator(staff=Depends(session)):
        if not staff["nickname"]:
            raise HTTPException(403, "Complete your nickname first.")
        return staff

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse({"detail": "Unable to confirm — retry the same request. Backend database unavailable."}, status_code=503)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(NotReady)
    async def not_ready(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/api/session")
    def identity(request: Request):
        try:
            staff = session(request)
        except HTTPException as exc:
            if exc.status_code == 401:
                return {"authenticated": False}
            raise
        return {"authenticated": True, "nickname": staff["nickname"], "csrf": staff["csrf"],
                "expires_at": staff["expires_at"], "event_id": app.state.settings.event_id}

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        now = time.time()
        with app.state.db.transaction() as conn:
            # Use socket peer only. Never trust arbitrary X-Forwarded-For headers.
            for bucket, maximum in ((digest(request.client.host if request.client else "local"), 15), ("global", 100)):
                attempt = one(conn, "SELECT * FROM login_attempts WHERE bucket=:b" + ("" if app.state.db.sqlite else " FOR UPDATE"), b=bucket)
                if attempt and now - attempt["window_at"] < 900 and attempt["attempts"] >= maximum:
                    raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.", headers={"Retry-After": "900"})
                conn.execute(text("""INSERT INTO login_attempts VALUES(:b,:t,1) ON CONFLICT(bucket) DO UPDATE SET
                 attempts=CASE WHEN login_attempts.window_at<:cutoff THEN 1 ELSE login_attempts.attempts+1 END,
                 window_at=CASE WHEN login_attempts.window_at<:cutoff THEN :t ELSE login_attempts.window_at END"""),
                             {"b": bucket, "t": now, "cutoff": now - 900})
        if not verify_password(body.password, app.state.settings.password_hash):
            raise HTTPException(401, "Incorrect shared password.")
        cookie, csrf, sid = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_hex(16)
        expiry = now + app.state.settings.session_seconds
        with app.state.db.transaction() as conn:
            # A fresh login invalidates this browser's old session.
            old = request.cookies.get(COOKIE)
            if old:
                conn.execute(text("UPDATE staff_sessions SET revoked=1 WHERE cookie_digest=:h"), {"h": digest(old)})
            conn.execute(text("INSERT INTO staff_sessions VALUES(:s,:h,:c,'',:t,:x,0)"), {"s": sid, "h": digest(cookie), "c": csrf, "t": now, "x": expiry})
        response.set_cookie(COOKIE, cookie, httponly=True, secure=app.state.settings.secure_cookie,
                            samesite="strict", max_age=app.state.settings.session_seconds, path="/")
        return {"authenticated": True, "nickname": "", "csrf": csrf, "expires_at": expiry, "event_id": app.state.settings.event_id}

    @app.post("/api/nickname")
    def nickname(body: Nickname, staff=Depends(session)):
        name = body.nickname.strip()
        if not 1 <= len(name) <= 40 or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise HTTPException(422, "Use 1–40 characters without control characters.")
        with app.state.db.transaction() as conn:
            changed = conn.execute(text("UPDATE staff_sessions SET nickname=:n WHERE session_id=:s AND nickname='' AND revoked=0 AND expires_at>:t"),
                                   {"n": name, "s": staff["session_id"], "t": time.time()}).rowcount
            if not changed:
                raise HTTPException(409, "Nickname already set or session expired. Log out to change operators.")
        return {"nickname": name}

    @app.post("/api/logout")
    def logout(response: Response, staff=Depends(session)):
        with app.state.db.transaction() as conn:
            conn.execute(text("UPDATE staff_sessions SET revoked=1 WHERE session_id=:s"), {"s": staff["session_id"]})
        response.delete_cookie(COOKIE, path="/", secure=app.state.settings.secure_cookie, httponly=True, samesite="strict")
        return {"status": "signed_out"}

    @app.post("/api/lookup")
    def lookup(body: Lookup, staff=Depends(operator)):
        return app.state.service.lookup(body.token)

    @app.post("/api/decode")
    def decode(body: Frame, staff=Depends(operator)):
        from .decoder import decode_image
        try:
            token = decode_image(body.image)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        return app.state.service.lookup(token) if token else {"status": "no_qr"}

    @app.get("/api/participants")
    def participants(q: str = Query("", max_length=160), tier: str = Query("", max_length=50),
                     attendance: Literal["", "arrived", "waiting", "ineligible"] = "", offset: int = Query(0, ge=0),
                     limit: int = Query(25, ge=1, le=100), staff=Depends(operator)):
        return app.state.service.list_participants(q, tier, attendance, offset, limit)

    @app.get("/api/participants/{participant_id}")
    def participant(participant_id: str, staff=Depends(operator)):
        with app.state.db.transaction() as conn:
            result = app.state.service.participant(conn, participant_id)
        if not result:
            raise HTTPException(404, "Participant not found.")
        return result

    @app.post("/api/checkins")
    def checkin(body: Confirm, staff=Depends(operator)):
        return app.state.service.confirm(body.participant_id, str(body.request_id), body.method, staff)

    @app.get("/api/requests/{request_id}")
    def reconcile(request_id: UUID, staff=Depends(operator)):
        # Any signed-in operator may read a known request's outcome after re-login.
        # A new session cannot impersonate the prior session for a POST replay.
        import json
        with app.state.db.transaction() as conn:
            prior = one(conn, "SELECT result FROM requests WHERE event_id=:e AND request_id=:r", e=app.state.settings.event_id, r=str(request_id))
        return {"resolved": bool(prior), "result": json.loads(prior["result"]) if prior else None}

    @app.get("/api/dashboard")
    def dashboard(staff=Depends(operator)):
        return app.state.service.dashboard()

    @app.post("/api/sync")
    def sync_now(staff=Depends(operator)):
        app.state.sync.request()
        return {"status": "queued"}

    frontend = ROOT / "frontend"
    app.mount("/assets", StaticFiles(directory=frontend / "dist", check_dir=False), name="assets")

    @app.get("/")
    def index():
        path = frontend / "dist/index.html"
        if not path.exists():
            raise HTTPException(503, "Build the frontend: cd frontend && npm ci && npm run build")
        return FileResponse(path)

    return app


app = create_app()
