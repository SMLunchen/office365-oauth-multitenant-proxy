import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    last_login: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    must_setup_totp: Mapped[bool] = mapped_column(Boolean, default=True)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    smtp_username: Mapped[str] = mapped_column(String(128), unique=True)
    smtp_password_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_tls_mode: Mapped[str] = mapped_column(String(16), default="starttls")
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    container_status: Mapped[str] = mapped_column(String(32), default="not_created")
    api_key: Mapped[str] = mapped_column(String(64))
    encryption_key: Mapped[str] = mapped_column(String(64))  # Per-tenant Fernet key for config file
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    oauth_configs: Mapped[list["OAuthConfig"]] = relationship("OAuthConfig", back_populates="tenant", cascade="all, delete-orphan")
    mail_logs: Mapped[list["MailLog"]] = relationship("MailLog", back_populates="tenant", cascade="all, delete-orphan")


class OAuthConfig(Base):
    __tablename__ = "oauth_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    flow_type: Mapped[str] = mapped_column(String(32))  # client_credentials | delegated
    azure_tenant_id: Mapped[str] = mapped_column(String(64))
    client_id: Mapped[str] = mapped_column(String(64))
    client_secret_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender_email: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="oauth_configs")


class MailLog(Base):
    __tablename__ = "mail_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"))
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    client_ip: Mapped[str] = mapped_column(String(64))
    mail_from: Mapped[str] = mapped_column(String(255))
    rcpt_tos: Mapped[str] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_error: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="mail_logs")


class BruteForceRecord(Base):
    __tablename__ = "brute_force_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip_address: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(64))  # "admin_login"
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (UniqueConstraint("ip_address", "endpoint", name="uq_ip_endpoint"),)
