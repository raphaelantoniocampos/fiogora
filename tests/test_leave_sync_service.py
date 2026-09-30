import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pandas as pd

from app.domain.enums import AutomationTaskStatus
from app.services.leave_sync_service import LeaveSyncService


@pytest.mark.asyncio
async def test_execute_leaves_batch_success():
    """
    Test that execute_leaves_batch correctly fetches pending tasks, runs the browser,
    and updates statuses to SUCCESS when there are no errors.
    """
    repo = MagicMock()
    job_id = uuid4()
    task_id = uuid4()

    # Mocking task
    class MockTask:
        def __init__(self, id, type, status, payload):
            self.id = id
            self.type = type
            self.status = status
            self.payload = payload

    task = MockTask(
        id=task_id,
        type="ADD_LEAVE",
        status=AutomationTaskStatus.PENDING,
        payload={
            "leaves": [
                {
                    "id": "000001",
                    "cod": "001",
                    "start_date": "01/01/2026",
                    "end_date": "10/01/2026",
                }
            ]
        },
    )

    repo.get_automation_tasks_by_job = AsyncMock(return_value=[task])
    repo.get_ahgora_leaves_df = AsyncMock(return_value=pd.DataFrame([{"id": "000001"}]))
    repo.update_task_status = AsyncMock()
    repo.save_ahgora_leaves_batch = AsyncMock()
    repo.evaluate_and_update_job_status = AsyncMock()
    repo.add_log = AsyncMock()

    service = LeaveSyncService(repo=repo)

    # Mock _run_browser_batch_import to return success
    service._run_browser_batch_import = MagicMock(
        return_value=[
            {"payload": task.payload, "status": "success", "message": "", "index": 0}
        ]
    )
    await service.execute_leaves_batch(job_id)

    # Assert repo methods were called to update status
    # First to RUNNING, then to SUCCESS
    repo.update_task_status.assert_any_call(task_id, AutomationTaskStatus.RUNNING)
    repo.update_task_status.assert_any_call(
        task_id,
        AutomationTaskStatus.SUCCESS,
        message="Importação concluída: 1 importados, 0 já existentes ignorados, 0 com erro",
        payload=task.payload,
    )


@pytest.mark.asyncio
async def test_execute_leaves_batch_with_validation_errors():
    """
    Test that if validation errors occur in Ahgora, the correct tasks are failed.
    """
    repo = MagicMock()
    job_id = uuid4()
    task_id = uuid4()

    class MockTask:
        def __init__(self, id, type, status, payload):
            self.id = id
            self.type = type
            self.status = status
            self.payload = payload

    task = MockTask(
        id=task_id,
        type="ADD_LEAVE",
        status=AutomationTaskStatus.PENDING,
        payload={"leaves": [{"id": "000001"}, {"id": "000002"}]},
    )

    repo.get_automation_tasks_by_job = AsyncMock(return_value=[task])
    repo.get_ahgora_leaves_df = AsyncMock(
        return_value=pd.DataFrame([{"id": "000001"}, {"id": "000002"}])
    )
    repo.update_task_status = AsyncMock()
    repo.save_ahgora_leaves_batch = AsyncMock()
    repo.evaluate_and_update_job_status = AsyncMock()
    repo.add_log = AsyncMock()

    service = LeaveSyncService(repo=repo)

    service._run_browser_batch_import = MagicMock(
        return_value=[
            {
                "payload": {"id": "000001"},
                "status": "error",
                "message": "Interseccao",
                "index": 0,
            },
            {
                "payload": {"id": "000002"},
                "status": "success",
                "message": "",
                "index": 1,
            },
        ]
    )
    await service.execute_leaves_batch(job_id)

    repo.update_task_status.assert_any_call(
        task_id,
        AutomationTaskStatus.SUCCESS,
        message="Importação concluída: 1 importados, 0 já existentes ignorados, 1 com erro",
        payload=task.payload,
    )


