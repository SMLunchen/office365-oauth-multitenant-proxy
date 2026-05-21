import asyncio
import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class _Entry:
    attempts: int = 0
    locked_until: datetime.datetime = field(default_factory=lambda: datetime.datetime.min)


class BruteForceProtection:
    def __init__(self, max_attempts: int = 5, lockout_minutes: int = 30):
        self._max = max_attempts
        self._lockout = datetime.timedelta(minutes=lockout_minutes)
        self._lock = asyncio.Lock()
        self._store: dict[str, _Entry] = defaultdict(_Entry)

    async def is_locked(self, ip: str) -> bool:
        async with self._lock:
            entry = self._store[ip]
            if entry.locked_until > datetime.datetime.utcnow():
                return True
            if entry.locked_until != datetime.datetime.min:
                entry.locked_until = datetime.datetime.min
                entry.attempts = 0
            return False

    async def record_failure(self, ip: str) -> bool:
        async with self._lock:
            entry = self._store[ip]
            entry.attempts += 1
            if entry.attempts >= self._max:
                entry.locked_until = datetime.datetime.utcnow() + self._lockout
                log.warning("IP %s locked out for %d minutes after %d failures", ip, self._lockout.seconds // 60, entry.attempts)
                return True
            return False

    async def record_success(self, ip: str) -> None:
        async with self._lock:
            self._store[ip] = _Entry()

    async def reset_ip(self, ip: str) -> None:
        async with self._lock:
            self._store.pop(ip, None)
        log.info("Brute force record reset for IP %s", ip)

    async def status(self) -> dict:
        async with self._lock:
            now = datetime.datetime.utcnow()
            return {
                ip: {
                    "attempts": e.attempts,
                    "locked": e.locked_until > now,
                    "locked_until": e.locked_until.isoformat() if e.locked_until > now else None,
                }
                for ip, e in self._store.items()
                if e.attempts > 0
            }
