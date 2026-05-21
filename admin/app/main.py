import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .auth import get_current_user
from .database import SessionLocal, get_db, init_db
from .models import AdminUser, MailLog, Tenant
from .routes.auth_routes import router as auth_router
from .routes.log_routes import router as log_router
from .routes.tenant_routes import router as tenant_router
from .security import hash_password
from .templates import templates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="SMTP OAuth Proxy Admin", docs_url=None, redoc_url=None)

from pathlib import Path
static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth_router)
app.include_router(tenant_router)
app.include_router(log_router)


@app.on_event("startup")
async def startup():
    await init_db()
    await _ensure_admin_user()


async def _ensure_admin_user() -> None:
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        log.error("ADMIN_PASSWORD not set!")
        return

    async with SessionLocal() as db:
        result = await db.execute(select(AdminUser).where(AdminUser.username == username))
        user = result.scalar_one_or_none()
        if user is None:
            user = AdminUser(
                username=username,
                password_hash=hash_password(password),
                must_setup_totp=True,
            )
            db.add(user)
            await db.commit()
            log.info("Admin user '%s' created", username)
        else:
            log.info("Admin user '%s' already exists", username)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    async with SessionLocal() as db:
        user = await get_current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)

        result = await db.execute(select(Tenant).order_by(Tenant.name))
        tenants = result.scalars().all()

        result = await db.execute(
            select(MailLog).order_by(MailLog.timestamp.desc()).limit(10)
        )
        recent_logs = result.scalars().all()

        from .docker_manager import get_container_status, get_queue_count
        tenant_statuses = []
        for t in tenants:
            st = get_container_status(t)
            tenant_statuses.append({
                "tenant": t,
                "status": st,
                "queue": get_queue_count(t) if st == "running" else 0,
            })
        running = sum(1 for ts in tenant_statuses if ts["status"] == "running")

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "user": user,
                "tenant_count": len(tenants),
                "running_count": running,
                "tenant_statuses": tenant_statuses,
                "recent_logs": recent_logs,
            },
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8080, reload=False)
