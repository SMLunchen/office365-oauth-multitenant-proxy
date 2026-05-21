import os
import secrets

import bcrypt
from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer

_fernet: Fernet | None = None
_signer: URLSafeTimedSerializer | None = None


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        raw = os.environ.get("ENCRYPTION_KEY", "")
        if not raw or raw.startswith("changeme"):
            raise RuntimeError(
                "ENCRYPTION_KEY is not set or still uses the placeholder value.\n"
                "Generate a valid key with:\n"
                "  python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
                "Then set ENCRYPTION_KEY=<result> in your .env file."
            )
        try:
            _fernet = Fernet(raw.encode())
        except Exception:
            raise RuntimeError(
                f"ENCRYPTION_KEY is invalid (must be 32 url-safe base64 bytes, got {len(raw)} chars).\n"
                "Generate a valid key with:\n"
                "  python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
    return _fernet


def get_signer() -> URLSafeTimedSerializer:
    global _signer
    if _signer is None:
        _signer = URLSafeTimedSerializer(os.environ["SECRET_KEY"])
    return _signer


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def encrypt(value: str) -> str:
    return get_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return get_fernet().decrypt(value.encode()).decode()


def tenant_fernet(key: str) -> Fernet:
    return Fernet(key.encode())


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def generate_tenant_encryption_key() -> str:
    return Fernet.generate_key().decode()


def create_session_token(user_id: int) -> str:
    return get_signer().dumps({"uid": user_id})


def verify_session_token(token: str, max_age: int = 28800) -> int | None:
    try:
        data = get_signer().loads(token, max_age=max_age)
        return data["uid"]
    except Exception:
        return None
