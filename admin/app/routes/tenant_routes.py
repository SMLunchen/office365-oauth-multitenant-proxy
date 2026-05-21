import json

import msal
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..docker_manager import (
    get_container_logs,
    get_container_status,
    get_queue_count,
    next_available_port,
    remove_container,
    start_container,
    stop_container,
    write_config_file,
)
from ..models import MailLog, OAuthConfig, Tenant
from ..security import (
    decrypt,
    encrypt,
    generate_api_key,
    generate_tenant_encryption_key,
    hash_password,
)
from ..templates import templates

router = APIRouter(prefix="/tenants")


async def _require_user(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        raise RedirectException("/login")
    if user.must_setup_totp:
        raise RedirectException("/setup-2fa")
    return user


class RedirectException(Exception):
    def __init__(self, url: str):
        self.url = url


@router.get("", response_class=HTMLResponse)
async def tenant_list(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    result = await db.execute(select(Tenant).order_by(Tenant.name))
    tenants = result.scalars().all()

    tenants_with_status = []
    for t in tenants:
        status = get_container_status(t)
        tenants_with_status.append({"tenant": t, "status": status})

    return templates.TemplateResponse(
        "tenants/list.html",
        {"request": request, "user": user, "tenants": tenants_with_status},
    )


@router.get("/new", response_class=HTMLResponse)
async def tenant_new(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    next_port = next_available_port()
    return templates.TemplateResponse(
        "tenants/create_edit.html",
        {"request": request, "user": user, "tenant": None, "oauth_configs": [], "next_port": next_port, "error": None},
    )


@router.get("/{tenant_id}/edit", response_class=HTMLResponse)
async def tenant_edit_page(tenant_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        return RedirectResponse("/tenants", status_code=303)
    result = await db.execute(select(OAuthConfig).where(OAuthConfig.tenant_id == tenant_id))
    oauth_configs = result.scalars().all()
    return templates.TemplateResponse(
        "tenants/create_edit.html",
        {
            "request": request,
            "user": user,
            "tenant": tenant,
            "oauth_configs": oauth_configs,
            "next_port": tenant.smtp_port,
            "error": None,
        },
    )


@router.post("/{tenant_id}/edit")
async def tenant_edit(
    tenant_id: int,
    request: Request,
    description: str = Form(default=""),
    smtp_username: str = Form(...),
    smtp_password: str = Form(default=""),
    smtp_port: int = Form(...),
    smtp_tls_mode: str = Form(default="starttls"),
    smtp_hostname: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        return RedirectResponse("/tenants", status_code=303)

    tenant.description = description or None
    tenant.smtp_username = smtp_username
    tenant.smtp_port = smtp_port
    tenant.smtp_tls_mode = smtp_tls_mode
    tenant.smtp_hostname = smtp_hostname or None
    if smtp_password:
        tenant.smtp_password_enc = encrypt(smtp_password)

    await db.commit()

    # Always regenerate the config file so it's ready for the next start
    result = await db.execute(
        select(OAuthConfig).where(OAuthConfig.tenant_id == tenant_id, OAuthConfig.is_active == True)
    )
    active_oauth = result.scalars().all()
    oauth_list = [
        {
            "id": o.id,
            "name": o.name,
            "flow_type": o.flow_type,
            "azure_tenant_id": o.azure_tenant_id,
            "client_id": o.client_id,
            "client_secret": decrypt(o.client_secret_enc),
            "sender_email": o.sender_email,
            "refresh_token": decrypt(o.refresh_token_enc) if o.refresh_token_enc else None,
        }
        for o in active_oauth
    ]
    smtp_password_plain = decrypt(tenant.smtp_password_enc)
    write_config_file(tenant, smtp_password_plain, oauth_list)

    # Restart the container only if it was already running
    status = get_container_status(tenant)
    if status == "running":
        stop_container(tenant)
        container_id = start_container(tenant)
        tenant.container_id = container_id
        tenant.container_status = "running"
        await db.commit()
        return RedirectResponse(f"/tenants/{tenant_id}?started=1", status_code=303)

    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/new")
async def tenant_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    smtp_username: str = Form(...),
    smtp_password: str = Form(...),
    smtp_port: int = Form(...),
    smtp_tls_mode: str = Form(default="starttls"),
    smtp_hostname: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant = Tenant(
        name=name,
        description=description or None,
        smtp_username=smtp_username,
        smtp_password_enc=encrypt(smtp_password),
        smtp_port=smtp_port,
        smtp_tls_mode=smtp_tls_mode,
        smtp_hostname=smtp_hostname or None,
        api_key=generate_api_key(),
        encryption_key=generate_tenant_encryption_key(),
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return RedirectResponse(f"/tenants/{tenant.id}", status_code=303)


@router.get("/{tenant_id}", response_class=HTMLResponse)
async def tenant_detail(tenant_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        return RedirectResponse("/tenants", status_code=303)

    result = await db.execute(select(OAuthConfig).where(OAuthConfig.tenant_id == tenant_id))
    oauth_configs = result.scalars().all()

    result = await db.execute(
        select(MailLog)
        .where(MailLog.tenant_id == tenant_id)
        .order_by(MailLog.timestamp.desc())
        .limit(50)
    )
    logs = result.scalars().all()

    status = get_container_status(tenant)
    container_logs = get_container_logs(tenant, lines=50) if status == "running" else ""
    queue_count = get_queue_count(tenant) if status == "running" else 0
    smtp_password = decrypt(tenant.smtp_password_enc)
    just_started = request.query_params.get("started") == "1"

    return templates.TemplateResponse(
        "tenants/detail.html",
        {
            "request": request,
            "user": user,
            "tenant": tenant,
            "smtp_password": smtp_password,
            "oauth_configs": oauth_configs,
            "logs": logs,
            "status": status,
            "container_logs": container_logs,
            "queue_count": queue_count,
            "error": None,
            "success": None,
            "just_started": just_started,
        },
    )


@router.post("/{tenant_id}/start")
async def tenant_start(tenant_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        return RedirectResponse("/tenants", status_code=303)

    result = await db.execute(select(OAuthConfig).where(OAuthConfig.tenant_id == tenant_id, OAuthConfig.is_active == True))
    oauth_configs = result.scalars().all()

    oauth_list = [
        {
            "id": o.id,
            "name": o.name,
            "flow_type": o.flow_type,
            "azure_tenant_id": o.azure_tenant_id,
            "client_id": o.client_id,
            "client_secret": decrypt(o.client_secret_enc),
            "sender_email": o.sender_email,
            "refresh_token": decrypt(o.refresh_token_enc) if o.refresh_token_enc else None,
        }
        for o in oauth_configs
    ]

    smtp_password = decrypt(tenant.smtp_password_enc)
    write_config_file(tenant, smtp_password, oauth_list)

    error_msg = None
    try:
        container_id = start_container(tenant)
        tenant.container_id = container_id
        tenant.container_name = f"smtp_proxy_tenant_{tenant.id}"
        tenant.container_status = "running"
        await db.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("Failed to start container: %s", exc)
        error_msg = str(exc)

    if error_msg:
        result2 = await db.execute(select(OAuthConfig).where(OAuthConfig.tenant_id == tenant_id))
        all_oauth = result2.scalars().all()
        result3 = await db.execute(
            select(MailLog).where(MailLog.tenant_id == tenant_id).order_by(MailLog.timestamp.desc()).limit(50)
        )
        logs = result3.scalars().all()
        status = get_container_status(tenant)
        return templates.TemplateResponse(
            "tenants/detail.html",
            {
                "request": request,
                "user": user,
                "tenant": tenant,
                "smtp_password": smtp_password,
                "oauth_configs": all_oauth,
                "logs": logs,
                "status": status,
                "container_logs": "",
                "error": error_msg,
                "success": None,
            },
        )

    return RedirectResponse(f"/tenants/{tenant_id}?started=1", status_code=303)


@router.post("/{tenant_id}/stop")
async def tenant_stop(tenant_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant = await db.get(Tenant, tenant_id)
    if tenant:
        stop_container(tenant)
        tenant.container_status = "stopped"
        await db.commit()

    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/{tenant_id}/delete")
async def tenant_delete(tenant_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    tenant = await db.get(Tenant, tenant_id)
    if tenant:
        remove_container(tenant)
        await db.delete(tenant)
        await db.commit()

    return RedirectResponse("/tenants", status_code=303)


# ── OAuth Config CRUD ──────────────────────────────────────────────────────────

@router.post("/{tenant_id}/oauth/add")
async def oauth_add(
    tenant_id: int,
    request: Request,
    name: str = Form(...),
    flow_type: str = Form(...),
    azure_tenant_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    sender_email: str = Form(...),
    refresh_token: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    oauth = OAuthConfig(
        tenant_id=tenant_id,
        name=name,
        flow_type=flow_type,
        azure_tenant_id=azure_tenant_id,
        client_id=client_id,
        client_secret_enc=encrypt(client_secret),
        sender_email=sender_email,
        refresh_token_enc=encrypt(refresh_token) if refresh_token else None,
    )
    db.add(oauth)
    await db.commit()
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.post("/{tenant_id}/oauth/{oauth_id}/delete")
async def oauth_delete(tenant_id: int, oauth_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    oauth = await db.get(OAuthConfig, oauth_id)
    if oauth and oauth.tenant_id == tenant_id:
        await db.delete(oauth)
        await db.commit()
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)


@router.get("/{tenant_id}/oauth/device-code", response_class=HTMLResponse)
async def device_code_start(tenant_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Initiate Device Code Flow for delegated OAuth - user visits Microsoft login."""
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    azure_tenant_id = request.query_params.get("azure_tenant_id", "common")
    client_id = request.query_params.get("client_id", "")

    if not client_id:
        return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{azure_tenant_id}",
    )
    flow = app.initiate_device_flow(scopes=["https://outlook.office365.com/SMTP.Send"])

    return templates.TemplateResponse(
        "tenants/device_code.html",
        {
            "request": request,
            "user": user,
            "tenant_id": tenant_id,
            "flow": flow,
            "client_id": client_id,
            "azure_tenant_id": azure_tenant_id,
            "user_code": flow.get("user_code"),
            "verification_uri": flow.get("verification_uri"),
            "message": flow.get("message"),
            "flow_json": json.dumps(flow),
        },
    )


@router.post("/{tenant_id}/oauth/device-code/complete")
async def device_code_complete(
    tenant_id: int,
    request: Request,
    flow_json: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    azure_tenant_id: str = Form(...),
    sender_email: str = Form(...),
    name: str = Form(default="Delegated Account"),
    db: AsyncSession = Depends(get_db),
):
    user = await get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)

    flow = json.loads(flow_json)
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{azure_tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_by_device_flow(flow)

    if "refresh_token" not in result:
        return templates.TemplateResponse(
            "tenants/device_code.html",
            {
                "request": request,
                "user": user,
                "tenant_id": tenant_id,
                "error": f"Failed: {result.get('error_description', 'Unknown error')}",
                "flow": flow,
                "client_id": client_id,
                "azure_tenant_id": azure_tenant_id,
                "flow_json": flow_json,
                "user_code": flow.get("user_code"),
                "verification_uri": flow.get("verification_uri"),
            },
        )

    oauth = OAuthConfig(
        tenant_id=tenant_id,
        name=name,
        flow_type="delegated",
        azure_tenant_id=azure_tenant_id,
        client_id=client_id,
        client_secret_enc=encrypt(client_secret),
        sender_email=sender_email,
        refresh_token_enc=encrypt(result["refresh_token"]),
    )
    db.add(oauth)
    await db.commit()
    return RedirectResponse(f"/tenants/{tenant_id}", status_code=303)
