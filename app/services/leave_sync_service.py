import asyncio
import logging
import tempfile
import threading
from pathlib import Path
from typing import Optional
from uuid import UUID

import pandas as pd

from app.core.settings import settings
from app.core.task_registry import task_registry
from app.domain.enums import AutomationTaskStatus
from app.infrastructure.automation.web.ahgora_browser import AhgoraBrowser
from app.infrastructure.db.sqlalchemy_repo import SqlAlchemyRepo

logger = logging.getLogger(__name__)


class LeaveSyncService:
    def __init__(self, repo: SqlAlchemyRepo):
        self.repo = repo

    async def execute_leaves_batch(
        self,
        job_id: UUID,
        ahgora_user: Optional[str] = None,
        ahgora_password: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        ahgora_url: Optional[str] = None,
    ) -> None:
        """
        Executes all pending ADD_LEAVE tasks as a single batch using the Ahgora file importer.
        """
        logger.info(f"Starting batched leaf upload for job {job_id}")

        # 1. Fetch pending batch task
        tasks = await self.repo.get_automation_tasks_by_job(job_id)
        batch_task = next(
            (
                t
                for t in tasks
                if str(t.type).upper().endswith("ADD_LEAVE")
                and t.status
                in [
                    AutomationTaskStatus.PENDING,
                    AutomationTaskStatus.FAILED,
                    AutomationTaskStatus.CANCELLED,
                ]
            ),
            None,
        )

        if not batch_task:
            logger.info("No pending ADD_LEAVE batch task found.")
            return

        batch_payloads = batch_task.payload.get("leaves", [])
        if not batch_payloads:
            logger.info("ADD_LEAVE batch task has an empty payload array.")
            await self.repo.update_task_status(
                batch_task.id,
                AutomationTaskStatus.SUCCESS,
                message="Não há afastamentos para importar",
            )
            return

        await self.repo.update_task_status(batch_task.id, AutomationTaskStatus.RUNNING)
        await self.repo.add_log(
            job_id,
            "INFO",
            f"Iniciando a importação de {len(batch_payloads)} afastamentos no Ahgora",
            task_id=batch_task.id,
        )

        cancel_event = task_registry.get_cancel_event(job_id)
        if not cancel_event:
            cancel_event = threading.Event()
            task_registry.register_cancel_event(job_id, cancel_event)

        # 2. Run browser automation in thread
        loop = asyncio.get_running_loop()
        log_lock = asyncio.Lock()
        try:
            results = await asyncio.to_thread(
                self._run_browser_batch_import,
                batch_payloads,
                job_id,
                batch_task.id,
                loop,
                log_lock,
                cancel_event,
                ahgora_user=ahgora_user,
                ahgora_password=ahgora_password,
                ahgora_company=ahgora_company,
                ahgora_url=ahgora_url,
            )

            # Analyze results and update task statuses
            # results is a list of dicts: [{'payload': {...}, 'status': 'success' or 'error', 'message': '...', 'index': 0}]

            imported_count = 0
            ignored_count = 0
            error_count = 0

            successful_payloads = []

            for result in results:
                name = result["payload"].get("name", "N/A")
                start = result["payload"].get("start_date", "N/A")
                end = result["payload"].get("end_date", "N/A")
                cod_name = result["payload"].get("cod_name", "N/A")

                if result["status"] == "success":
                    if "Intersecção" in result["message"]:
                        ignored_count += 1
                        # Silent ignore, do not log individually
                    else:
                        imported_count += 1
                        successful_payloads.append(result["payload"])
                        await self.repo.add_log(
                            job_id,
                            "INFO",
                            f"Afastamento importado: {name} - {cod_name} - {start} a {end}",
                            task_id=batch_task.id,
                        )
                else:
                    error_count += 1
                    err_msg = result["message"]
                    await self.repo.add_log(
                        job_id,
                        "ERROR",
                        f"Falha ao importar o afastamento de {name}: {err_msg}",
                        task_id=batch_task.id,
                    )

            final_msg = f"Importação concluída: {imported_count} importados, {ignored_count} já existentes ignorados, {error_count} com erro"
            await self.repo.add_log(job_id, "INFO", final_msg, task_id=batch_task.id)

            batch_task.payload["leaves"] = successful_payloads

            # Save successfully imported leaves to DB state
            if successful_payloads:
                await self.repo.save_ahgora_leaves_batch(successful_payloads)
                await self.repo.add_log(
                    job_id,
                    "INFO",
                    f"{len(successful_payloads)} afastamentos salvos no banco de dados",
                    task_id=batch_task.id,
                )

            if error_count == len(batch_payloads) and len(batch_payloads) > 0:
                await self.repo.update_task_status(
                    batch_task.id,
                    AutomationTaskStatus.FAILED,
                    message="Todos os afastamentos falharam",
                    payload=batch_task.payload,
                )
            else:
                await self.repo.update_task_status(
                    batch_task.id,
                    AutomationTaskStatus.SUCCESS,
                    message=final_msg,
                    payload=batch_task.payload,
                )

        except Exception as e:
            logger.exception(f"Batched leaf sync failed catastrophically: {e}.")
            await self.repo.update_task_status(
                batch_task.id, AutomationTaskStatus.FAILED, message=str(e)
            )
            await self.repo.add_log(
                job_id,
                "ERROR",
                f"Falha crítica na importação de afastamentos: {str(e)}",
                task_id=batch_task.id,
            )

        await self.repo.evaluate_and_update_job_status(job_id)

    def _run_browser_batch_import(
        self,
        batch_payloads: list[dict],
        job_id: UUID,
        task_id: UUID,
        loop: asyncio.AbstractEventLoop,
        log_lock: asyncio.Lock,
        cancel_event: Optional[threading.Event] = None,
        ahgora_user: Optional[str] = None,
        ahgora_password: Optional[str] = None,
        ahgora_company: Optional[str] = None,
        ahgora_url: Optional[str] = None,
    ) -> list[dict]:
        """
        Sync execution of the browser automation for leaves batch.
        """

        async def safe_log(level: str, msg: str):
            async with log_lock:
                await self.repo.add_log(job_id, level, msg, task_id=task_id)

        def log_cb(level: str, msg: str):
            asyncio.run_coroutine_threadsafe(safe_log(level, msg), loop)

        df = pd.DataFrame(batch_payloads)
        results = []
        for i, row in enumerate(batch_payloads):
            results.append(
                {
                    "payload": row,
                    "status": "success",
                    "message": "",
                    "index": i,
                }
            )

        browser = AhgoraBrowser(
            ahgora_user=ahgora_user,
            ahgora_password=ahgora_password,
            ahgora_company=ahgora_company,
            ahgora_url=ahgora_url,
            log_callback=log_cb,
            headless=settings.HEADLESS_MODE_TASKS,
            cancel_event=cancel_event,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                temp_path = Path(temp_dir)

                initial_file = temp_path / "upload_leaves_initial.csv"
                final_file = temp_path / "upload_leaves_final.csv"

                # Ensure required columns exist, mapping from payload back to UPLOAD_LEAVES_COLUMNS
                upload_cols = settings.UPLOAD_LEAVES_COLUMNS

                # Filter/Map columns
                export_df = df.copy()
                for col in upload_cols:
                    if col not in export_df.columns:
                        export_df[col] = ""
                    if col in ["start_date", "end_date"]:
                        export_df[col] = pd.to_datetime(
                            export_df[col], format="mixed", dayfirst=True
                        )
                        export_df[col] = export_df[col].dt.strftime("%d/%m/%Y")

                # 1. Generate initial CSV
                export_df[upload_cols].to_csv(
                    initial_file, index=False, header=False, sep=","
                )

                # 2. Upload initial CSV
                browser.upload_leaves_file(str(initial_file))

                # 3. Extract errors
                import_errors = browser.extract_import_errors()

                # import_errors format: [{'row': 10, 'error': 'Intersecção...'}]
                error_rows = {err["row"] for err in import_errors}

                # Mark errors in results
                # Note: Ahgora row errors are 1-indexed.
                for err in import_errors:
                    idx = err["row"] - 1
                    if 0 <= idx < len(results):
                        results[idx]["status"] = "error"
                        results[idx]["message"] = err["error"]

                # 4. Filter DF
                # Drop rows that had errors (1-indexed matching)
                valid_indices = [
                    i for i in range(len(export_df)) if (i + 1) not in error_rows
                ]

                if not valid_indices:
                    log_cb("INFO", "Nenhum afastamento novo para importar")
                    for result in results:
                        result["status"] = "success"
                    return results

                final_df = export_df.iloc[valid_indices]

                # 5. Generate final CSV
                final_df[upload_cols].to_csv(
                    final_file, index=False, header=False, sep=","
                )

                # 6. Upload final CSV and Confirm
                browser.upload_leaves_file(str(final_file))

                # Optionally wait, then confirm
                browser.confirm_import()

                # Update leaves results
                for result in results:
                    if (
                        "Intersecção com afastamento existente no registro".lower().strip()
                        in result["message"].lower().strip()
                    ):
                        result["status"] = "success"

                return results

            except Exception as e:
                log_cb("ERROR", f"Falha no processo de importação: {e}")
                for result in results:
                    result["status"] = "failed"
                raise e
            finally:
                browser.close_driver()
