import asyncio
import email as email_lib
import email.policy
import json
import logging
import ssl

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

from .brute_force import BruteForceProtection
from .config import ProxyConfig
from .database import SessionLocal
from .mail_relay import RelayError, relay_email
from .models import MailLog
from .oauth_client import OAuthError

log = logging.getLogger(__name__)

# TLS mode reference:
#   starttls          – STARTTLS offered, TLS required before AUTH
#   starttls_optional – STARTTLS offered, plain AUTH also accepted (old devices)
#   tls               – Direct TLS / SMTPS (port 465 style)
#   plain             – No TLS (insecure, isolated networks only)

# Peer-based auth tracking: {(ip, port): True}
# Used as fallback because aiosmtpd's session.authenticated is not reliably
# readable from handler hooks in all 1.4.x minor versions.
_authed_peers: dict[tuple, bool] = {}


def _is_tls_active(session) -> bool:
    return bool(getattr(session, "tls_handshaked", False))


def _mark_authed(session) -> None:
    _authed_peers[session.peer] = True
    try:
        session._proxy_authed = True
    except AttributeError:
        pass  # __slots__ on SMTPSession — use peer dict instead


def _is_authed(session) -> bool:
    # Check our own flag first, fall back to aiosmtpd's session.authenticated
    if getattr(session, "_proxy_authed", False):
        return True
    if _authed_peers.get(session.peer):
        return True
    # Last resort: trust aiosmtpd's own auth tracking
    auth = getattr(session, "authenticated", None)
    return bool(auth) and auth is not False


def _clear_authed(session) -> None:
    _authed_peers.pop(session.peer, None)


class SMTPProxyHandler:
    def __init__(self, config: ProxyConfig, bf: BruteForceProtection):
        self.config = config
        self.bf = bf

    async def handle_QUIT(self, server, session, envelope):
        _clear_authed(session)
        return "221 Bye"

    async def handle_DATA(self, server, session, envelope):
        if not _is_authed(session):
            log.warning("DATA rejected: unauthenticated peer %s", session.peer)
            return "530 5.7.0 Authentication required"

        config = self.config
        if not config.oauth_configs:
            log.error("No OAuth configs for tenant %d", config.tenant_id)
            return "451 4.3.5 Server configuration error"

        active_oauth = config.oauth_configs[0]

        msg = email_lib.message_from_bytes(envelope.content, policy=email_lib.policy.default)
        subject = msg.get("Subject")
        message_id = msg.get("Message-ID")

        async with SessionLocal() as db:
            mail_log = MailLog(
                client_ip=session.peer[0],
                mail_from=envelope.mail_from,
                rcpt_tos=json.dumps(envelope.rcpt_tos),
                subject=subject,
                oauth_config_id=active_oauth.id,
                oauth_config_name=active_oauth.name,
                status="pending",
                size_bytes=len(envelope.content),
                message_id=message_id,
            )
            db.add(mail_log)
            await db.commit()
            await db.refresh(mail_log)
            log_id = mail_log.id

        try:
            await relay_email(envelope.mail_from, envelope.rcpt_tos, envelope.content, active_oauth)
            async with SessionLocal() as db:
                entry = await db.get(MailLog, log_id)
                entry.status = "success"
                await db.commit()
            await _report_log_to_admin(config, log_id, "success")
            log.info("Mail from %s relayed via %s", envelope.mail_from, active_oauth.flow_type)
            return "250 2.0.0 OK"

        except OAuthError as exc:
            log.error("OAuth error: %s", exc)
            async with SessionLocal() as db:
                entry = await db.get(MailLog, log_id)
                entry.status = "failed"
                entry.error = str(exc)
                entry.oauth_error = True
                await db.commit()
            await _report_log_to_admin(config, log_id, "failed")
            return "451 4.7.0 OAuth error, try again later"

        except RelayError as exc:
            log.error("Relay error: %s", exc)
            async with SessionLocal() as db:
                entry = await db.get(MailLog, log_id)
                entry.status = "failed"
                entry.error = str(exc)
                await db.commit()
            await _report_log_to_admin(config, log_id, "failed")
            return "451 4.4.0 Temporary relay failure"

        except Exception as exc:
            log.exception("Unexpected error relaying mail")
            async with SessionLocal() as db:
                entry = await db.get(MailLog, log_id)
                entry.status = "failed"
                entry.error = str(exc)
                await db.commit()
            return "451 4.3.0 Internal server error"


