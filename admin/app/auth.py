import datetime
import os

import pyotp
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AdminUser, BruteForceRecord
from .security import (
    create_session_token,
    hash_password,
    verify_password,
    verify_session_token,
)

BF_MAX = int(os.environ.get("ADMIN_BF_MAX_ATTEMPTS", 5))
BF_LOCKOUT = int(os.environ.get("ADMIN_BF_LOCKOUT_MINUTES", 15))


async def get_brute_force(db: AsyncSession, ip: str) -> BruteForceRecord:
    result = await db.execute(
        select(BruteForceRecord).where(
            BruteForceRecord.ip_address == ip,
            BruteForceRecord.endpoint == "admin_login",
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        record = BruteForceRecord(ip_address=ip, endpoint="admin_login")
        db.add(record)
        await db.flush()
    return record


async def is_locked(db: AsyncSession, ip: str) -> bool:
    record = await get_brute_force(db, ip)
    if record.locked_until and record.locked_until > datetime.datetime.utcnow():
        return True
    if record.locked_until and record.locked_until <= datetime.datetime.utcnow():
        record.attempt_count = 0
        record.locked_until = None
        await db.commit()
    return False


async def record_failure(db: AsyncSession, ip: str) -> bool:
    record = await get_brute_force(db, ip)
    record.attempt_count += 1
    record.last_attempt = datetime.datetime.utcnow()
    if record.attempt_count >= BF_MAX:
        record.locked_until = datetime.datetime.utcnow() + datetime.timedelta(minutes=BF_LOCKOUT)
        await db.commit()
        return True
    await db.commit()
    return False


async def record_success(db: AsyncSession, ip: str) -> None:
    # Only lift the lockout — keep attempt_count for audit visibility.
    # Admin can manually reset via the security page.
    record = await get_brute_force(db, ip)
    record.locked_until = None
    await db.commit()


async def reset_ip(db: AsyncSession, ip: str) -> None:
    result = await db.execute(
        select(BruteForceRecord).where(
            BruteForceRecord.ip_address == ip,
            BruteForceRecord.endpoint == "admin_login",
        )
    )
    record = result.scalar_one_or_none()
    if record:
        record.attempt_count = 0
        record.locked_until = None
        await db.commit()


async def get_current_user(request: Request, db: AsyncSession) -> AdminUser | None:
    session_token = request.cookies.get("session_token")
    if not session_token:
        return None
    user_id = verify_session_token(session_token)
    if user_id is None:
        return None
    return await db.get(AdminUser, user_id)


def verify_totp(secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=1)


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str) -> str:
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=username, issuer_name="SMTP OAuth Proxy")
