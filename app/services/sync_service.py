import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

import numpy as np
import pandas as pd

from app.core.file_manager import FileManager
from app.core.settings import settings
from app.core.task_registry import task_registry
from app.domain.entities import (
    AutomationTask,
    AutomationTaskStatus,
    AutomationTaskType,
    SyncJob,
    SyncLog,
    SyncResult,
)
from app.domain.enums import SyncStatus
from app.infrastructure.automation.web.ahgora_browser import AhgoraBrowser
from app.infrastructure.automation.web.fiorilli_browser import FiorilliBrowser
from app.infrastructure.db.sqlalchemy_repo import SqlAlchemyRepo

FIORILLI_EMPLOYEES_COLUMNS = settings.FIORILLI_EMPLOYEES_COLUMNS
AHGORA_EMPLOYEES_COLUMNS = settings.AHGORA_EMPLOYEES_COLUMNS
COLUMNS_TO_VERIFY_CHANGE = settings.COLUMNS_TO_VERIFY_CHANGE
LEAVES_COLUMNS = settings.LEAVES_COLUMNS
UPLOAD_LEAVES_COLUMNS = settings.UPLOAD_LEAVES_COLUMNS
PT_MONTHS = settings.PT_MONTHS
MAX_AGE_MINUTES = settings.MAX_AGE_MINUTES
SYNC_TIMEOUT_MAX = settings.SYNC_TIMEOUT_MAX

DATA_DIR = settings.DATA_DIR

logger = logging.getLogger(__name__)


