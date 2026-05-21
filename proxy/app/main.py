import asyncio
import logging
import os

import uvicorn

from .api import app as api_app
from .api import set_bf
from .brute_force import BruteForceProtection
from .cert_manager import ensure_cert
from .config import load_config
from .database import init_db
from .smtp_server import create_controller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def run() -> None:
    config = load_config()
    log.info("Starting SMTP proxy for tenant: %s (id=%d)", config.tenant_name, config.tenant_id)

    await init_db()

    cert_path, key_path = ensure_cert(config.smtp_hostname)
    bf = BruteForceProtection(
        max_attempts=config.bf_max_attempts,
        lockout_minutes=config.bf_lockout_minutes,
    )
    set_bf(bf)

    controller = create_controller(config, bf, cert_path, key_path)
    controller.start()
    log.info("SMTP server listening on port %d (mode: %s)", config.smtp_port, config.smtp_tls_mode)

    api_config = uvicorn.Config(api_app, host="0.0.0.0", port=8082, log_level="warning")
    api_server = uvicorn.Server(api_config)

    try:
        await api_server.serve()
    finally:
        controller.stop()
        log.info("SMTP server stopped")


if __name__ == "__main__":
    asyncio.run(run())
