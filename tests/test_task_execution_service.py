from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.domain.entities import AutomationTask
from app.domain.enums import AutomationTaskStatus as Status
from app.domain.enums import AutomationTaskType as Type
from app.services.leave_sync_service import LeaveSyncService
from app.services.task_execution_service import TaskExecutionService

JOB_ID = uuid4()


def _task(status, type_=Type.UPDATE_EMPLOYEE):
    return AutomationTask(job_id=JOB_ID, type=type_, status=status, payload={})


def _repo(tasks):
    repo = MagicMock()
    repo.get_automation_tasks_by_job = AsyncMock(return_value=tasks)
    repo.get_task = AsyncMock(
        side_effect=lambda task_id: {t.id: t for t in tasks}.get(task_id)
    )
    for method in (
        "update_job_status",
        "update_task_status",
        "evaluate_and_update_job_status",
        "add_log",
    ):
        setattr(repo, method, AsyncMock())
    return repo


def _executed(service):
    return [call.args[0] for call in service.execute_task.call_args_list]


@pytest.fixture
def tasks():
    return {status: _task(status) for status in Status}


@pytest.mark.asyncio
async def test_group_batch_skips_cancelled_tasks(tasks):
    service = TaskExecutionService(_repo(list(tasks.values())))
    service.execute_task = AsyncMock()

    await service.execute_batch(JOB_ID, "UPDATE_EMPLOYEE")

    assert _executed(service) == [tasks[Status.PENDING].id, tasks[Status.FAILED].id]


@pytest.mark.asyncio
async def test_execute_all_skips_cancelled_tasks(tasks):
    service = TaskExecutionService(_repo(list(tasks.values())))
    service.execute_task = AsyncMock()

    await service.execute_all_tasks(JOB_ID)

    assert _executed(service) == [tasks[Status.PENDING].id, tasks[Status.FAILED].id]


@pytest.mark.asyncio
async def test_selected_tasks_run_even_if_cancelled(tasks):
    repo = _repo(list(tasks.values()))
    service = TaskExecutionService(repo)
    service.execute_task = AsyncMock()

    await service.execute_selected([t.id for t in tasks.values()] + [uuid4()])

    # Explicit choice: everything but what is done or already running (and unknown ids)
    assert _executed(service) == [
        tasks[Status.PENDING].id,
        tasks[Status.FAILED].id,
        tasks[Status.CANCELLED].id,
    ]
    repo.update_job_status.assert_awaited_once()
    repo.evaluate_and_update_job_status.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
async def test_cancel_selected_counts_what_was_cancelled(tasks):
    service = TaskExecutionService(_repo(list(tasks.values())))

    cancelled = await service.cancel_selected([t.id for t in tasks.values()])

    # pending, running and failed can be cancelled; success and cancelled cannot
    assert cancelled == 3


@pytest.mark.asyncio
async def test_leave_batch_from_the_group_does_not_ask_for_cancelled():
    service = TaskExecutionService(_repo([]))

    with patch("app.services.leave_sync_service.LeaveSyncService") as leave_service:
        leave_service.return_value.execute_leaves_batch = AsyncMock()
        await service.execute_batch(JOB_ID, "ADD_LEAVE")

    kwargs = leave_service.return_value.execute_leaves_batch.call_args.kwargs
    assert not kwargs.get("include_cancelled")


@pytest.mark.asyncio
async def test_running_a_leave_task_itself_includes_cancelled():
    leave = _task(Status.CANCELLED, Type.ADD_LEAVE)
    service = TaskExecutionService(_repo([leave]))

    with patch("app.services.leave_sync_service.LeaveSyncService") as leave_service:
        leave_service.return_value.execute_leaves_batch = AsyncMock()
        await service.execute_task(leave.id)

    kwargs = leave_service.return_value.execute_leaves_batch.call_args.kwargs
    assert kwargs["include_cancelled"] is True
    assert kwargs["task_id"] == leave.id


@pytest.mark.asyncio
@pytest.mark.parametrize("include_cancelled, runs", [(False, False), (True, True)])
async def test_cancelled_leave_import_only_runs_when_asked(include_cancelled, runs):
    leave = _task(Status.CANCELLED, Type.ADD_LEAVE)
    leave.payload = {"leaves": [{"id": "000001"}]}
    repo = _repo([leave])
    repo.save_ahgora_leaves_batch = AsyncMock()
    service = LeaveSyncService(repo)
    service._run_browser_batch_import = MagicMock(return_value=[])

    await service.execute_leaves_batch(JOB_ID, include_cancelled=include_cancelled)

    assert service._run_browser_batch_import.called is runs
