import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OAuthConfigEntry:
    id: int
    name: str
    flow_type: str  # "client_credentials" or "delegated"
    azure_tenant_id: str
    client_id: str
    client_secret: str
    sender_email: str
    refresh_token: Optional[str] = None


@dataclass
class ProxyConfig:
    tenant_id: int
    tenant_name: str
    smtp_username: str
    smtp_password: str
    smtp_port: int
    smtp_tls_mode: str  # "starttls" | "tls" | "plain"
    oauth_configs: list[OAuthConfigEntry] = field(default_factory=list)
    admin_api_url: str = ""
    admin_api_key: str = ""
    bf_max_attempts: int = 5
    bf_lockout_minutes: int = 30
    smtp_hostname: str = "smtp.proxy.local"


_config: Optional[ProxyConfig] = None


def load_config() -> ProxyConfig:
    global _config
    config_path = os.environ.get("CONFIG_PATH", "/config/tenant.json")
    with open(config_path) as f:
        raw = json.load(f)

    oauth_list = [OAuthConfigEntry(**o) for o in raw.get("oauth_configs", [])]
    _config = ProxyConfig(
        tenant_id=raw["tenant_id"],
        tenant_name=raw["tenant_name"],
        smtp_username=raw["smtp_username"],
        smtp_password=raw["smtp_password"],
        smtp_port=raw.get("smtp_port", 587),
        smtp_tls_mode=raw.get("smtp_tls_mode", "starttls"),
        oauth_configs=oauth_list,
        admin_api_url=raw.get("admin_api_url", ""),
        admin_api_key=raw.get("admin_api_key", ""),
        bf_max_attempts=int(os.environ.get("SMTP_BF_MAX_ATTEMPTS", raw.get("bf_max_attempts", 5))),
        bf_lockout_minutes=int(os.environ.get("SMTP_BF_LOCKOUT_MINUTES", raw.get("bf_lockout_minutes", 30))),
        smtp_hostname=raw.get("smtp_hostname", "smtp.proxy.local"),
    )
    return _config


def get_config() -> ProxyConfig:
    if _config is None:
        return load_config()
    return _config
