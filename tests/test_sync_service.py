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
async def test_run_sync_background_failure_permanent():
    repo = MagicMock()
    job_id = uuid4()
    # Set retry_count to 3 to force final failure
    job = SyncJob(id=job_id, retry_count=3)
    repo.get_job = AsyncMock(return_value=job)
    repo.update_job_status = AsyncMock()
    repo.add_log = AsyncMock()

    service = SyncService(repo=repo)

    with patch.object(
        service,
        "_execute_sync_logic",
        new_callable=AsyncMock,
        side_effect=Exception("Browser Error"),
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

    repo.update_job_status.assert_any_call(job_id, SyncStatus.FAILED, "Browser Error")


@pytest.mark.asyncio
async def test_run_sync_background_retry_scheduled():
    repo = MagicMock()
    job_id = uuid4()
    # Fresh job, retry_count=0
    job = SyncJob(id=job_id, retry_count=0)
    repo.get_job = AsyncMock(return_value=job)
    repo.update_job_status = AsyncMock()
    repo.increment_job_retry = AsyncMock()
    repo.add_log = AsyncMock()

    service = SyncService(repo=repo)

    with patch.object(
        service,
        "_execute_sync_logic",
        new_callable=AsyncMock,
        return_value=SyncResult(
            success=False, status=SyncStatus.FAILED, message="Transient Error"
        ),
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

    # Should have called increment_job_retry, not update_job_status(FAILED)
    repo.increment_job_retry.assert_called_once()
    # Check that failed status was NOT finalized (only internal result status was FAILED)
    # The actual DB update for FAILED is only called if success=True/False in run_sync_background
    # wait, if result.success is False, it calls _handle_job_retry

    status_calls = [call.args[1] for call in repo.update_job_status.call_args_list]
    assert SyncStatus.FAILED not in status_calls
    assert SyncStatus.RUNNING in status_calls


@pytest.mark.asyncio
async def test_run_sync_background_credentials_not_set_fails_permanently():
    repo = MagicMock()
    job_id = uuid4()
    # Fresh job, retry_count=0
    job = SyncJob(id=job_id, retry_count=0)
    repo.get_job = AsyncMock(return_value=job)
    repo.update_job_status = AsyncMock()
    repo.increment_job_retry = AsyncMock()
    repo.add_log = AsyncMock()

    service = SyncService(repo=repo)

    with patch.object(
        service,
        "_execute_sync_logic",
        new_callable=AsyncMock,
        return_value=SyncResult(
            success=False,
            status=SyncStatus.FAILED,
            message="Ahgora credentials not set (AHGORA_USER, password from frontend, AHGORA_COMPANY)",
        ),
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

    # Should have called update_job_status with FAILED, not increment_job_retry
    repo.increment_job_retry.assert_not_called()
    repo.update_job_status.assert_any_call(
        job_id,
        SyncStatus.FAILED,
        "Ahgora credentials not set (AHGORA_USER, password from frontend, AHGORA_COMPANY)",
    )


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
    assert "1 employees in Ahgora CSV not present in DB" in log_text

    # Check that it warns about missing in CSV (ID 1)
    assert "1 employees in DB not present in Ahgora CSV" in log_text

    # Check that it finds discrepancies for common
    assert "Found 1 employees with data discrepancies" in log_text
