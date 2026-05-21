import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class MailLog(Base):
    __tablename__ = "mail_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow)
    client_ip: Mapped[str] = mapped_column(String(64))
    mail_from: Mapped[str] = mapped_column(String(255))
    rcpt_tos: Mapped[str] = mapped_column(Text)  # JSON list
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    oauth_config_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oauth_config_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # success | failed | auth_failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_error: Mapped[bool] = mapped_column(Boolean, default=False)
    message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