async def _report_log_to_admin(config: ProxyConfig, log_id: int, status: str) -> None:
    if not config.admin_api_url:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                f"{config.admin_api_url}/api/proxy/log",
                json={"tenant_id": config.tenant_id, "log_id": log_id, "status": status},
                headers={"X-API-Key": config.admin_api_key},
            )
    except Exception:
        pass


class SMTPAuthenticator:
    def __init__(self, config: ProxyConfig, bf: BruteForceProtection):
        self.config = config
        self.bf = bf

    async def __call__(self, server, session, envelope, mechanism, auth_data):
        ip = session.peer[0]

        # TLS enforcement for "starttls" mode (not optional)
        if self.config.smtp_tls_mode == "starttls" and not _is_tls_active(session):
            log.warning("AUTH rejected (no TLS) from %s", ip)
            return AuthResult(
                success=False,
                handled=True,
                message="538 5.7.11 Encryption required for requested authentication mechanism",
            )

        if await self.bf.is_locked(ip):
            log.warning("AUTH rejected (locked) from %s", ip)
            return AuthResult(success=False, handled=True, message="421 4.7.0 Too many failed attempts, try later")

        if mechanism not in ("LOGIN", "PLAIN"):
            return AuthResult(success=False, handled=True, message="504 5.5.4 Unrecognized authentication type")

        raw_login = auth_data.login
        raw_password = auth_data.password
        login = raw_login.decode() if isinstance(raw_login, bytes) else (raw_login or "")
        password = raw_password.decode() if isinstance(raw_password, bytes) else (raw_password or "")

        if not login or not password:
            await self.bf.record_failure(ip)
            log.warning("AUTH rejected (empty credentials) from %s", ip)
            return AuthResult(success=False, handled=True, message="535 5.7.8 Authentication credentials invalid")

        if login == self.config.smtp_username and password == self.config.smtp_password:
            await self.bf.record_success(ip)
            _mark_authed(session)
            log.info("AUTH success from %s user=%s tls=%s", ip, login, _is_tls_active(session))
            return AuthResult(success=True)

        locked = await self.bf.record_failure(ip)
        log.warning("AUTH failure from %s user=%s locked=%s", ip, login, locked)
        return AuthResult(success=False, handled=True, message="535 5.7.8 Authentication credentials invalid")


def build_ssl_context(cert_path, key_path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_path), str(key_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def create_controller(config: ProxyConfig, bf: BruteForceProtection, cert_path, key_path) -> Controller:
    handler = SMTPProxyHandler(config, bf)
    authenticator = SMTPAuthenticator(config, bf)
    ssl_ctx = build_ssl_context(cert_path, key_path)
    mode = config.smtp_tls_mode

    # auth_required=True: aiosmtpd rejects MAIL FROM for unauthenticated sessions.
    # auth_require_tls=False: we enforce TLS ourselves in SMTPAuthenticator
    # (aiosmtpd's default auth_require_tls=True would block all auth without TLS).
    common = dict(
        hostname="0.0.0.0",
        port=config.smtp_port,
        authenticator=authenticator,
        auth_required=True,
        auth_require_tls=False,
    )

    if mode == "tls":
        return Controller(handler, ssl_context=ssl_ctx, **common)
    elif mode == "plain":
        return Controller(handler, **common)
    else:
        return Controller(handler, tls_context=ssl_ctx, **common)
