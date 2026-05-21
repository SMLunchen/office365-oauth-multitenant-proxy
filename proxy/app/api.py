"""Internal management API - only accessible within the Docker network."""
import os
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Header
from sqlalchemy import desc, select

from .brute_force import BruteForceProtection
from .config import get_config
from .database import SessionLocal
from .models import MailLog

app = FastAPI(docs_url=None, redoc_url=None)

_bf_instance: Optional[BruteForceProtection] = None


def set_bf(bf: BruteForceProtection) -> None:
    global _bf_instance
    _bf_instance = bf


def _verify_key(x_api_key: str = Header(...)):
    expected = get_config().admin_api_key
    if not expected or x_api_key != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@app.get("/health")
async def health():
    cfg = get_config()
    return {"status": "ok", "tenant_id": cfg.tenant_id, "tenant_name": cfg.tenant_name}


@app.get("/logs", dependencies=[Depends(_verify_key)])
async def get_logs(limit: int = 100, offset: int = 0):
    async with SessionLocal() as db:
        result = await db.execute(
            select(MailLog).order_by(desc(MailLog.timestamp)).offset(offset).limit(limit)
        )
        logs = result.scalars().all()
    return [
        {
            "id": l.id,
            "timestamp": l.timestamp.isoformat(),
            "client_ip": l.client_ip,
            "mail_from": l.mail_from,
            "rcpt_tos": l.rcpt_tos,
            "subject": l.subject,
            "status": l.status,
            "error": l.error,
            "oauth_error": l.oauth_error,
            "size_bytes": l.size_bytes,
        }
        for l in logs
    ]


@app.get("/brute-force", dependencies=[Depends(_verify_key)])
async def get_brute_force():
    if _bf_instance is None:
        return {}
    return await _bf_instance.status()


@app.post("/brute-force/reset/{ip}", dependencies=[Depends(_verify_key)])
async def reset_brute_force(ip: str):
    if _bf_instance is None:
        raise HTTPException(status_code=503, detail="BF not initialized")
    await _bf_instance.reset_ip(ip)
    return {"reset": ip}
