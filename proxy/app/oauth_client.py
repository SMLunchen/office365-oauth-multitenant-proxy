import asyncio
import logging
import time
from typing import Optional

import msal

from .config import OAuthConfigEntry

log = logging.getLogger(__name__)

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
SMTP_SCOPE = ["https://outlook.office365.com/SMTP.Send"]

_token_cache: dict[int, dict] = {}
_cache_lock = asyncio.Lock()


async def get_access_token(oauth: OAuthConfigEntry) -> str:
    async with _cache_lock:
        cached = _token_cache.get(oauth.id)
        if cached and cached["expires_at"] > time.time() + 60:
            return cached["token"]

    if oauth.flow_type == "client_credentials":
        token, expires_in = await _client_credentials(oauth)
    elif oauth.flow_type == "delegated":
        token, expires_in = await _delegated(oauth)
    else:
        raise ValueError(f"Unknown OAuth flow type: {oauth.flow_type}")

    async with _cache_lock:
        _token_cache[oauth.id] = {
            "token": token,
            "expires_at": time.time() + expires_in,
        }

    return token


async def _client_credentials(oauth: OAuthConfigEntry) -> tuple[str, int]:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _acquire_cc, oauth)
    if "access_token" not in result:
        raise OAuthError(
            f"Client credentials token failed: {result.get('error')}: {result.get('error_description')}"
        )
    return result["access_token"], result.get("expires_in", 3600)


def _acquire_cc(oauth: OAuthConfigEntry) -> dict:
    app = msal.ConfidentialClientApplication(
        oauth.client_id,
        authority=f"https://login.microsoftonline.com/{oauth.azure_tenant_id}",
        client_credential=oauth.client_secret,
    )
    return app.acquire_token_for_client(scopes=GRAPH_SCOPE)


async def _delegated(oauth: OAuthConfigEntry) -> tuple[str, int]:
    if not oauth.refresh_token:
        raise OAuthError("No refresh token configured for delegated flow")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _acquire_delegated, oauth)
    if "access_token" not in result:
        raise OAuthError(
            f"Delegated token refresh failed: {result.get('error')}: {result.get('error_description')}"
        )
    if result.get("refresh_token") and result["refresh_token"] != oauth.refresh_token:
        oauth.refresh_token = result["refresh_token"]
        log.info("OAuth config %d: refresh token rotated", oauth.id)
    return result["access_token"], result.get("expires_in", 3600)


def _acquire_delegated(oauth: OAuthConfigEntry) -> dict:
    app = msal.ConfidentialClientApplication(
        oauth.client_id,
        authority=f"https://login.microsoftonline.com/{oauth.azure_tenant_id}",
        client_credential=oauth.client_secret,
    )
    return app.acquire_token_by_refresh_token(oauth.refresh_token, scopes=SMTP_SCOPE)


def invalidate_cache(oauth_id: int) -> None:
    _token_cache.pop(oauth_id, None)


class OAuthError(Exception):
    pass