class SyncService:
    MAX_JOB_RETRIES = 3

    def __init__(self, repo: SqlAlchemyRepo):
        self.repo = repo
        self._db_lock = asyncio.Lock()

    @staticmethod
    async def run_sync_task_standalone(
        job_id: UUID,
        fiorilli_url: str,
        fiorilli_user: str,
        fiorilli_password: str,
        ahgora_url: str,
        ahgora_user: str,
        ahgora_company: str,
        ahgora_password: str,
    ):
        """
        Static method to run a sync task with its own database session.
        This is used for background tasks to avoid session closure issues.
        """
        from app.core.database import async_session_factory

        async with async_session_factory() as session:
            repo = SqlAlchemyRepo(session)
            service = SyncService(repo)
            await service.run_sync_background(
                job_id,
                fiorilli_url,
                fiorilli_user,
                fiorilli_password,
                ahgora_url,
                ahgora_user,
                ahgora_company,
                ahgora_password,
            )

    async def create_job(self, triggered_by: str = "api") -> SyncJob:
        job = SyncJob(triggered_by=triggered_by)
        async with self._db_lock:
            await self.repo.save_job(job)
        return job

    async def get_job(self, job_id: UUID) -> Optional[SyncJob]:
        return await self.repo.get_job(job_id)

    async def get_job_status(self, job_id: UUID) -> Optional[SyncStatus]:
        return await self.repo.get_job_status(job_id)

    async def list_jobs(self) -> list[SyncJob]:
        return await self.repo.list_jobs()

    async def get_job_logs(self, job_id: UUID) -> list[SyncLog]:
        return await self.repo.get_job_logs(job_id)

    async def get_automation_tasks(self, job_id: UUID) -> list[AutomationTask]:
        return await self.repo.get_automation_tasks_by_job(job_id)

    async def list_automation_tasks(
        self, status: Optional[AutomationTaskStatus] = None
    ) -> list[AutomationTask]:
        return await self.repo.get_all_automation_tasks(status)

    async def run_sync_background(
        self,
        job_id: UUID,
        fiorilli_url: str,
        fiorilli_user: str,
        fiorilli_password: str,
        ahgora_url: str,
        ahgora_user: str,
        ahgora_company: str,
        ahgora_password: str,
    ):
        job = await self.repo.get_job(job_id)
        if not job:
            logger.error(f"Job {job_id} not found for execution")
            return

        async with self._db_lock:
            await self.repo.update_job_status(job_id, SyncStatus.RUNNING)
        await self._log(job_id, "INFO", f"Started background sync for job {job_id}")

        # Register the current task
        current_task = asyncio.current_task()
        if current_task is not None:
            task_registry.register(job_id, current_task)

        try:
            # Execute sync logic with a 30-minute timeout (prevents permanent hangs)
            result = await asyncio.wait_for(
                self._execute_sync_logic(
                    job_id,
                    fiorilli_url,
                    fiorilli_user,
                    fiorilli_password,
                    ahgora_url,
                    ahgora_user,
                    ahgora_company,
                    ahgora_password,
                ),
                timeout=SYNC_TIMEOUT_MAX * 60,
            )

            if result.success:
                async with self._db_lock:
                    await self.repo.evaluate_and_update_job_status(
                        job_id, result.message
                    )
                await self._log(
                    job_id,
                    "INFO",
                    f"Job {job_id} finished successfully: {result.message}",
                )
            else:
                # If it failed, check if we should retry (job level)
                await self._handle_job_retry(job, error_msg=result.message)

        except TimeoutError:
            error_msg = "Job timed out after 15 minutes of execution"
            logger.error(f"Job {job_id} timed out")
            try:
                await self._handle_job_retry(job, error_msg=error_msg)
            except Exception as e:
                logger.error(f"Failed to handle retry for timed out job {job_id}: {e}")

        except asyncio.CancelledError:
            logger.info(f"Job {job_id} was cancelled")
            try:
                async with self._db_lock:
                    await self.repo.update_job_status(
                        job_id, SyncStatus.CANCELLED, "Job was cancelled by user"
                    )
                await self._log(job_id, "WARNING", "Job was cancelled")
            except Exception as e:
                logger.error(f"Failed to update status for cancelled job {job_id}: {e}")
            raise  # Re-raise to finalize task cancellation

        except Exception as e:
            logger.exception(f"Unhandled error in job {job_id}")
            try:
                # Rollback any pending failed transaction to allow retry update
                if hasattr(self.repo, "session"):
                    await self.repo.session.rollback()
            except Exception as rollback_e:
                logger.error(f"Failed to rollback session: {rollback_e}")

            # Ensure status is UPDATED even if something very bad happens
            try:
                # Handle retry even for unhandled errors
                await self._handle_job_retry(job, error_msg=str(e))
            except Exception as inner_e:
                logger.error(f"Failed to handle retry for job {job_id}: {inner_e}")
        finally:
            task_registry.unregister(job_id)

    async def _handle_job_retry(self, job: SyncJob, error_msg: Optional[str] = None):
        """Calculates next retry and updates job if retries are available."""
        is_auth_error = False
        if error_msg:
            err_msg_lower = error_msg.lower()
            if (
                "inválidos" in err_msg_lower
                or "invalidos" in err_msg_lower
                or "credentials not set" in err_msg_lower
                or "credentials missing" in err_msg_lower
                or "credenciais não" in err_msg_lower
            ):
                is_auth_error = True

        if job.retry_count >= self.MAX_JOB_RETRIES or is_auth_error:
            final_msg = error_msg or "Max retries reached"
            async with self._db_lock:
                await self.repo.update_job_status(job.id, SyncStatus.FAILED, final_msg)
            await self._log(job.id, "ERROR", f"Sync failed permanently: {final_msg}")
            return

        # Exponential backoff: 5m, 30m, 1h
        backoffs = [
            timedelta(minutes=5),
            timedelta(minutes=30),
            timedelta(hours=1),
        ]

        delay = backoffs[job.retry_count]  # current retry_count is 0..2
        next_retry = datetime.now() + delay

        async with self._db_lock:
            await self.repo.increment_job_retry(job.id, next_retry)
        await self._log(
            job.id,
            "WARNING",
            f"Job failed. Scaled retry {job.retry_count + 1}/{self.MAX_JOB_RETRIES} scheduled for {next_retry}",
        )

    async def kill_job(self, job_id: UUID) -> bool:
        """Cancels a running job and updates its status to CANCELLED."""
        logger.info(f"Attempting to kill job {job_id}...")
        task = task_registry.get_task(job_id)

        # Check if job exists in DB and is in a state that can be cancelled
        job = await self.repo.get_job(job_id)
        if not job:
            logger.warning(f"Kill failed: Job {job_id} not found in database")
            return False

        is_zombie = task is None and job.status in (
            SyncStatus.RUNNING,
            SyncStatus.RETRYING,
        )

        if not task and not is_zombie:
            active_tasks = task_registry.get_all_tasks()
            logger.warning(
                f"Kill skipped: Job {job_id} is {job.status} and not in registry. "
                f"Currently registered: {list(active_tasks.keys())}"
            )
            return False

        if is_zombie:
            logger.info(
                f"Cleaning up zombie job {job_id} (marked RUNNING in DB but missing from registry)"
            )
        elif task:
            logger.info(f"Killing active task for job {job_id}")
            task.cancel()
        else:
            logger.warning(
                f"Kill failed: Job {job_id} task not found and not a zombie."
            )
            return False

        # Update the status to CANCELLED
        async with self._db_lock:
            log_msg = "Job was killed/cancelled by user request"
            await self.repo.update_job_status(job_id, SyncStatus.CANCELLED, log_msg)
            await self._log(job_id, "WARNING", log_msg)

        return True

    async def kill_all_jobs(self) -> int:
        """Cancels all active sync jobs in memory and cleans up running jobs in DB."""
        # 1. Kill tasks in registry
        tasks = task_registry.get_all_tasks()
        logger.info(
            f"Attempting to kill all {len(tasks)} registered tasks: {list(tasks.keys())}"
        )

        count = 0
        registry_job_ids = set()
        for job_id_str in tasks.keys():
            job_id = UUID(job_id_str)
            registry_job_ids.add(job_id)
            if await self.kill_job(job_id):
                count += 1

        # 2. Cleanup any other jobs marked as RUNNING in the database
        jobs = await self.repo.list_jobs()
        for job in jobs:
            if (
                job.status
                in (SyncStatus.RUNNING, SyncStatus.RETRYING, SyncStatus.PENDING)
            ) and job.id not in registry_job_ids:
                logger.info(
                    f"Cleaning up untracked RUNNING/RETRYING job {job.id} from database"
                )
                if await self.kill_job(job.id):
                    count += 1

        return count

    async def _log(self, job_id: UUID, level: str, message: str):
        # Log to standard logging
        log_func = getattr(logger, level.lower(), logger.info)
        log_func(f"Job {job_id}: {message}")

        # Persist to DB
        async with self._db_lock:
            await self.repo.add_log(job_id, level, message)

    def _is_download_cached(
        self, patterns: list[str], MAX_AGE_MINUTES: int = MAX_AGE_MINUTES
    ) -> bool:
        """
        Check if files matching the given patterns exist in the downloads directory
        and are newer than max_age_minutes.
        Returns True ONLY if all required patterns have a matching valid file.
        """
        if not settings.USE_CACHED_FILES or not patterns:
            return False

        now = datetime.now()
        found_patterns = 0

        search_dirs = []
        if hasattr(settings, "DOWNLOADS_DIR") and settings.DOWNLOADS_DIR.exists():
            search_dirs.append(settings.DOWNLOADS_DIR)
        if hasattr(settings, "DATA_DIR") and settings.DATA_DIR.exists():
            search_dirs.append(settings.DATA_DIR)

        if not search_dirs:
            return False

        for pattern_set in patterns:
            pattern_found = False
            alternatives = [p.strip().lower() for p in pattern_set.split("|")]

            for directory in search_dirs:
                if pattern_found:
                    break
                for file_path in directory.iterdir():
                    if not file_path.is_file():
                        continue

                    filename = file_path.name.lower()
                    if any(alt in filename for alt in alternatives):
                        mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                        if (now - mtime).total_seconds() <= MAX_AGE_MINUTES * 60:
                            pattern_found = True
                            break
            if pattern_found:
                found_patterns += 1

        return found_patterns == len(patterns)

    async def _execute_sync_logic(
        self,
        job_id: UUID,
        fiorilli_url: str,
        fiorilli_user: str,
        fiorilli_password: str,
        ahgora_url: str,
        ahgora_user: str,
        ahgora_company: str,
        ahgora_password: str,
    ) -> SyncResult:
        async def run_download_task_with_retries(
            browser_class,
            method_name,
            description,
            url,
            user,
            password,
            company=None,
            patterns=None,
            max_retries=3,
        ):
            if patterns and self._is_download_cached(patterns):
                await self._log(
                    job_id,
                    "INFO",
                    f"Skipping {description} (valid cached ({MAX_AGE_MINUTES} minutes) files found: {patterns})",
                )
                return True

            last_error = None
            for attempt in range(1, max_retries + 1):
                await self._log(
                    job_id,
                    "INFO",
                    f"Starting {description} (Attempt {attempt}/{max_retries})",
                )

                def blocking_wrapper():
                    # Pass the respective credentials to the browser
                    if browser_class == FiorilliBrowser:
                        browser = browser_class(
                            fiorilli_url=url,
                            fiorilli_user=user,
                            fiorilli_password=password,
                        )
                    elif browser_class == AhgoraBrowser:
                        browser = browser_class(
                            ahgora_url=url,
                            ahgora_user=user,
                            ahgora_company=company,
                            ahgora_password=password,
                        )
                    else:
                        browser = browser_class()

                    try:
                        getattr(browser, method_name)()
                    finally:
                        browser.close_driver()

                try:
                    await asyncio.to_thread(blocking_wrapper)
                    await self._log(
                        job_id, "INFO", f"Completed {description} on attempt {attempt}"
                    )
                    return True  # Success
                except Exception as e:
                    last_error = e
                    await self._log(
                        job_id,
                        "WARNING",
                        f"Attempt {attempt}/{max_retries} for {description} failed: {str(e)}",
                    )
                    err_msg_lower = str(e).lower()
                    if "inválidos" in err_msg_lower or "invalidos" in err_msg_lower:
                        break
                    if attempt < max_retries:
                        await asyncio.sleep(10)  # Short wait before retry (10s backoff)

            await self._log(
                job_id,
                "ERROR",
                f"All {max_retries} attempts failed for {description}: {str(last_error)}",
            )
            raise last_error

        try:
            if settings.HEADLESS_MODE:
                await self._log(
                    job_id, "INFO", "Running tasks concurrently (Headless Mode)"
                )
                await asyncio.gather(
                    run_download_task_with_retries(
                        FiorilliBrowser,
                        "download_employees",
                        "Fiorilli employees download",
                        fiorilli_url,
                        fiorilli_user,
                        fiorilli_password,
                        patterns=["trabalhador|fiorilli_employees"],
                    ),
                    run_download_task_with_retries(
                        FiorilliBrowser,
                        "download_leaves",
                        "Fiorilli leaves download",
                        fiorilli_url,
                        fiorilli_user,
                        fiorilli_password,
                        patterns=[
                            "pontoafastamentos|raw_leaves",
                            "pontoferias|raw_vacations",
                        ],
                    ),
                    run_download_task_with_retries(
                        AhgoraBrowser,
                        "download_employees",
                        "Ahgora employees download",
                        ahgora_url,
                        ahgora_user,
                        ahgora_password,
                        ahgora_company,
                        patterns=["funcionarios|ahgora_employees"],
                    ),
                )
            else:
                await self._log(job_id, "INFO", "Running tasks sequentially (UI Mode)")
                await run_download_task_with_retries(
                    FiorilliBrowser,
                    "download_employees",
                    "Fiorilli employees download",
                    fiorilli_url,
                    fiorilli_user,
                    fiorilli_password,
                    patterns=["trabalhador|fiorilli_employees"],
                )
                await run_download_task_with_retries(
                    FiorilliBrowser,
                    "download_leaves",
                    "Fiorilli leaves download",
                    fiorilli_url,
                    fiorilli_user,
                    fiorilli_password,
                    patterns=[
                        "pontoafastamentos|raw_leaves",
                        "pontoferias|raw_vacations",
                    ],
                )
                await run_download_task_with_retries(
                    AhgoraBrowser,
                    "download_employees",
                    "Ahgora employees download",
                    ahgora_url,
                    ahgora_user,
                    ahgora_password,
                    ahgora_company,
                    patterns=["funcionarios|ahgora_employees"],
                )

            # 5. Run analysis and create tasks
            ahgora_csv_employees = await self._run_analysis_and_create_tasks(job_id)

            # 6. Validate Data
            await self._validate_ahgora_state(job_id, ahgora_csv_employees)

            # 7. Remove downloads from download dir
            await asyncio.to_thread(FileManager.cleanup)

            return SyncResult(
                success=True,
                status=SyncStatus.SUCCESS,
                message="Sync completed",
            )
        except Exception as e:
            logger.error(f"Sync failed after retries: {e}")
            return SyncResult(
                success=False,
                status=SyncStatus.FAILED,
                message=f"Sync failed: {str(e)}",
            )

    async def _run_analysis_and_create_tasks(self, job_id: UUID):
        try:
            await self._log(job_id, "INFO", "Starting data analysis and task creation")

            # 1. Process downloads (move files to expected locations)
            await self._log(
                job_id, "INFO", "Moving downloaded files to data directory..."
            )
            await asyncio.to_thread(FileManager.move_downloads_to_data_dir)

            # 2. Get data
            await self._log(job_id, "INFO", "Loading employee data from files...")
            fiorilli_employees, ahgora_employees = await self._get_employees_data(
                job_id
            )

            # Load Ahgora CSV independently for cross-referencing
            ahgora_csv_employees = pd.DataFrame()
            raw_ahgora_path = settings.DATA_DIR / "ahgora_employees.csv"
            if raw_ahgora_path.exists():
                ahgora_csv_employees = await asyncio.to_thread(
                    self._read_csv, raw_ahgora_path
                )
                await self._log(
                    job_id,
                    "INFO",
                    f"Loaded {len(ahgora_csv_employees)} employees from Ahgora CSV for cross-reference.",
                )

            await self._log(job_id, "INFO", "Loading leave data from files...")
            last_leaves, all_leaves = await self._get_leaves_data(job_id)

            leave_codes_path = settings.DATA_DIR / "mappings" / "leave_codes.csv"
            if leave_codes_path.exists():
                await self._log(job_id, "INFO", "Enriching leave data with codes...")
                leave_codes = await asyncio.to_thread(
                    self._read_csv, leave_codes_path, columns=["cod", "desc"]
                )
                all_leaves = await self._get_view_leaves(
                    leaves_df=all_leaves,
                    fiorilli_employees=fiorilli_employees,
                    leave_codes=leave_codes,
                )

            # 3. Generate Task Dataframes
            await self._log(
                job_id, "INFO", "Generating task dataframes (comparing datasets)..."
            )
            (
                new_employees_df,
                seed_employees_df,
                dismissed_employees_df,
                changed_employees_df,
                new_leaves_df,
            ) = await self._generate_tasks_dfs(
                fiorilli_employees=fiorilli_employees,
                ahgora_employees=ahgora_employees,
                ahgora_csv_employees=ahgora_csv_employees,
                last_leaves=last_leaves,
                all_leaves=all_leaves,
            )

            # 4. Create and persist AutomationTasks
            await self._log(
                job_id, "INFO", "Persisting automation tasks to database..."
            )
            await self._create_automation_tasks(
                job_id,
                new_employees_df,
                seed_employees_df,
                dismissed_employees_df,
                changed_employees_df,
                new_leaves_df,
            )

            await self._log(
                job_id, "INFO", "Data analysis and task creation completed successfully"
            )

            return ahgora_csv_employees

        except Exception as e:
            error_msg = f"Critical error during analysis phase: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await self._log(job_id, "ERROR", error_msg)
            raise

    async def _get_employees_data(
        self, job_id: UUID
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        try:
            raw_fiorilli_path = settings.DATA_DIR / "fiorilli_employees.txt"

            if not raw_fiorilli_path.exists():
                await self._log(
                    job_id, "WARNING", f"Fiorilli file not found: {raw_fiorilli_path}"
                )
                return pd.DataFrame(), pd.DataFrame()

            fiorilli_employees = await asyncio.to_thread(
                self._read_csv, raw_fiorilli_path
            )

            # Load from Database State
            ahgora_employees = await self.repo.get_ahgora_employees_df()

            if ahgora_employees.empty:
                ahgora_employees = pd.DataFrame(columns=AHGORA_EMPLOYEES_COLUMNS)
                await self._log(job_id, "INFO", "Database Ahgora state is empty.")
            else:
                await self._log(job_id, "INFO", "Ahgora state loaded from PostgreSQL.")

            await self._log(
                job_id,
                "INFO",
                f"Loaded {len(fiorilli_employees)} Fiorilli employees and {len(ahgora_employees)} Ahgora employees",
            )
            return fiorilli_employees, ahgora_employees
        except Exception as e:
            await self._log(job_id, "ERROR", f"Error getting employee data: {str(e)}")
            return pd.DataFrame(), pd.DataFrame()

    async def _get_leaves_data(self, job_id: UUID) -> Tuple[pd.DataFrame, pd.DataFrame]:
        try:
            raw_leaves_path = settings.DATA_DIR / "raw_leaves.txt"
            raw_vacations_path = settings.DATA_DIR / "raw_vacations.txt"

            # 1. Try to load historical leaves from Database State
            last_leaves = await self.repo.get_ahgora_leaves_df()

            # 2. Fallback to legacy CSV if Database is empty (initial seed)
            if last_leaves.empty:
                last_leaves_path = settings.DATA_DIR / "leaves.csv"
                if last_leaves_path.exists():
                    await self._log(
                        job_id,
                        "INFO",
                        "Database Ahgora leaves empty, seeding from legacy CSV.",
                    )
                    last_leaves = await asyncio.to_thread(
                        self._read_csv, last_leaves_path
                    )
                else:
                    await self._log(
                        job_id,
                        "INFO",
                        "No historical leaves found in DB or legacy CSV.",
                    )
            else:
                await self._log(
                    job_id,
                    "INFO",
                    f"Loaded {len(last_leaves)} historical leaves from PostgreSQL.",
                )

            all_leaves_list = []
            if raw_vacations_path.exists():
                df_vac = await asyncio.to_thread(self._read_csv, raw_vacations_path)
                all_leaves_list.append(df_vac)
                await self._log(job_id, "INFO", f"Loaded {len(df_vac)} new vacations")

            if raw_leaves_path.exists():
                df_leaves = await asyncio.to_thread(self._read_csv, raw_leaves_path)
                all_leaves_list.append(df_leaves)
                await self._log(
                    job_id, "INFO", f"Loaded {len(df_leaves)} new leaves/absences"
                )

            all_leaves = (
                pd.concat(all_leaves_list) if all_leaves_list else pd.DataFrame()
            )
            if not all_leaves.empty:
                await self._log(
                    job_id,
                    "INFO",
                    f"Total combined leaves for process: {len(all_leaves)}",
                )

            return last_leaves, all_leaves
        except Exception as e:
            await self._log(job_id, "ERROR", f"Error getting leave data: {str(e)}")
            return pd.DataFrame(), pd.DataFrame()

    def _read_csv(
        self,
        path: Path,
        sep: str = ",",
        encoding: str = "utf-8",
        header: Any = "infer",
        columns: list[str] = [],
    ) -> pd.DataFrame:
        df = pd.DataFrame()
        try:
            match path.name:
                case "fiorilli_employees.txt":
                    df = pd.read_csv(
                        path,
                        sep="|",
                        encoding="latin1",
                        index_col=False,
                        header=None,
                    )
                    df = self._prepare_dataframe(df, columns=FIORILLI_EMPLOYEES_COLUMNS)

                case "ahgora_employees.csv":
                    df = pd.read_csv(path, index_col=False, header=None)
                    df = self._prepare_dataframe(df, columns=AHGORA_EMPLOYEES_COLUMNS)

                case "leaves.csv":
                    df = pd.read_csv(path, index_col=False, header=None)
                    df = self._prepare_dataframe(df, columns=LEAVES_COLUMNS)

                case "raw_vacations.txt" | "raw_leaves.txt":
                    df = pd.read_csv(path, index_col=False, header=None)
                    df = self._prepare_dataframe(df, columns=UPLOAD_LEAVES_COLUMNS)

                case _:
                    df = pd.read_csv(
                        path,
                        sep=sep,
                        encoding=encoding,
                        index_col=False,
                        header=header,
                    )
                    if columns:
                        df = self._prepare_dataframe(df, columns=columns)
        except Exception as e:
            logger.error(f"Error reading CSV {path}: {e}")
            return pd.DataFrame()

        return df

    def _prepare_dataframe(
        self,
        df: pd.DataFrame,
        columns: list[str] = [],
    ) -> pd.DataFrame:
        try:
            if columns:
                if len(df.columns) != len(columns):
                    logger.warning(
                        f"Column mismatch: expected {len(columns)} got {len(df.columns)}"
                    )
                df.columns = columns[: len(df.columns)]  # defensive

            for col in df.columns:
                if "date" in col:
                    df[col] = df[col].apply(self._convert_date)
                    df[col] = pd.to_datetime(
                        df[col],
                        dayfirst=True,
                        format="%d/%m/%Y",
                        errors="coerce",
                    )
                    df[col] = df[col].dt.strftime("%d/%m/%Y")

            if "cpf" in df.columns:
                df["cpf"] = df["cpf"].fillna("").astype(str).str.zfill(11)

            if "cod" in df.columns:
                df["cod"] = df["cod"].fillna("").astype(str).str.zfill(3)

            if "name" in df.columns:
                df["name"] = df["name"].str.strip().str.upper()

            if "id" in df.columns:
                df["id"] = df["id"].astype(str).str.zfill(6)

            return df
        except Exception as e:
            logger.error(f"Error preparing dataframe: {e}")
            raise

    def _convert_date(self, date_str: str):
        if pd.isna(date_str) or not isinstance(date_str, str) or date_str.strip() == "":
            return pd.NaT

        partes = date_str.split(", ")
        if len(partes) > 1:
            date_str = partes[1]

        for pt, en in PT_MONTHS.items():
            date_str = date_str.replace(f"{pt}/", f"{en}/")

        formats = ["%d/%b/%Y", "%d/%m/%Y"]
        for fmt in formats:
            try:
                return pd.to_datetime(date_str, format=fmt, errors="raise")
            except ValueError:
                continue

        return pd.to_datetime(date_str, format="ISO8601", errors="coerce")

    def _normalize_text(self, text):
        if pd.isna(text):
            return np.nan
        text = str(text)  # Ensure it's a string for regex
        text = str(" ".join(re.split(r"\s+", text, flags=re.UNICODE)))
        normalized = (
            unicodedata.normalize("NFKD", text)
            .encode("ASCII", "ignore")
            .decode("ASCII")
        )
        normalized = settings.EXCEPTIONS_AND_TYPOS.get(normalized, normalized)
        return normalized.lower().strip()

    async def _generate_tasks_dfs(
        self,
        fiorilli_employees: pd.DataFrame,
        ahgora_employees: pd.DataFrame,
        ahgora_csv_employees: pd.DataFrame,
        last_leaves: pd.DataFrame,
        all_leaves: pd.DataFrame,
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        if fiorilli_employees.empty:
            return (
                pd.DataFrame(),
                pd.DataFrame(),
                pd.DataFrame(),
                pd.DataFrame(),
                pd.DataFrame(),
            )

        # Combine Ahgora state prioritizing CSV over DB
        ahgora_csv_ids = set()
        if not ahgora_csv_employees.empty:
            ahgora_csv_ids = set(ahgora_csv_employees["id"])
            db_not_in_csv = ahgora_employees[
                ~ahgora_employees["id"].isin(ahgora_csv_ids)
            ]
            common_cols = list(
                set(ahgora_employees.columns) & set(ahgora_csv_employees.columns)
            )
            combined_ahgora = pd.concat(
                [ahgora_csv_employees[common_cols], db_not_in_csv[common_cols]],
                ignore_index=True,
            )
        else:
            combined_ahgora = ahgora_employees

        # Dismissed logic
        fiorilli_dismissed_df = fiorilli_employees[
            fiorilli_employees["dismissal_date"].notna()
        ]
        fiorilli_dismissed_ids = set(fiorilli_dismissed_df["id"])

        ahgora_dismissed_df = combined_ahgora[combined_ahgora["dismissal_date"].notna()]
        ahgora_dismissed_ids = set(ahgora_dismissed_df["id"])
        dismissed_ids = ahgora_dismissed_ids | fiorilli_dismissed_ids

        fiorilli_active_employees = fiorilli_employees[
            ~fiorilli_employees["id"].isin(dismissed_ids)
        ]

        # New employees: not in DB
        ahgora_db_ids = set(ahgora_employees["id"])
        missing_from_db = fiorilli_active_employees[
            ~fiorilli_active_employees["id"].isin(ahgora_db_ids)
        ]
        missing_from_db = missing_from_db[
            missing_from_db["binding"] != "AUXILIO RECLUSAO"
        ]

        # Truly new — not in DB AND not in Ahgora CSV → need Selenium automation
        new_employees_df = missing_from_db[~missing_from_db["id"].isin(ahgora_csv_ids)]

        # DB-only seed — in Ahgora CSV but not in DB → seed Ahgora CSV data to DB
        seed_ids = ahgora_csv_ids - ahgora_db_ids
        seed_employees_df = (
            ahgora_csv_employees[ahgora_csv_employees["id"].isin(seed_ids)]
            if not ahgora_csv_employees.empty and seed_ids
            else pd.DataFrame()
        )

        # Dismissed employees
        dismissed_employees_df = combined_ahgora[
            combined_ahgora["id"].isin(fiorilli_dismissed_ids)
            & ~combined_ahgora["id"].isin(ahgora_dismissed_ids)
        ]
        if not dismissed_employees_df.empty:
            dismissed_employees_df = dismissed_employees_df.drop(
                columns=["dismissal_date"]
            )
            dismissed_employees_df = dismissed_employees_df.merge(
                fiorilli_dismissed_df[["id", "dismissal_date"]],
                on="id",
                how="left",
            )
            dismissed_employees_df["dismissal_date_dt"] = pd.to_datetime(
                dismissed_employees_df["dismissal_date"],
                format="%d/%m/%Y",
            )
            today = datetime.now()
            dismissed_employees_df = dismissed_employees_df[
                dismissed_employees_df["dismissal_date_dt"] <= today
            ]
            dismissed_employees_df = dismissed_employees_df.drop(
                columns=["dismissal_date_dt"]
            )

        # Changed employees: compare Fiorilli vs combined Ahgora state (DB + CSV)

        changed_employees_df = await self._get_changed_employees_df(
            fiorilli_active_employees, combined_ahgora
        )

        # New leaves
        new_leaves_df = await self._get_new_leaves_df(last_leaves, all_leaves)

        return (
            new_employees_df,
            seed_employees_df,
            dismissed_employees_df,
            changed_employees_df,
            new_leaves_df,
        )

    async def _get_changed_employees_df(
        self,
        fiorilli_active_employees: pd.DataFrame,
        ahgora_employees: pd.DataFrame,
    ) -> pd.DataFrame:
        merged = fiorilli_active_employees.merge(
            ahgora_employees,
            on="id",
            suffixes=("_expected", "_actual"),
            how="inner",
        )

        # Normalize date columns to dd/mm/yyyy so DB format (yyyy-mm-dd HH:MM:SS)
        # and CSV format (dd/mm/yyyy) are comparable
        for col in COLUMNS_TO_VERIFY_CHANGE:
            if "date" in col:
                for suffix in ("_expected", "_actual"):
                    col_name = f"{col}{suffix}"
                    if col_name in merged:
                        merged[col_name] = pd.to_datetime(
                            merged[col_name], dayfirst=True, errors="coerce"
                        ).dt.strftime("%d/%m/%Y")

        for col in COLUMNS_TO_VERIFY_CHANGE:
            if f"{col}_expected" in merged:
                merged[f"{col}_expected_norm"] = merged[f"{col}_expected"].apply(
                    self._normalize_text
                )
            if f"{col}_actual" in merged:
                merged[f"{col}_actual_norm"] = merged[f"{col}_actual"].apply(
                    self._normalize_text
                )

        change_conditions = []
        placeholder = "___NULL___"
        for col in COLUMNS_TO_VERIFY_CHANGE:
            f_col = f"{col}_expected_norm"
            a_col = f"{col}_actual_norm"
            if f_col in merged and a_col in merged:
                condition = merged[f_col].fillna(placeholder) != merged[a_col].fillna(
                    placeholder
                )
                change_conditions.append(condition)

        # Check location
        from app.core.settings import settings
        import csv
        import ast

        csv_path = settings.DATA_DIR / "mappings" / "department_to_location.csv"
        dept_to_loc = {}
        if csv_path.exists():
            try:
                with open(csv_path, mode="r", encoding="latin1") as f:
                    reader = csv.reader(f)
                    for row in reader:
                        if len(row) >= 2:
                            dept = row[0].strip().upper()
                            val = row[1].strip()
                            if val.startswith("[") and val.endswith("]"):
                                try:
                                    target_locations = [
                                        x.strip().upper() for x in ast.literal_eval(val)
                                    ]
                                except Exception:
                                    target_locations = [val.upper()]
                            elif val:
                                target_locations = [
                                    x.strip().upper() for x in val.split("|")
                                ]
                            else:
                                target_locations = []

                            target_locations = [x for x in target_locations if x]
                            dept_to_loc[dept] = sorted(target_locations)
            except Exception as e:
                logger.warning(
                    f"Could not read department_to_location.csv in sync_service: {e}"
                )

        def extract_ahgora_locations(val):
            if not isinstance(val, str) or pd.isna(val):
                return []
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                try:
                    locs = [x.strip().upper() for x in ast.literal_eval(val)]
                except Exception:
                    locs = [val.upper()]
            elif val:
                locs = [x.strip().upper() for x in val.split(";")]
            else:
                locs = []

            locs = [x for x in locs if x]
            return sorted(locs)

        if "location" in merged and dept_to_loc and settings.UPDATE_LOCATIONS:
            merged["location_expected"] = merged["department_expected"].apply(
                lambda x: dept_to_loc.get(
                    str(x).strip().upper() if pd.notna(x) else "", []
                )
            )
            merged["location_actual"] = merged["location"].apply(
                extract_ahgora_locations
            )
            location_condition = merged.apply(
                lambda row: (
                    len(row["location_expected"]) > 0
                    and row["location_expected"] != row["location_actual"]
                    and str(row["id"]).zfill(6)
                    not in settings.IGNORE_LOCATION_CHANGE_IDS
                ),
                axis=1,
            )
            change_conditions.append(location_condition)

        if not change_conditions:
            return pd.DataFrame(columns=merged.columns)

        combined_condition = pd.concat(change_conditions, axis=1).any(axis=1)
        return merged[combined_condition]

    async def _get_new_leaves_df(
        self,
        last_leaves: pd.DataFrame,
        all_leaves: pd.DataFrame,
    ) -> pd.DataFrame:
        if all_leaves.empty:
            return pd.DataFrame()

        for df in [last_leaves, all_leaves]:
            if df.empty:
                continue
            for col in ["start_date", "end_date"]:
                if col in df.columns:
                    df[col] = pd.to_datetime(
                        df[col], format="%d/%m/%Y", errors="coerce"
                    )

            # Apply normalize text for normalization
            for col in df.columns:
                if df[col].dtype == object:
                    df[col] = df[col].replace(settings.EXCEPTIONS_AND_TYPOS)

        if last_leaves.empty:
            all_leaves_copy = all_leaves.copy()
            for col in ["start_date", "end_date"]:
                if col in all_leaves_copy.columns:
                    all_leaves_copy[col] = (
                        all_leaves_copy[col].dt.strftime("%d/%m/%Y").fillna("")
                    )
            return all_leaves_copy

        # Filter using composite key (employee id + cod + start_date + end_date)
        last_leaves_match = last_leaves.copy()
        all_leaves_match = all_leaves.copy()

        # Ensure string comparison format
        for df in [last_leaves_match, all_leaves_match]:
            if "id" in df.columns:
                df["_id_str"] = df["id"].astype(str).str.zfill(6)
            else:
                df["_id_str"] = ""

            if "cod" in df.columns:
                df["_cod_str"] = df["cod"].astype(str).str.zfill(3)
            else:
                df["_cod_str"] = ""

            if "start_date" in df.columns:
                df["_start_str"] = df["start_date"].dt.strftime("%d/%m/%Y").fillna("")
            else:
                df["_start_str"] = ""

            if "end_date" in df.columns:
                df["_end_str"] = df["end_date"].dt.strftime("%d/%m/%Y").fillna("")
            else:
                df["_end_str"] = ""

            df["_composite"] = (
                df["_id_str"]
                + "_"
                + df["_cod_str"]
                + "_"
                + df["_start_str"]
                + "_"
                + df["_end_str"]
            )

        already_existing = all_leaves_match["_composite"].isin(
            last_leaves_match["_composite"]
        )

        # Return only the items from all_leaves that don't match the composite key
        new_leaves = all_leaves[~already_existing].copy()

        for col in ["start_date", "end_date"]:
            if col in new_leaves.columns:
                new_leaves[col] = new_leaves[col].dt.strftime("%d/%m/%Y").fillna("")

        return new_leaves

    async def _get_view_leaves(
        self,
        leaves_df: pd.DataFrame,
        fiorilli_employees: pd.DataFrame,
        leave_codes: pd.DataFrame,
    ) -> pd.DataFrame:
        if leaves_df.empty:
            return leaves_df

        leaves_df["start_date_dt"] = pd.to_datetime(
            leaves_df["start_date"], format="%d/%m/%Y", errors="coerce"
        )
        leaves_df["end_date_dt"] = pd.to_datetime(
            leaves_df["end_date"], format="%d/%m/%Y", errors="coerce"
        )

        leaves_df = leaves_df.merge(
            fiorilli_employees[["id", "name"]], on="id", how="left"
        )
        leaves_df = leaves_df.merge(
            leave_codes[["cod", "desc"]], on="cod", how="left"
        ).rename(columns={"desc": "cod_name"})

        leaves_df["duration"] = (
            leaves_df["end_date_dt"] - leaves_df["start_date_dt"]
        ).dt.days + 1
        leaves_df["duration"] = leaves_df["duration"].clip(lower=1)

        # Ensure all columns exist
        for col in LEAVES_COLUMNS:
            if col not in leaves_df.columns:
                leaves_df[col] = None

        return leaves_df[LEAVES_COLUMNS]

    async def _create_automation_tasks(
        self,
        job_id: UUID,
        new_employees_df: pd.DataFrame,
        seed_employees_df: pd.DataFrame,
        dismissed_employees_df: pd.DataFrame,
        changed_employees_df: pd.DataFrame,
        new_leaves_df: pd.DataFrame,
    ):
        tasks_to_create = []

        # Helper to create payload with numpy type sanitization
        def _sanitize_value(val):
            """Convert numpy/pandas types to native Python for JSON serialization."""
            if isinstance(val, list):
                return [_sanitize_value(x) for x in val]
            if isinstance(val, (np.ndarray,)):
                return [_sanitize_value(x) for x in val.tolist()]

            try:
                if pd.isna(val):
                    return None
            except ValueError:
                pass

            import datetime as dt  # Inline import to reach date class safely

            if isinstance(val, (pd.Timestamp, datetime, dt.date)):
                return val.strftime("%d/%m/%Y")
            if isinstance(val, (np.integer,)):
                return int(val)
            if isinstance(val, (np.floating,)):
                return float(val)
            if isinstance(val, (np.bool_,)):
                return bool(val)
            return val

        def df_to_payloads(df: pd.DataFrame) -> List[Dict[str, Any]]:
            return [
                {str(k): _sanitize_value(v) for k, v in row.to_dict().items()}
                for _, row in df.iterrows()
            ]

        # Seed employees that already exist in Ahgora but are missing from DB
        if not seed_employees_df.empty:
            seed_payloads = df_to_payloads(seed_employees_df)
            async with self._db_lock:
                await self.repo.save_ahgora_employees_batch(seed_payloads)
            await self._log(
                job_id,
                "INFO",
                f"Seeded {len(seed_payloads)} employees directly to DB (already in Ahgora).",
            )

        if not new_employees_df.empty:
            for payload in df_to_payloads(new_employees_df):
                tasks_to_create.append(
                    AutomationTask(
                        job_id=job_id,
                        type=AutomationTaskType.ADD_EMPLOYEE,
                        payload=payload,
                    )
                )

        if not dismissed_employees_df.empty:
            for payload in df_to_payloads(dismissed_employees_df):
                tasks_to_create.append(
                    AutomationTask(
                        job_id=job_id,
                        type=AutomationTaskType.REMOVE_EMPLOYEE,
                        payload=payload,
                    )
                )

        if not changed_employees_df.empty:
            for payload in df_to_payloads(changed_employees_df):
                payload["name"] = payload.get("name_expected")
                tasks_to_create.append(
                    AutomationTask(
                        job_id=job_id,
                        type=AutomationTaskType.UPDATE_EMPLOYEE,
                        payload=payload,
                    )
                )

        if not new_leaves_df.empty:
            leaves_payloads = df_to_payloads(new_leaves_df)
            tasks_to_create.append(
                AutomationTask(
                    job_id=job_id,
                    type=AutomationTaskType.ADD_LEAVE,
                    payload={"name": "AFASTAMENTOS", "leaves": leaves_payloads},
                )
            )

        # Save all tasks in a single batch commit
        if tasks_to_create:
            async with self._db_lock:
                await self.repo.save_automation_tasks_batch(tasks_to_create)

        job = await self.get_job(job_id)
        if job:
            job.metadata_info["tasks_generated"] = len(tasks_to_create)
            async with self._db_lock:
                await self.repo.save_job(job)

        await self._log(
            job_id, "INFO", f"Created {len(tasks_to_create)} automation tasks"
        )

    async def _validate_ahgora_state(
        self, job_id: UUID, ahgora_employees: pd.DataFrame
    ):
        try:
            await self._log(job_id, "INFO", "Validating CSV vs DB state")

            if ahgora_employees is None or ahgora_employees.empty:
                await self._log(
                    job_id, "WARNING", "No Ahgora CSV data available to validate"
                )
                return

            # Get current DB state
            db_employees = await self.repo.get_ahgora_employees_df()

            if db_employees.empty:
                await self._log(
                    job_id, "WARNING", "No DB state available to validate against CSV"
                )
                return

            # Find discrepancies (e.g. employee in CSV but not in DB, or vice-versa)
            csv_ids = set(ahgora_employees["id"].astype(str))
            db_ids = set(db_employees["id"].astype(str))

            missing_in_db = csv_ids - db_ids
            missing_in_csv = db_ids - csv_ids

            if missing_in_db:
                logger.warning(
                    f"Found {len(missing_in_db)} employees in Ahgora CSV not present in DB."
                )
                await self._log(
                    job_id,
                    "WARNING",
                    f"Validation: {len(missing_in_db)} employees in Ahgora CSV not present in DB.",
                )

            if missing_in_csv:
                logger.warning(
                    f"Found {len(missing_in_csv)} employees in DB not present in Ahgora CSV."
                )
                await self._log(
                    job_id,
                    "WARNING",
                    f"Validation: {len(missing_in_csv)} employees in DB not present in Ahgora CSV. Check if dismissal syncing failed.",
                )

            # Validate column discrepancies for matching IDs
            common_ids = csv_ids.intersection(db_ids)
            if common_ids:
                # Filter DFs
                csv_common = ahgora_employees[
                    ahgora_employees["id"].astype(str).isin(common_ids)
                ]
                db_common = db_employees[
                    db_employees["id"].astype(str).isin(common_ids)
                ]

                # Check discrepancies using the _get_changed_employees_df logic
                discrepancies_df = await self._get_changed_employees_df(
                    fiorilli_active_employees=csv_common,  # Treat CSV as source of truth for this comparison
                    ahgora_employees=db_common,
                )

                if not discrepancies_df.empty:
                    mismatch_count = len(discrepancies_df)
                    await self._log(
                        job_id,
                        "WARNING",
                        f"Validation: Found {mismatch_count} employees with data discrepancies between DB and Ahgora CSV.",
                    )
                    # Log IDs for debugging
                    mismatched_ids = discrepancies_df["id"].tolist()
                    logger.warning(f"Data discrepancies for IDs: {mismatched_ids}")
                else:
                    await self._log(
                        job_id,
                        "INFO",
                        "Validation: DB and Ahgora CSV state are completely synchronized for common employees.",
                    )

        except Exception as e:
            logger.error(
                f"Error during Ahgora state validation: {str(e)}", exc_info=True
            )
            await self._log(job_id, "ERROR", f"Validation check failed: {str(e)}")
