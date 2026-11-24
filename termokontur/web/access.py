"""Login, sessions and building-level access to the panel."""
from __future__ import annotations

import hmac
import os
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .. import auth, school_calendar

router = APIRouter(prefix="/api")


def signature(message: str) -> str:
    return hmac.new(os.environ["TERMOKONTUR_SECRET"].encode(),
                    message.encode(), "sha256").hexdigest()


def session_cookie() -> str:
    payload = f"{int(time.time()) + 8 * 3600}:{secrets.token_urlsafe(18)}"
    return f"{payload}:{signature(payload + ':' + str(auth.session_epoch()))}"


def authenticated(request: Request) -> bool:
    cookie = request.cookies.get("tk_session", "")
    try:
        expires, nonce, mac = cookie.split(":", 2)
        if int(expires) <= time.time() or len(nonce) < 12:
            return False
        return hmac.compare_digest(
            mac, signature(f"{expires}:{nonce}:{auth.session_epoch()}")
        )
    except (ValueError, KeyError):
        return False


def _building_session(request: Request) -> dict | None:
    cookie = request.cookies.get("tk_session", "")
    if not cookie.startswith("v:") or len(cookie) > 256:
        return None
    try:
        _, username, expires, nonce, mac = cookie.split(":")
        if int(expires) <= time.time() or len(nonce) < 12:
            return None
    except ValueError:
        return None
    account = auth.building_account(username)
    if account and hmac.compare_digest(mac, _building_signature(cookie.rsplit(":", 1)[0], account)):
        return account
    return None


def _building_signature(payload: str, account: dict) -> str:
    return signature(f"{payload}:{auth.session_epoch()}:{account['session_key']}")


def _verify_origin(request: Request) -> None:
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            host = request.headers.get("host", "")
            if origin not in (f"http://{host}", f"https://{host}"):
                raise HTTPException(403, "Недопустимый источник запроса")


def require_admin(request: Request):
    if not authenticated(request):
        raise HTTPException(401, "Войдите как администратор")
    _verify_origin(request)
    request.state.audit_admin = True


def scoped_access(request: Request) -> str:
    if authenticated(request):
        scope = ""
    else:
        account = _building_session(request)
        if not account:
            raise HTTPException(401, "Войдите в панель")
        scope = account["building"]
    _verify_origin(request)
    return scope


def scope_building(requested: str, allowed: str) -> str:
    if allowed and requested and requested != allowed:
        raise HTTPException(403, "Отчёт доступен только по вашему корпусу")
    return allowed or requested


class Login(BaseModel):
    password: str
    username: str = Field(default="", max_length=32)


class BuildingAccountIn(BaseModel):
    building: str = Field(min_length=1, max_length=100)
    password: str | None = Field(default=None, max_length=256)
    max_user_id: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


@router.get("/session")
def session(request: Request):
    if authenticated(request):
        return {"authenticated": True}
    account = _building_session(request)
    if account:
        return {"authenticated": True, "building": account["building"]}
    return {"authenticated": False}


@router.post("/login")
def login(payload: Login, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    failed_logins = request.app.state.failed_logins
    failures = [t for t in failed_logins.get(ip, []) if now - t < 300]
    if len(failures) >= 5:
        raise HTTPException(429, "Слишком много попыток входа. Повторите позже.")
    username = payload.username.strip().lower()
    viewer = auth.check_building_password(username, payload.password) if username else None
    if (username and not viewer) or (not username and not auth.check_password(payload.password)):
        failures.append(now)
        failed_logins[ip] = failures
        raise HTTPException(401, "Неверный пароль")
    failed_logins.pop(ip, None)
    if viewer:
        session_data = f"v:{username}:{int(time.time()) + 8 * 3600}:{secrets.token_urlsafe(18)}"
        cookie = f"{session_data}:{_building_signature(session_data, viewer)}"
    else:
        cookie = session_cookie()
    response.set_cookie("tk_session", cookie, httponly=True, samesite="strict",
                        secure=(os.getenv("TERMOKONTUR_SECURE_COOKIE") == "1" or request.url.scheme == "https"),
                        max_age=8 * 3600, path="/")
    return {"ok": True, "building": viewer["building"]} if viewer else {"ok": True}


@router.post("/logout", dependencies=[Depends(scoped_access)])
def logout(response: Response):
    response.delete_cookie("tk_session", path="/", samesite="strict")
    return {"ok": True}


@router.post("/settings/password", dependencies=[Depends(require_admin)])
def change_password(payload: PasswordIn, response: Response):
    try:
        changed = auth.change_password(payload.current_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not changed:
        raise HTTPException(403, "Неверный текущий пароль")
    response.delete_cookie("tk_session", path="/", samesite="strict")
    return {"ok": True, "relogin_required": True}


@router.get("/building-accounts", dependencies=[Depends(require_admin)])
def building_accounts():
    return {"accounts": auth.list_building_accounts(), "buildings": school_calendar.buildings()}


@router.put("/building-accounts/{username}", dependencies=[Depends(require_admin)])
def save_building_account(username: str, payload: BuildingAccountIn):
    try:
        return auth.save_building_account(username.lower(), **payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/building-accounts/{username}", dependencies=[Depends(require_admin)])
def delete_building_account(username: str):
    if not auth.remove_building_account(username):
        raise HTTPException(404, "Учётная запись не найдена")
    return {"ok": True}
