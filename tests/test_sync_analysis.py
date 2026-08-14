import pytest
import pandas as pd
import numpy as np
from uuid import uuid4
from unittest.mock import MagicMock, AsyncMock

from app.services.sync_service import SyncService
from app.domain.entities import AutomationTask, AutomationTaskType


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.save_automation_tasks_batch = AsyncMock()
    repo.save_ahgora_employees_batch = AsyncMock()
    repo.add_log = AsyncMock()
    repo.update_job_status = AsyncMock()
    repo.get_job = AsyncMock(return_value=None)
    repo.save_job = AsyncMock()
    return repo


@pytest.fixture
def sync_service(mock_repo):
    return SyncService(repo=mock_repo)


def test_normalize_text(sync_service):
    assert sync_service._normalize_text("  Teste  Acentuação  ") == "teste acentuacao"
    assert sync_service._normalize_text("VIGILANCIA EM SAUDE") == "vigilancia em saude"
    assert pd.isna(sync_service._normalize_text(np.nan))


def test_convert_date(sync_service):
    # Test valid dates
    dt = sync_service._convert_date("19/02/2024")
    assert dt.strftime("%d/%m/%Y") == "19/02/2024"

    dt = sync_service._convert_date("Seg, 19/Fev/2024")
    assert dt.strftime("%d/%m/%Y") == "19/02/2024"

    # Test invalid/empty
    assert pd.isna(sync_service._convert_date(""))
    assert pd.isna(sync_service._convert_date(None))


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_create_automation_tasks(sync_service, mock_repo):
    job_id = uuid4()
    new_employees = pd.DataFrame(
        [
            {
                "id": "123456",
                "name": "TEST USER",
                "admission_date": "01/01/2024",
                "binding": "CLT",
            }
        ]
    )

    await sync_service._create_automation_tasks(
        job_id,
        new_employees_df=new_employees,
        seed_employees_df=pd.DataFrame(),
        dismissed_employees_df=pd.DataFrame(),
        changed_employees_df=pd.DataFrame(),
        new_leaves_df=pd.DataFrame(),
    )

    assert mock_repo.save_automation_tasks_batch.call_count == 1
    tasks_passed = mock_repo.save_automation_tasks_batch.call_args[0][0]
    assert len(tasks_passed) == 1
    task = tasks_passed[0]
    assert isinstance(task, AutomationTask)
    assert task.type == AutomationTaskType.ADD_EMPLOYEE
    assert task.payload["id"] == "123456"


@pytest.mark.asyncio
async def test_generate_tasks_dfs_new_employee(sync_service):
    fiorilli_df = pd.DataFrame(
        [{"id": "000001", "name": "NEW USER", "dismissal_date": None, "binding": "CLT"}]
    )
    ahgora_df = pd.DataFrame(columns=["id", "name", "dismissal_date"])
    ahgora_csv_df = pd.DataFrame(columns=["id", "name", "dismissal_date"])

    (
        new_emp,
        seed_emp,
        dismissed,
        changed,
        leaves,
    ) = await sync_service._generate_tasks_dfs(
        fiorilli_df, ahgora_df, ahgora_csv_df, pd.DataFrame(), pd.DataFrame()
    )

    assert len(new_emp) == 1
    assert new_emp.iloc[0]["id"] == "000001"
    assert seed_emp.empty
    assert dismissed.empty
    assert changed.empty


@pytest.mark.asyncio
async def test_generate_tasks_dfs_dismissed(sync_service):
    fiorilli_df = pd.DataFrame(
        [
            {
                "id": "000001",
                "name": "USER",
                "dismissal_date": "01/01/2024",
                "binding": "CLT",
            }
        ]
    )
    ahgora_df = pd.DataFrame([{"id": "000001", "name": "USER", "dismissal_date": None}])

    (
        new_emp,
        seed_emp,
        dismissed,
        changed,
        leaves,
    ) = await sync_service._generate_tasks_dfs(
        fiorilli_df, ahgora_df, pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    )

    assert new_emp.empty
    assert seed_emp.empty
    assert len(dismissed) == 1
    assert dismissed.iloc[0]["id"] == "000001"
