from datetime import datetime
from typing import List, Optional
from uuid import UUID

import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_log_context
from app.domain.entities import (
    AutomationTask,
    AutomationTaskStatus,
    AutomationTaskType,
    SyncJob,
    SyncLog,
    SyncStatus,
)
from app.infrastructure.db.models import (
    AhgoraEmployeeModel,
    AhgoraLeaveModel,
    AutomationTaskModel,
    GlobalSettingsModel,
    SyncJobModel,
    SyncLogModel,
    UserCredentialModel,
    UserModel,
)


def _tasks_summary(
    running: int, pending: int, completed: int, failed: int, cancelled: int
) -> str:
    """Job message derived from its tasks, e.g. "2 tarefas: 1 concluída, 1 com falha"."""
    total = running + pending + completed + failed + cancelled
    parts = [
        f"{count} {label}"
        for count, label in (
            (running, "em execução"),
            (pending, "pendente" if pending == 1 else "pendentes"),
            (completed, "concluída" if completed == 1 else "concluídas"),
            (failed, "com falha"),
            (cancelled, "cancelada" if cancelled == 1 else "canceladas"),
        )
        if count
    ]
    return f"{total} {'tarefa' if total == 1 else 'tarefas'}: {', '.join(parts)}"


class SqlAlchemyRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_job(db: SyncJobModel) -> SyncJob:
        return SyncJob(
            id=db.id,
            status=SyncStatus(db.status),
            triggered_by=db.triggered_by,
            user_id=db.user_id,
            created_at=db.created_at,
            started_at=db.started_at,
            finished_at=db.finished_at,
            error_message=db.error_message,
            metadata_info=db.metadata_info,
            retry_count=db.retry_count,
            next_retry_at=db.next_retry_at,
        )

    @staticmethod
    def _to_log(db: SyncLogModel) -> SyncLog:
        return SyncLog(
            id=db.id,
            job_id=db.job_id,
            task_id=db.task_id,
            level=db.level,
            message=db.message,
            timestamp=db.timestamp,
            username=db.username,
        )

    async def get_user_by_username(self, username: str) -> Optional[UserModel]:
        result = await self.session.execute(
            select(UserModel).filter_by(username=username)
        )
        return result.scalars().first()

    async def create_user(
        self, username: str, hashed_password: str, is_admin: bool = False
    ) -> UserModel:
        db_user = UserModel(
            username=username, hashed_password=hashed_password, is_admin=is_admin
        )
        self.session.add(db_user)
        await self.session.commit()
        await self.session.refresh(db_user)
        return db_user

    async def update_user_password(self, username: str, hashed_password: str) -> None:
        user = await self.get_user_by_username(username)
        if user:
            user.hashed_password = hashed_password
            await self.session.commit()

    async def save_job(self, job: SyncJob) -> None:
        # Check if job exists
        db_job = await self.session.get(SyncJobModel, job.id)
        if not db_job:
            db_job = SyncJobModel(
                id=job.id,
                status=job.status,
                triggered_by=job.triggered_by,
                user_id=job.user_id,
                created_at=job.created_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                error_message=job.error_message,
                metadata_info=job.metadata_info,
                retry_count=job.retry_count,
                next_retry_at=job.next_retry_at,
            )
            self.session.add(db_job)
        else:
            db_job.status = job.status
            if job.user_id is not None:
                db_job.user_id = job.user_id
            db_job.started_at = job.started_at  # type: ignore
            db_job.finished_at = job.finished_at  # type: ignore
            db_job.error_message = job.error_message  # type: ignore
            db_job.metadata_info = job.metadata_info
            db_job.retry_count = job.retry_count
            db_job.next_retry_at = job.next_retry_at  # type: ignore

        await self.session.commit()

    async def get_job(self, job_id: UUID) -> Optional[SyncJob]:
        db_job = await self.session.get(SyncJobModel, job_id)
        if not db_job:
            return None

        return self._to_job(db_job)

    async def get_job_status(self, job_id: UUID) -> Optional[SyncStatus]:
        job = await self.get_job(job_id)

        return job.status if job else None

    async def list_jobs(self, username: Optional[str] = None) -> List[SyncJob]:
        """All jobs, newest first. With `username`, only the jobs that user started or
        worked on (ran or cancelled tasks), which leaves a log entry with their name."""
        query = select(SyncJobModel).order_by(SyncJobModel.created_at.desc())
        if username:
            owner_id = (
                select(UserModel.id)
                .where(UserModel.username == username)
                .scalar_subquery()
            )
            worked_on = select(SyncLogModel.job_id).where(
                SyncLogModel.username == username
            )
            query = query.where(
                or_(SyncJobModel.user_id == owner_id, SyncJobModel.id.in_(worked_on))
            )
        result = await self.session.execute(query)
        return [self._to_job(db) for db in result.scalars().all()]

    async def get_usernames(self) -> dict[UUID, str]:
        result = await self.session.execute(
            select(UserModel.id, UserModel.username).order_by(UserModel.username)
        )
        return {user_id: username for user_id, username in result.all()}

    async def update_job_status(
        self, job_id: UUID, status: SyncStatus, message: Optional[str] = None
    ):
        db_job = await self.session.get(SyncJobModel, job_id)
        if db_job:
            db_job.status = status
            if status == SyncStatus.RUNNING:
                if not db_job.started_at:
                    db_job.started_at = datetime.now()
                # Clear next_retry_at when starting
                db_job.next_retry_at = None  # type: ignore
            elif status in [
                SyncStatus.SUCCESS,
                SyncStatus.FAILED,
                SyncStatus.CANCELLED,
            ]:
                db_job.finished_at = datetime.now()

            if message:
                db_job.error_message = message

            await self.session.commit()

    async def increment_job_retry(self, job_id: UUID, next_retry_at: datetime):
        db_job = await self.session.get(SyncJobModel, job_id)
        if db_job:
            db_job.retry_count += 1
            db_job.next_retry_at = next_retry_at
            db_job.status = SyncStatus.RETRYING
            await self.session.commit()

    async def get_jobs_ready_for_retry(self) -> List[SyncJob]:
        now = datetime.now()
        result = await self.session.execute(
            select(SyncJobModel)
            .where(SyncJobModel.status == SyncStatus.RETRYING)
            .where(SyncJobModel.next_retry_at <= now)
        )
        return [self._to_job(db) for db in result.scalars().all()]

    async def add_log(
        self, job_id: UUID, level: str, message: str, task_id: Optional[UUID] = None
    ) -> None:
        db_log = SyncLogModel(
            job_id=job_id,
            task_id=task_id,
            level=level,
            message=message,
            timestamp=datetime.now(),
            username=get_log_context().get("username"),
        )
        self.session.add(db_log)
        await self.session.commit()

    async def get_job_logs(self, job_id: UUID) -> List[SyncLog]:
        result = await self.session.execute(
            select(SyncLogModel)
            .filter_by(job_id=job_id)
            .order_by(SyncLogModel.timestamp.asc())
        )
        return [self._to_log(db) for db in result.scalars().all()]

    async def get_task_logs(self, task_id: UUID) -> List[SyncLog]:
        result = await self.session.execute(
            select(SyncLogModel)
            .filter_by(task_id=task_id)
            .order_by(SyncLogModel.timestamp.asc())
        )
        return [self._to_log(db) for db in result.scalars().all()]

    async def save_automation_task(self, task: AutomationTask) -> None:
        db_task = await self.session.get(AutomationTaskModel, task.id)
        if not db_task:
            db_task = AutomationTaskModel(
                id=task.id,
                job_id=task.job_id,
                type=task.type,
                status=task.status,
                payload_info=task.payload,
                created_at=task.created_at,
                started_at=task.started_at,
                finished_at=task.finished_at,
                error_message=task.error_message,
                retry_count=task.retry_count,
            )
            self.session.add(db_task)
        else:
            db_task.status = task.status
            db_task.payload_info = task.payload
            db_task.started_at = task.started_at  # type: ignore
            db_task.finished_at = task.finished_at  # type: ignore
            db_task.error_message = task.error_message  # type: ignore
            db_task.retry_count = task.retry_count

        await self.session.commit()

    async def get_task(self, task_id: UUID) -> Optional[AutomationTask]:
        db_task = await self.session.get(AutomationTaskModel, task_id)
        if not db_task:
            return None

        return AutomationTask(
            id=db_task.id,
            job_id=db_task.job_id,
            type=AutomationTaskType(db_task.type),
            status=AutomationTaskStatus(db_task.status),
            payload=db_task.payload_info,
            created_at=db_task.created_at,
            started_at=db_task.started_at,
            finished_at=db_task.finished_at,
            error_message=db_task.error_message,
            retry_count=db_task.retry_count,
        )

    async def update_task_status(
        self,
        task_id: UUID,
        status: AutomationTaskStatus,
        message: Optional[str] = None,
        payload: Optional[dict] = None,
    ):
        db_task = await self.session.get(AutomationTaskModel, task_id)
        if db_task:
            db_task.status = status

            if payload is not None:
                db_task.payload_info = payload

            if status == AutomationTaskStatus.RUNNING:
                db_task.started_at = datetime.now()
            elif status in [
                AutomationTaskStatus.SUCCESS,
                AutomationTaskStatus.FAILED,
                AutomationTaskStatus.CANCELLED,
            ]:
                db_task.finished_at = datetime.now()

            if message:
                db_task.error_message = message

            await self.session.commit()

    async def evaluate_and_update_job_status(
        self, job_id: UUID, message: Optional[str] = None
    ) -> None:
        """
        Evaluates and updates the job status based on its associated tasks.
        Rules:
        - If ANY task is PENDING or RUNNING -> Job is PENDING (or RUNNING)
        - Else If ANY task is FAILED -> Job is FAILED
        - Else If there are MORE CANCELLED tasks than SUCCESS -> Job is CANCELLED
        - Else (All SUCCESS) -> Job is SUCCESS
        With tasks, the job message becomes a summary of them (`message` is only used for
        jobs without tasks), so it never contradicts the status.
        """
        db_job = await self.session.get(SyncJobModel, job_id)
        if not db_job:
            return

        result = await self.session.execute(
            select(AutomationTaskModel).filter_by(job_id=job_id)
        )
        tasks = result.scalars().all()

        if not tasks:
            new_status = SyncStatus.SUCCESS
            if new_status != db_job.status or message:
                db_job.status = new_status
                db_job.finished_at = datetime.now()
                if message:
                    db_job.error_message = message
                await self.session.commit()
            return

        is_running = 0
        is_pending = 0
        is_failed = 0
        is_cancelled = 0
        completed = 0

        for t in tasks:
            if t.status == AutomationTaskStatus.RUNNING:
                is_running += 1
            elif t.status == AutomationTaskStatus.PENDING:
                is_pending += 1
            elif t.status == AutomationTaskStatus.FAILED:
                is_failed += 1
            elif t.status == AutomationTaskStatus.CANCELLED:
                is_cancelled += 1
            elif t.status == AutomationTaskStatus.SUCCESS:
                completed += 1

        total = len(tasks)
        if is_running:
            new_status = SyncStatus.RUNNING
        elif is_pending:
            new_status = SyncStatus.PENDING
        elif completed == total:
            new_status = SyncStatus.SUCCESS
        elif is_cancelled == total:
            new_status = SyncStatus.CANCELLED
        elif is_failed:
            new_status = SyncStatus.FAILED
        else:
            new_status = SyncStatus.SUCCESS

        summary = _tasks_summary(
            is_running, is_pending, completed, is_failed, is_cancelled
        )
        if new_status != db_job.status or summary != db_job.error_message:
            db_job.status = new_status
            if new_status in [
                SyncStatus.SUCCESS,
                SyncStatus.FAILED,
                SyncStatus.CANCELLED,
            ]:
                db_job.finished_at = datetime.now()

            db_job.error_message = summary

            await self.session.commit()

    async def save_automation_tasks_batch(self, tasks: List[AutomationTask]) -> None:
        db_tasks = [
            AutomationTaskModel(
                id=task.id,
                job_id=task.job_id,
                type=task.type,
                status=task.status,
                payload_info=task.payload,
                created_at=task.created_at,
                started_at=task.started_at,
                finished_at=task.finished_at,
                error_message=task.error_message,
                retry_count=task.retry_count,
            )
            for task in tasks
        ]
        self.session.add_all(db_tasks)
        await self.session.commit()

    async def get_automation_tasks_by_job(self, job_id: UUID) -> List[AutomationTask]:
        result = await self.session.execute(
            select(AutomationTaskModel)
            .filter_by(job_id=job_id)
            .order_by(AutomationTaskModel.created_at.asc())
        )
        db_tasks = result.scalars().all()
        return [
            AutomationTask(
                id=db.id,
                job_id=db.job_id,
                type=AutomationTaskType(db.type),
                status=AutomationTaskStatus(db.status),
                payload=db.payload_info,
                created_at=db.created_at,
                started_at=db.started_at,
                finished_at=db.finished_at,
                error_message=db.error_message,
                retry_count=db.retry_count,
            )
            for db in db_tasks
        ]

    async def get_all_automation_tasks(
        self, status: Optional[AutomationTaskStatus] = None
    ) -> List[AutomationTask]:
        query = select(AutomationTaskModel).order_by(
            AutomationTaskModel.created_at.desc()
        )
        if status:
            query = query.filter_by(status=status)

        result = await self.session.execute(query)
        db_tasks = result.scalars().all()
        return [
            AutomationTask(
                id=db.id,
                job_id=db.job_id,
                type=AutomationTaskType(db.type),
                status=AutomationTaskStatus(db.status),
                payload=db.payload_info,
                created_at=db.created_at,
                started_at=db.started_at,
                finished_at=db.finished_at,
                error_message=db.error_message,
                retry_count=db.retry_count,
            )
            for db in db_tasks
        ]

    async def get_global_settings(self) -> Optional[GlobalSettingsModel]:
        """Retrieve the global settings from the database"""
        result = await self.session.execute(select(GlobalSettingsModel))
        gs = result.scalar_one_or_none()
        if gs is None:
            return None
        # Return only the URL fields and company to callers to avoid leaking credentials
        return GlobalSettingsModel(
            id=gs.id,
            fiorilli_url=gs.fiorilli_url,
            ahgora_url=gs.ahgora_url,
            ahgora_company=gs.ahgora_company,
        )

    async def get_user_credentials(self, user_id: UUID) -> Optional[dict]:
        """Retrieve credentials for a specific user"""
        result = await self.session.execute(
            select(UserCredentialModel).filter_by(user_id=user_id)
        )
        creds_model = result.scalar_one_or_none()

        if creds_model is None:
            return None

        return {
            "fiorilli_url": creds_model.fiorilli_url,
            "fiorilli_user": creds_model.fiorilli_user,
            "fiorilli_password_encrypted": creds_model.fiorilli_password_encrypted,
            "ahgora_url": creds_model.ahgora_url,
            "ahgora_user": creds_model.ahgora_user,
            "ahgora_password_encrypted": creds_model.ahgora_password_encrypted,
            "ahgora_company": creds_model.ahgora_company,
        }

    async def save_user_credentials(
        self, user_id: UUID, credentials_dict: dict
    ) -> None:
        """Save or update credentials for a specific user"""
        # Check if credentials already exist for this user
        result = await self.session.execute(
            select(UserCredentialModel).filter_by(user_id=user_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            # Update existing record
            existing.fiorilli_url = credentials_dict.get("fiorilli_url")
            existing.fiorilli_user = credentials_dict.get("fiorilli_user")
            existing.fiorilli_password_encrypted = credentials_dict.get(
                "fiorilli_password_encrypted"
            )
            existing.ahgora_url = credentials_dict.get("ahgora_url")
            existing.ahgora_user = credentials_dict.get("ahgora_user")
            existing.ahgora_password_encrypted = credentials_dict.get(
                "ahgora_password_encrypted"
            )
            existing.ahgora_company = credentials_dict.get("ahgora_company")
            existing.updated_at = datetime.now()
        else:
            # Create new record
            new_creds = UserCredentialModel(
                user_id=user_id,
                fiorilli_url=credentials_dict.get("fiorilli_url"),
                fiorilli_user=credentials_dict.get("fiorilli_user"),
                fiorilli_password_encrypted=credentials_dict.get(
                    "fiorilli_password_encrypted"
                ),
                ahgora_url=credentials_dict.get("ahgora_url"),
                ahgora_user=credentials_dict.get("ahgora_user"),
                ahgora_password_encrypted=credentials_dict.get(
                    "ahgora_password_encrypted"
                ),
                ahgora_company=credentials_dict.get("ahgora_company"),
            )
            self.session.add(new_creds)

        await self.session.commit()

    async def get_ahgora_employees_df(self) -> pd.DataFrame:
        """Returns the cached Ahgora employees as a DataFrame"""
        result = await self.session.execute(select(AhgoraEmployeeModel))
        employees = result.scalars().all()
        data = [
            {
                "id": emp.id,
                "name": emp.name,
                "position": emp.position,
                "scale": emp.scale,
                "department": emp.department,
                "location": emp.location,
                "admission_date": emp.admission_date,
                "dismissal_date": emp.dismissal_date,
            }
            for emp in employees
        ]
        return pd.DataFrame(data)

    async def get_ahgora_leaves_df(self) -> pd.DataFrame:
        """Returns the cached Ahgora leaves as a DataFrame"""
        result = await self.session.execute(select(AhgoraLeaveModel))
        leaves = result.scalars().all()
        data = [
            {
                "id": leave.employee_id,
                "cod": leave.cod,
                "cod_name": leave.cod_name,
                "start_date": leave.start_date,
                "end_date": leave.end_date,
                "start_time": leave.start_time,
                "end_time": leave.end_time,
                "duration": leave.duration,
            }
            for leave in leaves
        ]
        return pd.DataFrame(data)

    @staticmethod
    def _parse_date(value) -> datetime | None:
        """Convert date string or datetime to datetime object for DB storage."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
        return None

    async def save_ahgora_employees_batch(self, employees: List[dict]) -> None:
        """Performs a merge (upsert) for a batch of Ahgora employees"""
        for emp_dict in employees:
            db_emp = AhgoraEmployeeModel(
                id=emp_dict.get("id"),
                name=emp_dict.get("name"),
                position=emp_dict.get("position"),
                scale=emp_dict.get("scale"),
                department=emp_dict.get("department"),
                location=emp_dict.get("location"),
                admission_date=self._parse_date(emp_dict.get("admission_date")),
                dismissal_date=self._parse_date(emp_dict.get("dismissal_date")),
                last_synced_at=datetime.now(),
            )
            await self.session.merge(db_emp)
        await self.session.commit()

    async def cleanup_stuck_executions(self) -> None:
        """
        Marks any RUNNING jobs or tasks as FAILED since the server is starting up.
        This handles cases where the system crashed or process was killed abruptly.
        """
        from sqlalchemy import update

        # 1. Update jobs
        await self.session.execute(
            update(SyncJobModel)
            .where(SyncJobModel.status == SyncStatus.RUNNING)
            .values(
                status=SyncStatus.FAILED,
                error_message="Sistema reiniciado durante a execução. Sincronização interrompida.",
                finished_at=datetime.now(),
            )
        )

        # 2. Update tasks
        await self.session.execute(
            update(AutomationTaskModel)
            .where(AutomationTaskModel.status == AutomationTaskStatus.RUNNING)
            .values(
                status=AutomationTaskStatus.FAILED,
                error_message="Sistema reiniciado durante a execução. Tarefa interrompida.",
                finished_at=datetime.now(),
            )
        )
        await self.session.commit()

    async def save_ahgora_leaves_batch(self, leaves: List[dict]) -> None:
        """Saves a batch of Ahgora leaves to the DB cache"""
        for leave_dict in leaves:
            db_leave = AhgoraLeaveModel(
                employee_id=leave_dict.get(
                    "id"
                ),  # DataFrame uses 'id' for the employee id
                cod=leave_dict.get("cod"),
                cod_name=leave_dict.get("cod_name"),
                start_date=self._parse_date(leave_dict.get("start_date")),
                end_date=self._parse_date(leave_dict.get("end_date")),
                start_time=leave_dict.get("start_time"),
                end_time=leave_dict.get("end_time"),
                duration=leave_dict.get("duration"),
                last_synced_at=datetime.now(),
            )
            self.session.add(db_leave)
        await self.session.commit()
