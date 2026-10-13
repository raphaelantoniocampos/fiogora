import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from app.domain.entities import SyncJob, SyncResult
from app.domain.enums import SyncStatus
from app.services.sync_service import SyncService


@pytest.mark.asyncio
async def test_create_job():
    repo = MagicMock()
    repo.save_job = AsyncMock()
    service = SyncService(repo=repo)

    job = await service.create_job(triggered_by="test")

    assert job.triggered_by == "test"
    assert job.status == SyncStatus.PENDING
    repo.save_job.assert_called_once()


@pytest.mark.asyncio
async def test_run_sync_background_success():
    repo = MagicMock()
    job_id = uuid4()
    job = SyncJob(id=job_id)
    repo.get_job = AsyncMock(return_value=job)
    repo.update_job_status = AsyncMock()
    repo.evaluate_and_update_job_status = AsyncMock()
    repo.add_log = AsyncMock()

    service = SyncService(repo=repo)

    # Mock _execute_sync_logic as an AsyncMock
    with patch.object(
        service,
        "_execute_sync_logic",
        new_callable=AsyncMock,
        return_value=SyncResult(success=True, status=SyncStatus.SUCCESS, message="OK"),
    ):
        await service.run_sync_background(
            job_id,
            "http://fiorilli",
            "user1",
            "pass1",
            "http://ahgora",
            "user2",
            "company",
            "pass2",
        )

    repo.update_job_status.assert_any_call(job_id, SyncStatus.RUNNING)
    repo.evaluate_and_update_job_status.assert_called_once_with(job_id, "OK")
    assert repo.add_log.call_count >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, message",
    [
        (Exception("Browser Error"), "Browser Error"),
        (
            SyncResult(
                success=False, status=SyncStatus.FAILED, message="Transient Error"
            ),
            "Transient Error",
        ),
    ],
)
async def test_run_sync_background_failure_marks_job_failed(outcome, message):
    """There is no automatic retry: a failed sync ends as failed, with the reason."""
    repo = MagicMock()
    job_id = uuid4()
    job = SyncJob(id=job_id)
    repo.get_job = AsyncMock(return_value=job)
    repo.update_job_status = AsyncMock()
    repo.add_log = AsyncMock()

    service = SyncService(repo=repo)

    side_effect = {"side_effect": outcome} if isinstance(outcome, Exception) else {}
    return_value = {} if side_effect else {"return_value": outcome}
    with patch.object(
        service,
        "_execute_sync_logic",
        new_callable=AsyncMock,
        **side_effect,
        **return_value,
    ):
        await service.run_sync_background(
            job_id,
            "http://fiorilli",
            "user1",
            "pass1",
            "http://ahgora",
            "user2",
            "company",
            "pass2",
        )

    repo.update_job_status.assert_any_call(job_id, SyncStatus.FAILED, message)
    logged = [call.args[2] for call in repo.add_log.call_args_list]
    assert f"Sincronização falhou: {message}" in logged


@patch("app.services.sync_service.Path.exists", return_value=True)
@pytest.mark.asyncio
async def test_validate_ahgora_state(mock_exists):
    import pandas as pd

    repo = MagicMock()
    repo.get_ahgora_employees_df = AsyncMock(
        return_value=pd.DataFrame(
            [
                {"id": "1", "name": "DB Only Employee"},
                {"id": "2", "name": "Common Employee"},
                {"id": "3", "name": "Changed Name DB"},
            ]
        )
    )

    service = SyncService(repo=repo)
    service._log = AsyncMock()

    # Mock _get_changed_employees_df to simulate "Changed Name DB" != "Changed Name CSV"
    service._get_changed_employees_df = AsyncMock(
        return_value=pd.DataFrame([{"id": "3"}])
    )

    csv_df = pd.DataFrame(
        [
            {"id": "2", "name": "Common Employee"},
            {"id": "3", "name": "Changed Name CSV"},
            {"id": "4", "name": "CSV Only Employee"},
        ]
    )
    service._read_csv = MagicMock(return_value=csv_df)

    await service._validate_ahgora_state(uuid4(), csv_df)

    log_calls = [call.args[2] for call in service._log.call_args_list]
    log_text = " ".join(log_calls)

    # Check that it warns about missing in DB (ID 4)
    assert "1 funcionários do CSV do Ahgora não estão no banco" in log_text

    # Check that it warns about missing in CSV (ID 1)
    assert "1 funcionários do banco não estão no CSV do Ahgora" in log_text

    # Check that it finds discrepancies for common
    assert "1 funcionários com dados diferentes" in log_text
