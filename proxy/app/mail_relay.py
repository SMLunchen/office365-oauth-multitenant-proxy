import asyncio
import base64
import email as email_lib
import email.policy
import json
import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import OAuthConfigEntry
from .oauth_client import OAuthError, get_access_token

log = logging.getLogger(__name__)


async def relay_email(mail_from: str, rcpt_tos: list[str], content: bytes, oauth: OAuthConfigEntry) -> None:
    token = await get_access_token(oauth)
    if oauth.flow_type == "delegated":
        await _relay_smtp_xoauth2(mail_from, rcpt_tos, content, oauth.sender_email, token)
    else:
        await _relay_graph_api(mail_from, rcpt_tos, content, oauth.sender_email, token)


async def _relay_smtp_xoauth2(
    mail_from: str, rcpt_tos: list[str], content: bytes, sender_email: str, token: str
) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _smtp_xoauth2_sync, mail_from, rcpt_tos, content, sender_email, token
    )


def _smtp_xoauth2_sync(
    mail_from: str, rcpt_tos: list[str], content: bytes, sender_email: str, token: str
) -> None:
    auth_string = f"user={sender_email}\x01auth=Bearer {token}\x01\x01"
    auth_b64 = base64.b64encode(auth_string.encode()).decode()

    with smtplib.SMTP("smtp.office365.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.docmd("AUTH", f"XOAUTH2 {auth_b64}")
        smtp.sendmail(sender_email, rcpt_tos, content)
    log.info("Email relayed via SMTP XOAUTH2 to %s", rcpt_tos)


async def _relay_graph_api(
    mail_from: str, rcpt_tos: list[str], content: bytes, sender_email: str, token: str
) -> None:
    msg = email_lib.message_from_bytes(content, policy=email_lib.policy.default)
    body_text, body_type = _extract_body(msg)

    payload = {
        "message": {
            "subject": msg.get("Subject", "(no subject)"),
            "body": {"contentType": "HTML" if body_type == "html" else "Text", "content": body_text},
            "from": {"emailAddress": {"address": sender_email}},
            "toRecipients": [{"emailAddress": {"address": r}} for r in rcpt_tos],
        }
    }

    attachments = _extract_attachments(msg)
    if attachments:
        payload["message"]["attachments"] = attachments

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            content=json.dumps(payload),
        )
        if resp.status_code not in (200, 202):
            raise RelayError(f"Graph API error {resp.status_code}: {resp.text[:500]}")

    log.info("Email relayed via Graph API to %s", rcpt_tos)


def _extract_body(msg) -> tuple[str, str]:
    if msg.is_multipart():
        html_part = None
        text_part = None
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and html_part is None:
                html_part = part.get_content()
            elif ct == "text/plain" and text_part is None:
                text_part = part.get_content()
        if html_part:
            return html_part, "html"
        if text_part:
            return text_part, "text"
    else:
        ct = msg.get_content_type()
        content = msg.get_content()
        return content, "html" if ct == "text/html" else "text"
    return "", "text"


def _extract_attachments(msg) -> list[dict]:
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                data = part.get_payload(decode=True)
                if data:
                    attachments.append({
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": part.get_filename() or "attachment",
                        "contentType": part.get_content_type(),
                        "contentBytes": base64.b64encode(data).decode(),
                    })
    return attachments


class RelayError(Exception):
    pass
