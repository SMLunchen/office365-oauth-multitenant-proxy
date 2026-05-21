from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user, reset_ip
from ..database import get_db
from ..models import BruteForceRecord, MailLog, Tenant
from ..templates import templates

router = APIRouter()


@router.get("/logs", response_class=HTMLResponse)
async def all_logs(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=303)

    result = await db.execute(
        select(MailLog, Tenant.name.label("tenant_name"))
        .join(Tenant, MailLog.tenant_id == Tenant.id)
        .order_by(desc(MailLog.timestamp))
        .limit(200)
    )
    logs = result.all()

    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "user": user, "logs": logs},
    )


@router.get("/security", response_class=HTMLResponse)
async def security_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=303)

    result = await db.execute(
        select(BruteForceRecord).order_by(desc(BruteForceRecord.last_attempt))
    )
    records = result.scalars().all()

    from datetime import datetime
    return templates.TemplateResponse(
        "security.html",
        {"request": request, "user": user, "records": records, "now": datetime.utcnow()},
    )


@router.post("/security/reset/{ip}")
async def reset_brute_force(ip: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/login", status_code=303)

    await reset_ip(db, ip)
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/security", status_code=303)


@router.post("/api/proxy/log")
async def receive_proxy_log(
    request: Request,
    x_api_key: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    body = await request.json()
    tenant_id = body.get("tenant_id")
    tenant = await db.get(Tenant, tenant_id)
    if not tenant or tenant.api_key != x_api_key:
        raise HTTPException(status_code=403)

    entry = MailLog(
        tenant_id=tenant_id,
        client_ip=body.get("client_ip", "postfix-pipe"),
        mail_from=body.get("mail_from", ""),
        rcpt_tos=body.get("rcpt_tos", "[]"),
        subject=body.get("subject"),
        status=body.get("status", "unknown"),
        error=body.get("error"),
        oauth_error=bool(body.get("oauth_error", False)),
    )
    db.add(entry)
    await db.commit()
    return {"ok": True}
