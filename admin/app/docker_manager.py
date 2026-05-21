import json
import logging
import os
from pathlib import Path

import docker
from docker.errors import APIError, NotFound

from .models import Tenant
from .security import decrypt, tenant_fernet

log = logging.getLogger(__name__)

PROXY_IMAGE = os.environ.get("PROXY_IMAGE_NAME", "smtp-proxy-tenant")
PROXY_NETWORK = os.environ.get("PROXY_NETWORK_NAME", "smtp_proxy_net")
CONFIG_DIR = Path(os.environ.get("TENANT_CONFIG_DIR", "/tenant_configs"))
PORT_MODE = os.environ.get("PORT_MODE_ENABLED", "true").lower() == "true"
SUBDOMAIN_MODE = os.environ.get("SUBDOMAIN_MODE_ENABLED", "false").lower() == "true"

_client: docker.DockerClient | None = None


def get_docker() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def _get_config_volume() -> str:
    """Detect the actual volume name/path for /tenant_configs from the running admin container.
    Docker Compose prefixes volume names with the project name, so we can't hardcode it."""
    client = get_docker()
    try:
        container = client.containers.get("smtp_proxy_admin")
        for mount in container.attrs.get("Mounts", []):
            if mount.get("Destination") == "/tenant_configs":
                if mount.get("Type") == "volume":
                    name = mount["Name"]
                    log.debug("Detected tenant config volume: %s", name)
                    return name
                elif mount.get("Type") == "bind":
                    src = mount["Source"]
                    log.debug("Detected tenant config bind mount: %s", src)
                    return src
    except Exception as e:
        log.warning("Could not detect config volume from admin container: %s — falling back to 'tenant_configs'", e)
    return "tenant_configs"


def _container_name(tenant: Tenant) -> str:
    return f"smtp_proxy_tenant_{tenant.id}"


def write_config_file(tenant: Tenant, smtp_password: str, oauth_configs: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_data = {
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "smtp_username": tenant.smtp_username,
        "smtp_password": smtp_password,
        "smtp_port": 587,
        "smtp_tls_mode": tenant.smtp_tls_mode,
        "smtp_hostname": tenant.smtp_hostname or f"tenant-{tenant.id}.smtp.proxy",
        "oauth_configs": oauth_configs,
        "admin_api_url": f"http://smtp_proxy_admin:8080",
        "admin_api_key": tenant.api_key,
        "bf_max_attempts": int(os.environ.get("SMTP_BF_MAX_ATTEMPTS", 5)),
        "bf_lockout_minutes": int(os.environ.get("SMTP_BF_LOCKOUT_MINUTES", 30)),
    }
    config_path = CONFIG_DIR / f"tenant_{tenant.id}.json"
    config_path.write_text(json.dumps(config_data, indent=2))
    log.info("Config written for tenant %d at %s", tenant.id, config_path)


def start_container(tenant: Tenant) -> str:
    client = get_docker()
    name = _container_name(tenant)

    try:
        client.images.get(f"{PROXY_IMAGE}:latest")
    except NotFound:
        raise RuntimeError(
            f"Proxy image '{PROXY_IMAGE}:latest' not found on this Docker host. "
            f"Build it first on the host with:\n  docker build -t {PROXY_IMAGE}:latest ./proxy"
        )

    try:
        existing = client.containers.get(name)
        if existing.status == "running":
            return existing.id
        existing.remove(force=True)
    except NotFound:
        pass

    port_bindings = {}
    if PORT_MODE and tenant.smtp_port:
        port_bindings = {"587/tcp": tenant.smtp_port}

    labels = {"smtp_proxy_tenant": str(tenant.id)}
    if SUBDOMAIN_MODE and tenant.smtp_hostname:
        base_domain = os.environ.get("SUBDOMAIN_BASE_DOMAIN", "")
        labels.update({
            "traefik.enable": "true",
            f"traefik.tcp.routers.smtp-{tenant.id}.rule": f"HostSNI(`{tenant.smtp_hostname}.{base_domain}`)",
            f"traefik.tcp.routers.smtp-{tenant.id}.tls": "true",
            f"traefik.tcp.services.smtp-{tenant.id}.loadbalancer.server.port": "587",
        })

    container = client.containers.run(
        image=PROXY_IMAGE,
        name=name,
        environment={
            "TENANT_ID": str(tenant.id),
            "CONFIG_PATH": f"/config/tenant_{tenant.id}.json",
            "DATA_DIR": f"/data",
            "CERT_DIR": f"/data/certs",
        },
        volumes={
            _get_config_volume(): {"bind": "/config", "mode": "ro"},
            f"smtp_proxy_tenant_{tenant.id}_data": {"bind": "/data", "mode": "rw"},
        },
        network=PROXY_NETWORK,
        ports=port_bindings,
        labels=labels,
        detach=True,
        restart_policy={"Name": "unless-stopped"},
    )
    log.info("Container %s started (id=%s)", name, container.id[:12])
    return container.id


def stop_container(tenant: Tenant) -> None:
    client = get_docker()
    name = _container_name(tenant)
    try:
        container = client.containers.get(name)
        container.stop(timeout=10)
        log.info("Container %s stopped", name)
    except NotFound:
        pass


def remove_container(tenant: Tenant) -> None:
    client = get_docker()
    name = _container_name(tenant)
    try:
        container = client.containers.get(name)
        container.remove(force=True)
        log.info("Container %s removed", name)
    except NotFound:
        pass


def get_container_status(tenant: Tenant) -> str:
    client = get_docker()
    name = _container_name(tenant)
    try:
        container = client.containers.get(name)
        return container.status  # running | exited | paused | ...
    except NotFound:
        return "not_found"


def get_container_logs(tenant: Tenant, lines: int = 100) -> str:
    client = get_docker()
    name = _container_name(tenant)
    try:
        container = client.containers.get(name)
        return container.logs(tail=lines).decode(errors="replace")
    except NotFound:
        return ""


def get_queue_count(tenant: Tenant) -> int:
    """Return number of messages currently in the Postfix mail queue."""
    client = get_docker()
    name = _container_name(tenant)
    try:
        container = client.containers.get(name)
        result = container.exec_run("mailq -C /etc/postfix", user="root")
        output = result.output.decode(errors="replace")
        if "Mail queue is empty" in output:
            return 0
        import re
        match = re.search(r"(\d+)\s+Request", output)
        if match:
            return int(match.group(1))
        # Count queue ID lines (8-char hex IDs)
        return len(re.findall(r"^[0-9A-F]{8,}", output, re.MULTILINE))
    except Exception:
        return 0


def next_available_port() -> int:
    base = int(os.environ.get("PORT_MODE_BASE_PORT", 10025))
    client = get_docker()
    used_ports: set[int] = set()
    try:
        for c in client.containers.list(all=True, filters={"label": "smtp_proxy_tenant"}):
            for port_info in (c.ports or {}).values():
                if port_info:
                    for p in port_info:
                        used_ports.add(int(p["HostPort"]))
    except APIError:
        pass
    port = base
    while port in used_ports:
        port += 1
    return port
