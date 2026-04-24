from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy import Boolean, DateTime, String, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid6.uuid7)
    clerk_user_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    subscription_status: Mapped[str] = mapped_column(
        String(32), default="inactive", server_default="inactive"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
