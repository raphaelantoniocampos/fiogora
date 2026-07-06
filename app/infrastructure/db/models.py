from typing import Optional
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Boolean
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.domain.enums import AutomationTaskStatus, AutomationTaskType, SyncStatus


class SyncJobModel(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    status: Mapped[SyncStatus] = mapped_column(String, default=SyncStatus.PENDING)
    triggered_by: Mapped[str] = mapped_column(String, default="system")
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metadata_info: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    retry_count: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    logs: Mapped[list["SyncLogModel"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    automation_tasks: Mapped[list["AutomationTaskModel"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    user: Mapped[Optional["UserModel"]] = relationship()


class SyncLogModel(Base):
    __tablename__ = "sync_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("sync_jobs.id"))
    task_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("automation_tasks.id"), nullable=True)
    level: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    job: Mapped["SyncJobModel"] = relationship(back_populates="logs")
    task: Mapped[Optional["AutomationTaskModel"]] = relationship(back_populates="logs")


class AutomationTaskModel(Base):
    __tablename__ = "automation_tasks"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    job_id: Mapped[UUID] = mapped_column(ForeignKey("sync_jobs.id"))
    type: Mapped[AutomationTaskType] = mapped_column(String)
    status: Mapped[AutomationTaskStatus] = mapped_column(
        String, default=AutomationTaskStatus.PENDING
    )
    payload_info: Mapped[dict] = mapped_column("payload", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(default=0)

    job: Mapped["SyncJobModel"] = relationship(back_populates="automation_tasks")
    logs: Mapped[list["SyncLogModel"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class GlobalSettingsModel(Base):
    __tablename__ = "global_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    fiorilli_url: Mapped[Optional[str]] = mapped_column(String, default="")
    ahgora_url: Mapped[Optional[str]] = mapped_column(String, default="")
    ahgora_company: Mapped[Optional[str]] = mapped_column(String, default="")


class AhgoraEmployeeModel(Base):
    """Stores the latest synced state of employees in the Ahgora system."""

    __tablename__ = "ahgora_employees"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    position: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scale: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    department: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    admission_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dismissal_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AhgoraLeaveModel(Base):
    """Stores the latest synced state of employee leaves in the Ahgora system."""

    __tablename__ = "ahgora_leaves"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(String, index=True)
    cod: Mapped[str] = mapped_column(String)
    cod_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    start_date: Mapped[datetime] = mapped_column(DateTime)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    start_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    end_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    duration: Mapped[Optional[int]] = mapped_column(nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UserCredentialModel(Base):
    __tablename__ = "user_credentials"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), unique=True, nullable=False
    )
    fiorilli_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fiorilli_user: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fiorilli_password_encrypted: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    ahgora_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ahgora_user: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ahgora_password_encrypted: Mapped[Optional[str]] = mapped_column(
        String, nullable=True
    )
    ahgora_company: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    # Relationship
    user: Mapped["UserModel"] = relationship(back_populates="credentials")


# Add relationship to UserModel
UserModel.credentials = relationship(
    "UserCredentialModel", back_populates="user", uselist=False
)
