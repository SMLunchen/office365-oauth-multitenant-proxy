#!/usr/bin/env python3
"""
Postfix pipe transport MDA.
Called by Postfix for each message: stdin = RFC 822 mail, argv = sender recipients.
Exit 0 = delivered, 1 = deferred (Postfix retries), 2 = bounced (permanent).
"""

import base64
import email
import email.policy
import json
import logging
import os
import smtplib
import sys

import httpx
import msal

# Fixed local path written by configure.py at container startup.
# Postfix pipe (running as nobody) does not inherit container env vars,
# so we cannot use CONFIG_PATH env var here.
CONFIG_PATH = "/etc/oauth_proxy_config.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("oauth_deliver")

EX_OK = 0
EX_TEMPFAIL = 1   # Postfix defers and retries
EX_UNAVAILABLE = 2  # Postfix bounces


class OAuthError(Exception):
    pass


class RelayError(Exception):
    pass


# ── Token acquisition ──────────────────────────────────────────────────────────

def get_token(oauth: dict) -> str:
    if oauth.get("flow_type") == "delegated":
        return _token_delegated(oauth)
    return _token_client_credentials(oauth)


def _token_client_credentials(oauth: dict) -> str:
    app = msal.ConfidentialClientApplication(
        oauth["client_id"],
        authority=f"https://login.microsoftonline.com/{oauth['azure_tenant_id']}",
        client_credential=oauth["client_secret"],
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise OAuthError(f"{result.get('error')}: {result.get('error_description')}")
    return result["access_token"]


def _token_delegated(oauth: dict) -> str:
    if not oauth.get("refresh_token"):
        raise OAuthError("No refresh_token configured for delegated flow")
    app = msal.ConfidentialClientApplication(
        oauth["client_id"],
        authority=f"https://login.microsoftonline.com/{oauth['azure_tenant_id']}",
        client_credential=oauth["client_secret"],
    )
    result = app.acquire_token_by_refresh_token(
        oauth["refresh_token"],
        scopes=["https://outlook.office365.com/SMTP.Send"],
    )
    if "access_token" not in result:
        raise OAuthError(f"{result.get('error')}: {result.get('error_description')}")
    return result["access_token"]


# ── Delivery ───────────────────────────────────────────────────────────────────

def relay_smtp_xoauth2(
    sender: str, recipients: list[str], content: bytes, oauth: dict, token: str
) -> None:
    sender_email = oauth["sender_email"]
    auth_str = f"user={sender_email}\x01auth=Bearer {token}\x01\x01"
    auth_b64 = base64.b64encode(auth_str.encode()).decode()

    with smtplib.SMTP("smtp.office365.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        code, reply = smtp.docmd("AUTH", f"XOAUTH2 {auth_b64}")
        if code != 235:
            raise RelayError(f"XOAUTH2 auth failed: {code} {reply.decode(errors='replace')}")
        smtp.sendmail(sender_email, recipients, content)
    log.info("Relayed via SMTP XOAUTH2: %s → %s", sender_email, recipients)


def relay_graph_api(
    sender: str, recipients: list[str], content: bytes, oauth: dict, token: str
) -> None:
    sender_email = oauth["sender_email"]
    msg = email.message_from_bytes(content, policy=email.policy.default)

    payload = {
        "message": {
            "subject": msg.get("Subject", "(no subject)"),
            "body": _extract_body(msg),
            "from": {"emailAddress": {"address": sender_email}},
            "toRecipients": [{"emailAddress": {"address": r}} for r in recipients],
        }
    }
    attachments = _extract_attachments(msg)
    if attachments:
        payload["message"]["attachments"] = attachments

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
    if resp.status_code not in (200, 202):
        raise RelayError(f"Graph API {resp.status_code}: {resp.text[:300]}")
    log.info("Relayed via Graph API: %s → %s", sender_email, recipients)


def _extract_body(msg) -> dict:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                return {"contentType": "HTML", "content": part.get_content()}
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return {"contentType": "Text", "content": part.get_content()}
    ct = msg.get_content_type()
    try:
        body = msg.get_content() if not msg.is_multipart() else ""
    except Exception:
        body = ""
    return {"contentType": "HTML" if ct == "text/html" else "Text", "content": body}


def _extract_attachments(msg) -> list[dict]:
    result = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                data = part.get_payload(decode=True)
                if data:
                    result.append({
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": part.get_filename() or "attachment",
                        "contentType": part.get_content_type(),
                        "contentBytes": base64.b64encode(data).decode(),
                    })
    return result


# ── Admin reporting ────────────────────────────────────────────────────────────

def report_to_admin(
    config: dict,
    status: str,
    mail_from: str,
    rcpt_tos: list[str],
    subject: str | None,
    error: str | None = None,
    oauth_error: bool = False,
) -> None:
    admin_url = config.get("admin_api_url", "")
    api_key = config.get("admin_api_key", "")
    if not admin_url:
        return
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.post(
                f"{admin_url}/api/proxy/log",
                json={
                    "tenant_id": config["tenant_id"],
                    "status": status,
                    "mail_from": mail_from,
                    "rcpt_tos": json.dumps(rcpt_tos),
                    "subject": subject,
                    "error": error,
                    "oauth_error": oauth_error,
                    "client_ip": "postfix-pipe",
                },
                headers={"X-API-Key": api_key},
            )
        if resp.status_code != 200:
            log.warning("Admin log report failed: HTTP %s — %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.warning("Admin log report error: %s", exc)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    if len(sys.argv) < 3:
        log.error("Usage: oauth_deliver.py <sender> <recipient> [...]")
        return EX_UNAVAILABLE

    sender = sys.argv[1]
    recipients = sys.argv[2:]
    content = sys.stdin.buffer.read()

    # Parse subject for logging
    try:
        msg = email.message_from_bytes(content, policy=email.policy.default)
        subject = msg.get("Subject")
    except Exception:
        subject = None

    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except Exception as e:
        log.error("Config read error: %s", e)
        return EX_TEMPFAIL

    oauth_configs = [o for o in config.get("oauth_configs", []) if o.get("sender_email")]
    if not oauth_configs:
        log.error("No OAuth configs for tenant %s", config.get("tenant_name"))
        report_to_admin(config, "failed", sender, recipients, subject,
                        "No OAuth configs configured")
        return EX_TEMPFAIL

    oauth = oauth_configs[0]
    flow = oauth.get("flow_type", "client_credentials")
    log.info("Delivering %s → %s via %s", sender, recipients, flow)

    try:
        token = get_token(oauth)
    except OAuthError as e:
        log.error("OAuth token error: %s", e)
        report_to_admin(config, "failed", sender, recipients, subject,
                        str(e), oauth_error=True)
        return EX_TEMPFAIL

    try:
        if flow == "delegated":
            relay_smtp_xoauth2(sender, recipients, content, oauth, token)
        else:
            relay_graph_api(sender, recipients, content, oauth, token)

        report_to_admin(config, "success", sender, recipients, subject)
        return EX_OK

    except RelayError as e:
        log.error("Relay error: %s", e)
        report_to_admin(config, "failed", sender, recipients, subject, str(e))
        return EX_TEMPFAIL

    except Exception as e:
        log.exception("Unexpected delivery error")
        report_to_admin(config, "failed", sender, recipients, subject, str(e))
        return EX_TEMPFAIL


if __name__ == "__main__":
    sys.exit(main())
