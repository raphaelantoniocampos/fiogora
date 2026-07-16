import asyncio
import logging
import threading
from typing import Optional
from uuid import UUID

from app.core.task_registry import task_registry
from app.domain.enums import (
    AutomationTaskStatus as TaskStatus,
)
from app.domain.enums import (
    AutomationTaskType as TaskType,
)
from app.domain.enums import (
    SyncStatus as JobStatus,
)
from app.infrastructure.automation.web.ahgora_browser import AhgoraBrowser
from app.infrastructure.db.sqlalchemy_repo import SqlAlchemyRepo

logger = logging.getLogger(__name__)


class TaskExecutionService:
    def __init__(self, repo: SqlAlchemyRepo):
        self.repo = repo

    async def execute_task(
        self,
        task_id: UUID,
        fiorilli_url: Optional[str] = None,
        fiorilli_user: Optional[str] = None,
        fiorilli_password: Optional[str] = None,
        ahgora_url: Optional[str] = None,
        ahgora_user: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        ahgora_password: Optional[str] = None,
        evaluate_job: bool = True,
    ) -> bool:
        """
        Executes a single automation task.
        Returns True if successful, False otherwise.
        """
        task = await self.repo.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found.")
            return False

        if task.status in [TaskStatus.SUCCESS, TaskStatus.RUNNING]:
            logger.warning(f"Task {task_id} is already completed or running.")
            return False

        await self.repo.update_task_status(task_id, TaskStatus.RUNNING)
        await self.repo.add_log(
            task.job_id,
            "INFO",
            f"Starting web automation (Selenium) for task {task.type}.",
            task_id=task_id,
        )

        if evaluate_job:
            await self.repo.evaluate_and_update_job_status(task.job_id)

        success = False
        error_msg = None

        cancel_event = task_registry.get_cancel_event(task_id)
        if not cancel_event:
            cancel_event = threading.Event()
            task_registry.register_cancel_event(task_id, cancel_event)

        try:
            # We run the browser automation in a separate thread so we don't block the async loop
            loop = asyncio.get_running_loop()
            success = await asyncio.to_thread(
                self._run_browser_automation,
                task.type,
                task.payload,
                task.job_id,
                task_id,
                loop,
                cancel_event,
                ahgora_user=ahgora_user,
                ahgora_password=ahgora_password,
                ahgora_company=ahgora_company,
                ahgora_url=ahgora_url,
            )
        except Exception as e:
            logger.exception(f"Error executing task {task_id}")
            error_msg = str(e)
            await self.repo.add_log(
                task.job_id,
                "ERROR",
                f"Automation failure: {error_msg}.",
                task_id=task_id,
            )

        if success:
            await self.repo.update_task_status(task_id, TaskStatus.SUCCESS)
            await self.repo.add_log(
                task.job_id,
                "INFO",
                "Automation completed successfully.",
                task_id=task_id,
            )
            # Update Ahgora model state based on task success
            await self._update_ahgora_state(task.type, task.payload)
        else:
            if not error_msg:
                # If error wasn't raised but automation returned False
                await self.repo.add_log(
                    task.job_id,
                    "WARNING",
                    "Automation finished unsuccessfully, but without raising an exception.",
                    task_id=task_id,
                )

            await self.repo.update_task_status(
                task_id, TaskStatus.FAILED, message=error_msg
            )

        if evaluate_job:
            await self.repo.evaluate_and_update_job_status(task.job_id)
        return success

    async def execute_batch(
        self,
        job_id: UUID,
        task_type: str,
        fiorilli_url: Optional[str] = None,
        fiorilli_user: Optional[str] = None,
        fiorilli_password: Optional[str] = None,
        ahgora_url: Optional[str] = None,
        ahgora_user: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        ahgora_password: Optional[str] = None,
    ) -> None:
        """
        Executes all pending, failed or cancelled tasks of a certain type for a given job.
        Runs sequentially to respect browser limitations.
        """
        from app.domain.enums import AutomationTaskStatus

        logger.info(f"Starting batch execution for job {job_id}, type {task_type}")

        await self.repo.update_job_status(job_id=job_id, status=JobStatus.RUNNING)

        await self.repo.evaluate_and_update_job_status(job_id)

        if "ADD_LEAVE" in str(task_type).upper():
            logger.info("Delegating ADD_LEAVE batch to LeaveSyncService")
            from app.services.leave_sync_service import LeaveSyncService

            leave_service = LeaveSyncService(self.repo)
            await leave_service.execute_leaves_batch(
                job_id,
                ahgora_user=ahgora_user,
                ahgora_password=ahgora_password,
                ahgora_company=ahgora_company,
                ahgora_url=ahgora_url,
            )
            return

        # We need a custom repo method or we fetch all and filter
        tasks = await self.repo.get_automation_tasks_by_job(job_id)
        batch = [
            t
            for t in tasks
            if (
                t.type.value == task_type
                or t.type.name == task_type
                or str(t.type) == task_type
            )
            and t.status
            in [
                AutomationTaskStatus.PENDING,
                AutomationTaskStatus.FAILED,
                AutomationTaskStatus.CANCELLED,
            ]
        ]

        for t in batch:
            await self.execute_task(
                t.id,
                fiorilli_url=fiorilli_url,
                fiorilli_user=fiorilli_user,
                fiorilli_password=fiorilli_password,
                ahgora_url=ahgora_url,
                ahgora_user=ahgora_user,
                ahgora_company=ahgora_company,
                ahgora_password=ahgora_password,
                evaluate_job=False,
            )

        await self.repo.evaluate_and_update_job_status(job_id)

    async def execute_all_tasks(
        self,
        job_id: UUID,
        fiorilli_url: Optional[str] = None,
        fiorilli_user: Optional[str] = None,
        fiorilli_password: Optional[str] = None,
        ahgora_url: Optional[str] = None,
        ahgora_user: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        ahgora_password: Optional[str] = None,
    ) -> None:
        """
        Executes all pending, failed or cancelled tasks for a given job.
        Runs them one by one sequentially.
        """
        logger.info(f"Starting execution of all tasks for job {job_id}")

        await self.repo.update_job_status(job_id=job_id, status=JobStatus.RUNNING)

        tasks = await self.repo.get_automation_tasks_by_job(job_id)

        for t in tasks:
            # Re-fetch task to ensure we have the latest status
            current_task = await self.repo.get_task(t.id)
            if not current_task:
                continue
            if current_task.status in [TaskStatus.SUCCESS, TaskStatus.RUNNING]:
                logger.info(
                    f"Skipping task {t.id} because status is {current_task.status}"
                )
                continue

            if "ADD_LEAVE" in str(current_task.type).upper():
                logger.info("Delegating ADD_LEAVE task to LeaveSyncService")
                from app.services.leave_sync_service import LeaveSyncService

                leave_service = LeaveSyncService(self.repo)
                await leave_service.execute_leaves_batch(
                    job_id,
                    ahgora_user=ahgora_user,
                    ahgora_password=ahgora_password,
                    ahgora_company=ahgora_company,
                    ahgora_url=ahgora_url,
                )
            else:
                await self.execute_task(
                    current_task.id,
                    fiorilli_url=fiorilli_url,
                    fiorilli_user=fiorilli_user,
                    fiorilli_password=fiorilli_password,
                    ahgora_url=ahgora_url,
                    ahgora_user=ahgora_user,
                    ahgora_company=ahgora_company,
                    ahgora_password=ahgora_password,
                    evaluate_job=False,
                )

        await self.repo.evaluate_and_update_job_status(job_id)

    async def cancel_task(self, task_id: UUID) -> bool:
        """
        Cancels a single automation task if it is pending, running, or failed.
        Returns True if cancelled successfully.
        """

        task = await self.repo.get_task(task_id)
        if not task:
            logger.error(f"Task {task_id} not found.")
            return False

        if task.status in [
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
        ]:
            if task.status == TaskStatus.RUNNING:
                cancel_event = task_registry.get_cancel_event(task_id)
                if cancel_event:
                    cancel_event.set()

            await self.repo.update_task_status(
                task_id, TaskStatus.CANCELLED, "Cancelled by user via API"
            )
            logger.info(f"Task {task_id} cancelled.")

            # Re-evaluate job status since a task was cancelled
            await self.repo.evaluate_and_update_job_status(task.job_id)
            return True
        else:
            logger.warning(
                f"Task {task_id} cannot be cancelled in state {task.status}."
            )
            return False

    async def cancel_batch(self, job_id: UUID, task_type: str) -> None:
        """
        Cancels all pending or running tasks of a certain type for a given job.
        Note: If a browser is currently driving a task, it might finish unless hard-killed,
        but we can at least mark pending ones as cancelled.
        """
        from app.domain.enums import AutomationTaskStatus

        logger.info(f"Cancelling batch execution for job {job_id}, type {task_type}")

        tasks = await self.repo.get_automation_tasks_by_job(job_id)
        batch = [
            t
            for t in tasks
            if (
                t.type.value == task_type
                or t.type.name == task_type
                or str(t.type) == task_type
            )
            and t.status
            in [
                AutomationTaskStatus.PENDING,
                AutomationTaskStatus.RUNNING,
                AutomationTaskStatus.FAILED,
            ]
        ]

        for t in batch:
            await self.cancel_task(t.id)

        await self.repo.evaluate_and_update_job_status(job_id)

    async def cancel_all_for_job(self, job_id: UUID) -> None:
        """
        Cancels all pending, running or failed tasks for a given job.
        """
        from app.domain.enums import AutomationTaskStatus

        logger.info(f"Cancelling all tasks for job {job_id}")

        tasks = await self.repo.get_automation_tasks_by_job(job_id)
        batch = [
            t
            for t in tasks
            if t.status
            in [
                AutomationTaskStatus.PENDING,
                AutomationTaskStatus.RUNNING,
                AutomationTaskStatus.FAILED,
            ]
        ]

        for t in batch:
            await self.cancel_task(t.id)

        await self.repo.evaluate_and_update_job_status(job_id)

    def _run_browser_automation(
        self,
        task_type: TaskType,
        payload: dict,
        job_id: UUID,
        task_id: UUID,
        loop: asyncio.AbstractEventLoop,
        cancel_event: threading.Event = None,
        ahgora_user: Optional[str] = None,
        ahgora_password: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        ahgora_url: Optional[str] = None,
    ) -> bool:
        """
        Runs the actual Selenium browser automation based on task type.
        This runs in a sync thread.
        """

        def log_cb(level: str, msg: str):
            async def _do_log():
                from app.core.database import async_session_factory

                try:
                    async with async_session_factory() as session:
                        repo = SqlAlchemyRepo(session)
                        await repo.add_log(job_id, level, msg, task_id=task_id)
                except Exception as ex:
                    logger.error(f"Background log failed for task {task_id}: {ex}.")

            asyncio.run_coroutine_threadsafe(_do_log(), loop)

        from app.core.settings import settings

        browser = None
        try:
            browser = AhgoraBrowser(
                ahgora_user=ahgora_user,
                ahgora_password=ahgora_password,
                ahgora_company=ahgora_company,
                ahgora_url=ahgora_url,
                log_callback=log_cb,
                headless=settings.HEADLESS_MODE_TASKS,
                cancel_event=cancel_event,
            )
            match task_type:
                case TaskType.ADD_EMPLOYEE:
                    browser.add_employee(payload)
                case TaskType.UPDATE_EMPLOYEE:
                    browser.update_employee(payload)
                case TaskType.REMOVE_EMPLOYEE:
                    browser.remove_employee(payload)
                case TaskType.ADD_LEAVE:
                    logger.error(
                        "ADD_LEAVE must be executed via batch (execute_batch). Individual execution not supported."
                    )
                    return False
                case _:
                    logger.error(f"Unsupported task type for automation: {task_type}")
                    return False
            return True
        except Exception as e:
            logger.error(f"Browser automation failed: {str(e)}")
            raise e
        finally:
            if browser:
                browser.close_driver()

        return True

    async def _update_ahgora_state(self, task_type: TaskType, payload: dict):
        """
        Updates the local database Ahgora state after a successful task.
        """
        try:
            # Reconstruct Ahgora state from payload
            if task_type in [
                TaskType.ADD_EMPLOYEE,
                TaskType.UPDATE_EMPLOYEE,
                TaskType.REMOVE_EMPLOYEE,
            ]:
                emp_data = {
                    "id": str(payload.get("id")),
                    "name": payload.get("name"),
                    "position": payload.get("position"),
                    "department": payload.get("department"),
                    "admission_date": payload.get("admission_date"),
                }
                if task_type == TaskType.REMOVE_EMPLOYEE:
                    emp_data["dismissal_date"] = payload.get("dismissal_date")

                await self.repo.save_ahgora_employees_batch([emp_data])
                logger.info(
                    f"Updated Ahgora DB state for employee ID: {emp_data['id']}"
                )
            else:
                logger.info("Task type does not require Ahgora employee state update.")
        except Exception as e:
            logger.error(f"Failed to update Ahgora state: {str(e)}")
