import base64
import io

import pyotp
import qrcode
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    generate_totp_secret,
    get_current_user,
    is_locked,
    record_failure,
    record_success,
    totp_provisioning_uri,
    verify_totp,
)
from ..database import get_db
from ..models import AdminUser
from ..security import create_session_token, verify_password
from ..templates import templates

router = APIRouter()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    ip = _client_ip(request)

    if await is_locked(db, ip):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many failed attempts. Please wait before trying again."},
            status_code=429,
        )

    from sqlalchemy import select
    result = await db.execute(select(AdminUser).where(AdminUser.username == username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        await record_failure(db, ip)
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Invalid credentials"}, status_code=401
        )

    if user.totp_enabled:
        if not totp_code:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": None, "need_totp": True, "username": username, "password": password},
            )
        if not verify_totp(user.totp_secret, totp_code):
            await record_failure(db, ip)
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Invalid 2FA code", "need_totp": True, "username": username, "password": password},
                status_code=401,
            )

    await record_success(db, ip)

    from datetime import datetime
    user.last_login = datetime.utcnow()
    await db.commit()

    token = create_session_token(user.id)
    response = RedirectResponse(
        url="/setup-2fa" if user.must_setup_totp else "/", status_code=303
    )
    response.set_cookie(
        "session_token", token, httponly=True, samesite="strict", max_age=28800
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session_token")
    return response


@router.get("/setup-2fa", response_class=HTMLResponse)
async def setup_2fa_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if not user.totp_secret:
        user.totp_secret = generate_totp_secret()
        await db.commit()

    uri = totp_provisioning_uri(user.totp_secret, user.username)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return templates.TemplateResponse(
        "setup_2fa.html",
        {
            "request": request,
            "user": user,
            "qr_b64": qr_b64,
            "totp_secret": user.totp_secret,
            "error": None,
        },
    )


@router.post("/setup-2fa")
async def setup_2fa(
    request: Request,
    totp_code: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if verify_totp(user.totp_secret, totp_code):
        user.totp_enabled = True
        user.must_setup_totp = False
        await db.commit()
        return RedirectResponse("/", status_code=303)

    uri = totp_provisioning_uri(user.totp_secret, user.username)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return templates.TemplateResponse(
        "setup_2fa.html",
        {
            "request": request,
            "user": user,
            "qr_b64": qr_b64,
            "totp_secret": user.totp_secret,
            "error": "Invalid code, please try again",
        },
    )


@router.post("/setup-2fa/skip")
async def setup_2fa_skip(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/", status_code=303)
