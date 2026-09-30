from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from .enums import AutomationTaskStatus, AutomationTaskType, SyncStatus


@dataclass
class SyncJob:
    id: UUID = field(default_factory=uuid4)
    status: SyncStatus = field(default=SyncStatus.PENDING)
    triggered_by: str = field(default="system")
    user_id: Optional[UUID] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    metadata_info: Dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0
    next_retry_at: Optional[datetime] = None


@dataclass
class SyncResult:
    success: bool
    status: SyncStatus
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncLog:
    id: Optional[int]
    job_id: UUID
    level: str
    message: str
    task_id: Optional[UUID] = None
    timestamp: datetime = field(default_factory=datetime.now)
    username: Optional[str] = None


@dataclass
class AutomationTask:
    job_id: UUID
    type: AutomationTaskType
    id: UUID = field(default_factory=uuid4)
    status: AutomationTaskStatus = field(default=AutomationTaskStatus.PENDING)
    payload: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: int = 0

    def describe(self) -> str:
        """Readable label for logs, e.g. "Atualizar funcionário: FULANO DE TAL (123)"."""
        if self.type == AutomationTaskType.ADD_LEAVE:
            return f"{self.type.label} ({len(self.payload.get('leaves', []))})"
        name = self.payload.get("name") or self.payload.get("name_expected")
        employee_id = self.payload.get("id")
        if not name:
            return self.type.label
        return f"{self.type.label}: {name}" + (
            f" ({employee_id})" if employee_id else ""
        )