@pytest.mark.asyncio
async def test_execute_leaves_batch_catastrophic_failure():
    """
    Test that all tasks are marked as failed if a catastrophic exception occurs.
    """
    repo = MagicMock()
    job_id = uuid4()
    task_id = uuid4()

    class MockTask:
        def __init__(self, id, type, status, payload):
            self.id = id
            self.type = type
            self.status = status
            self.payload = payload

    task = MockTask(
        id=task_id,
        type="ADD_LEAVE",
        status=AutomationTaskStatus.PENDING,
        payload={"leaves": [{"id": "000001"}]},
    )
    repo.get_automation_tasks_by_job = AsyncMock(return_value=[task])
    repo.get_ahgora_leaves_df = AsyncMock(return_value=pd.DataFrame([{"id": "000001"}]))
    repo.update_task_status = AsyncMock()
    repo.save_ahgora_leaves_batch = AsyncMock()
    repo.evaluate_and_update_job_status = AsyncMock()
    repo.add_log = AsyncMock()

    service = LeaveSyncService(repo=repo)

    service._run_browser_batch_import = MagicMock(
        side_effect=Exception("Browser crashed")
    )
    await service.execute_leaves_batch(job_id)

    repo.update_task_status.assert_any_call(
        task_id, AutomationTaskStatus.FAILED, message="Browser crashed"
    )


def _leave_task(payloads):
    class MockTask:
        def __init__(self):
            self.id = uuid4()
            self.type = "ADD_LEAVE"
            self.status = AutomationTaskStatus.PENDING
            self.payload = {"leaves": payloads}

    return MockTask()


def _leave_repo(task):
    repo = MagicMock()
    repo.get_automation_tasks_by_job = AsyncMock(return_value=[task])
    repo.update_task_status = AsyncMock()
    repo.save_ahgora_leaves_batch = AsyncMock()
    repo.evaluate_and_update_job_status = AsyncMock()
    repo.add_log = AsyncMock()
    return repo


@pytest.mark.asyncio
async def test_leaves_already_in_ahgora_are_saved_so_they_are_not_sent_again():
    new, existing, broken = {"id": "000001"}, {"id": "000002"}, {"id": "000003"}
    task = _leave_task([new, existing, broken])
    repo = _leave_repo(task)
    service = LeaveSyncService(repo=repo)
    service._run_browser_batch_import = MagicMock(
        return_value=[
            {"payload": new, "status": "success", "message": "", "index": 0},
            {
                "payload": existing,
                "status": "success",
                "message": "Intersecção com afastamento existente no registro",
                "index": 1,
            },
            {
                "payload": broken,
                "status": "error",
                "message": "Data inválida",
                "index": 2,
            },
        ]
    )

    await service.execute_leaves_batch(uuid4())

    repo.save_ahgora_leaves_batch.assert_called_once_with([new, existing])
    assert task.payload["leaves"] == [new]


@pytest.mark.asyncio
async def test_real_errors_stay_errors_when_no_row_is_left_to_import():
    """All rows refused: only the ones Ahgora already has count as done."""
    import asyncio

    payloads = [
        {
            "id": "000001",
            "cod": "001",
            "start_date": "01/01/2026",
            "end_date": "02/01/2026",
        },
        {
            "id": "000002",
            "cod": "001",
            "start_date": "01/01/2026",
            "end_date": "02/01/2026",
        },
    ]
    browser = MagicMock()
    browser.extract_import_errors.return_value = [
        {"row": 1, "error": "Intersecção com afastamento existente no registro"},
        {"row": 2, "error": "Código de afastamento inválido"},
    ]
    repo = MagicMock()
    repo.add_log = AsyncMock()
    service = LeaveSyncService(repo=repo)

    with patch("app.services.leave_sync_service.AhgoraBrowser", return_value=browser):
        results = await asyncio.to_thread(
            service._run_browser_batch_import,
            payloads,
            uuid4(),
            uuid4(),
            asyncio.get_running_loop(),
            asyncio.Lock(),
        )

    assert [r["status"] for r in results] == ["success", "error"]
    browser.confirm_import.assert_not_called()
