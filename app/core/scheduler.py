import asyncio
import logging

from app.core.database import async_session_factory
from app.core.settings import settings
from app.domain.enums import SyncStatus
from app.infrastructure.db.sqlalchemy_repo import SqlAlchemyRepo
from app.services.credential_crypto import (
    decrypt_password,
    extract_credentials_from_metadata,
)

logger = logging.getLogger(__name__)


class RetryScheduler:
    def __init__(self, interval_seconds: int = 60):
        self.interval_seconds = interval_seconds
        self._running = False
        self._task = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(f"Retry Scheduler started (interval: {self.interval_seconds}s)")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Retry Scheduler stopped")

    async def _run(self):
        while self._running:
            try:
                await self._check_and_retry_jobs()
            except Exception as e:
                logger.error(f"Error in Retry Scheduler: {e}")

            await asyncio.sleep(self.interval_seconds)

    async def _check_and_retry_jobs(self):
        async with async_session_factory() as session:
            repo = SqlAlchemyRepo(session)
            ready_jobs = await repo.get_jobs_ready_for_retry()

            if not ready_jobs:
                return

            logger.info(f"Found {len(ready_jobs)} jobs ready for retry")

            for job in ready_jobs:
                logger.info(
                    f"Scheduling retry for job {job.id} (Attempt {job.retry_count + 1})"
                )

                creds = extract_credentials_from_metadata(job.metadata_info)
                if creds is None:
                    logger.warning(
                        f"Job {job.id} has no stored credentials; marking FAILED."
                    )
                    await repo.update_job_status(
                        job.id,
                        SyncStatus.FAILED,
                        "No stored credentials available for retry",
                    )
                    continue

                fiorilli_password, ahgora_password = creds
                final_fiorilli_password = fiorilli_password
                final_ahgora_password = ahgora_password

                fiorilli_url = settings.FIORILLI_URL
                ahgora_url = settings.AHGORA_URL
                fiorilli_user = None
                ahgora_user = None
                ahgora_company = settings.AHGORA_COMPANY

                # Get user-specific credentials if available, otherwise fall back to global settings
                if job.user_id:
                    user_creds = await repo.get_user_credentials(job.user_id)
                    if user_creds:
                        # Decrypt passwords from user credentials
                        if user_creds.get("fiorilli_password_encrypted"):
                            try:
                                final_fiorilli_password = decrypt_password(
                                    user_creds["fiorilli_password_encrypted"]
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to decrypt Fiorilli password for user {job.user_id}: {e}"
                                )
                        if user_creds.get("ahgora_password_encrypted"):
                            try:
                                final_ahgora_password = decrypt_password(
                                    user_creds["ahgora_password_encrypted"]
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to decrypt Ahgora password for user {job.user_id}: {e}"
                                )

                        # Use user urls if available, otherwise fall back to stored ones
                        fiorilli_url = (
                            user_creds.get("fiorilli_url") or settings.FIORILLI_URL
                        )
                        ahgora_url = user_creds.get("ahgora_url") or settings.AHGORA_URL

                        # Use decrypted passwords if available, otherwise fall back to stored ones
                        fiorilli_user = user_creds.get("fiorilli_user")
                        ahgora_user = user_creds.get("ahgora_user")
                        ahgora_company = (
                            user_creds.get("ahgora_company") or settings.AHGORA_COMPANY
                        )

                    else:
                        # No user credentials found, fall back to global settings for URLs only.
                        global_settings = await repo.get_global_settings()
                        if global_settings is None:
                            logger.error(
                                f"Neither user nor global settings found for job {job.id}; marking FAILED."
                            )
                            await repo.update_job_status(
                                job.id,
                                SyncStatus.FAILED,
                                "User and global settings not found",
                            )
                            continue
                        # Global settings only provide URLs; usernames/passwords must come from user credentials or job metadata.
                        # Without usernames/passwords we cannot proceed
                        logger.error(
                            f"User credentials missing for job {job.id}; marking FAILED."
                        )
                        await repo.update_job_status(
                            job.id,
                            SyncStatus.FAILED,
                            "User credentials not found; global settings do not contain usernames/passwords",
                        )
                        continue
                else:
                    # No user_id in job (legacy job), fall back to global settings
                    global_settings = await repo.get_global_settings()
                    if global_settings is None:
                        logger.error(
                            f"Global settings not found for job {job.id}; marking FAILED."
                        )
                        await repo.update_job_status(
                            job.id,
                            SyncStatus.FAILED,
                            "Global settings not found",
                        )
                        continue
                    fiorilli_url = global_settings.fiorilli_url or settings.FIORILLI_URL
                    ahgora_url = global_settings.ahgora_url or settings.AHGORA_URL
                    ahgora_company = (
                        global_settings.ahgora_company or settings.AHGORA_COMPANY
                    )

                from app.services.sync_service import SyncService

                asyncio.create_task(
                    SyncService.run_sync_task_standalone(
                        job.id,
                        fiorilli_url,
                        fiorilli_user,
                        final_fiorilli_password,
                        ahgora_url,
                        ahgora_user,
                        ahgora_company,
                        final_ahgora_password,
                    )
                )


scheduler = RetryScheduler()
