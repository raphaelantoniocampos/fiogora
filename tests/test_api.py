from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.endpoints import get_execution_service, get_service
from app.core.database import get_db
from app.domain.entities import AutomationTask, SyncJob, SyncLog
from app.domain.enums import AutomationTaskStatus, AutomationTaskType
from app.main import app

# Mock database dependency
mock_db = MagicMock()


@pytest.fixture
def client():
    # Override get_db dependency
    app.dependency_overrides[get_db] = lambda: mock_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@patch("app.api.endpoints.transport_crypto.decrypt")
@patch("app.api.endpoints.SyncService")
def test_run_sync_job(mock_service_class, mock_decrypt, client):
    mock_decrypt.side_effect = lambda x: x
    mock_service = mock_service_class.return_value
    job_id = uuid4()
    mock_service.create_job = AsyncMock(
        return_value=SyncJob(id=job_id, triggered_by="api")
    )
    # mock background task isn't strictly necessary for the service mock,
    # but the endpoint triggers BackgroundTasks.add_task

    # Override get_service dependency specially to inject the mock instance
    app.dependency_overrides[get_service] = lambda: mock_service

    response = client.post(
        "/api/sync/run", json={"fiorilli_password": "enc", "ahgora_password": "enc"}
    )
    assert response.status_code == 200
    assert response.json()["id"] == str(job_id)
    mock_service.create_job.assert_called_once()

    app.dependency_overrides.pop(get_service, None)


@patch("app.api.endpoints.SyncService")
def test_list_jobs(mock_service_class, client):
    mock_service = mock_service_class.return_value
    job_id = uuid4()
    mock_service.list_jobs = AsyncMock(
        return_value=[SyncJob(id=job_id, triggered_by="api")]
    )
    app.dependency_overrides[get_service] = lambda: mock_service

    response = client.get("/api/sync/jobs")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == str(job_id)

    app.dependency_overrides.pop(get_service, None)


@patch("app.api.endpoints.SyncService")
def test_kill_job_success(mock_service_class, client):
    mock_service = mock_service_class.return_value
    job_id = uuid4()
    mock_service.kill_job = AsyncMock(return_value=True)
    app.dependency_overrides[get_service] = lambda: mock_service

    response = client.post(f"/api/sync/jobs/{job_id}/kill")
    assert response.status_code == 200
    assert "Kill signal sent" in response.json()["message"]

    app.dependency_overrides.pop(get_service, None)


@patch("app.api.endpoints.SyncService")
def test_kill_job_not_found(mock_service_class, client):
    mock_service = mock_service_class.return_value
    job_id = uuid4()
    mock_service.kill_job = AsyncMock(return_value=False)
    app.dependency_overrides[get_service] = lambda: mock_service

    response = client.post(f"/api/sync/jobs/{job_id}/kill")
    assert response.status_code == 404

    app.dependency_overrides.pop(get_service, None)


@patch("app.api.endpoints.SyncService")
def test_get_job_logs(mock_service_class, client):
    mock_service = mock_service_class.return_value
    job_id = uuid4()
    logs = [SyncLog(id=1, job_id=job_id, level="INFO", message="Test Log")]
    mock_service.get_job_logs = AsyncMock(return_value=logs)
    app.dependency_overrides[get_service] = lambda: mock_service

    response = client.get(f"/api/sync/jobs/{job_id}/logs")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["message"] == "Test Log"

    app.dependency_overrides.pop(get_service, None)


@patch("app.api.endpoints.SyncService")
def test_get_automation_tasks(mock_service_class, client):
    mock_service = mock_service_class.return_value
    job_id = uuid4()
    task = AutomationTask(
        id=uuid4(),
        job_id=job_id,
        type=AutomationTaskType.ADD_EMPLOYEE,
        status=AutomationTaskStatus.PENDING,
        payload={},
    )
    mock_service.get_automation_tasks = AsyncMock(return_value=[task])
    app.dependency_overrides[get_service] = lambda: mock_service

    response = client.get(f"/api/sync/jobs/{job_id}/tasks")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["type"] == AutomationTaskType.ADD_EMPLOYEE.value

    app.dependency_overrides.pop(get_service, None)


@patch("app.api.endpoints.SyncService")
def test_list_automation_tasks(mock_service_class, client):
    mock_service = mock_service_class.return_value
    task = AutomationTask(
        id=uuid4(),
        job_id=uuid4(),
        type=AutomationTaskType.ADD_EMPLOYEE,
        status=AutomationTaskStatus.PENDING,
        payload={},
    )
    mock_service.list_automation_tasks = AsyncMock(return_value=[task])
    app.dependency_overrides[get_service] = lambda: mock_service

    response = client.get("/api/sync/tasks")
    assert response.status_code == 200
    assert len(response.json()) == 1

    app.dependency_overrides.pop(get_service, None)


@patch("app.api.endpoints.task_registry")
@patch("asyncio.Task")
def test_get_registry_diagnostics(mock_task, mock_registry, client):
    mock_registry.get_all_tasks.return_value = {uuid4(): mock_task}

    response = client.get("/api/sync/diagnostics/registry")
    assert response.status_code == 200
    assert response.json()["active_task_count"] == 1


@patch("app.api.endpoints.TaskExecutionService")
def test_execute_task(mock_exec_service_class, client):
    mock_exec_service = mock_exec_service_class.return_value
    mock_exec_service.execute_task = AsyncMock(return_value=True)
    task_id = uuid4()

    app.dependency_overrides[get_execution_service] = lambda: mock_exec_service

    response = client.post(f"/api/sync/tasks/{task_id}/execute")
    assert response.status_code == 200
    assert "triggered" in response.json()["message"]

    # Ensure it's passed off to background tasks; since we mock the service, the background
    # task will just try to call mock_exec_service.execute_task(task_id).

    app.dependency_overrides.pop(get_execution_service, None)


@patch("app.api.endpoints.TaskExecutionService")
def test_execute_batch_tasks(mock_exec_service_class, client):
    mock_exec_service = mock_exec_service_class.return_value
    mock_exec_service.execute_batch = AsyncMock(return_value=None)
    job_id = uuid4()
    task_type = "ADD_EMPLOYEE"

    app.dependency_overrides[get_execution_service] = lambda: mock_exec_service

    response = client.post(
        "/api/sync/tasks/batch/execute",
        params={"job_id": str(job_id), "task_type": task_type},
    )
    assert response.status_code == 200, response.json()
    assert task_type in response.json()["message"]

    app.dependency_overrides.pop(get_execution_service, None)


@patch("app.api.endpoints.TaskExecutionService")
def test_execute_all_job_tasks(mock_exec_service_class, client):
    mock_exec_service = mock_exec_service_class.return_value
    mock_exec_service.execute_all_tasks = AsyncMock(return_value=None)
    job_id = uuid4()

    app.dependency_overrides[get_execution_service] = lambda: mock_exec_service

    response = client.post(f"/api/sync/jobs/{job_id}/execute-all-tasks")
    assert response.status_code == 200, response.json()
    assert "triggered" in response.json()["message"]

    app.dependency_overrides.pop(get_execution_service, None)


@patch("app.api.endpoints.TaskExecutionService")
def test_cancel_task_success(mock_exec_service_class, client):
    mock_exec_service = mock_exec_service_class.return_value
    task_id = uuid4()
    mock_exec_service.cancel_task = AsyncMock(return_value=True)

    app.dependency_overrides[get_execution_service] = lambda: mock_exec_service

    response = client.post(f"/api/sync/tasks/{task_id}/cancel")
    assert response.status_code == 200
    assert "sucesso" in response.json()["message"]

    app.dependency_overrides.pop(get_execution_service, None)


@patch("app.api.endpoints.TaskExecutionService")
def test_cancel_task_failure(mock_exec_service_class, client):
    mock_exec_service = mock_exec_service_class.return_value
    task_id = uuid4()
    mock_exec_service.cancel_task = AsyncMock(return_value=False)

    app.dependency_overrides[get_execution_service] = lambda: mock_exec_service

    response = client.post(f"/api/sync/tasks/{task_id}/cancel")
    assert response.status_code == 400

    app.dependency_overrides.pop(get_execution_service, None)


@patch("app.api.endpoints.TaskExecutionService")
def test_cancel_batch_tasks(mock_exec_service_class, client):
    mock_exec_service = mock_exec_service_class.return_value
    job_id = uuid4()
    task_type = "ADD_EMPLOYEE"
    mock_exec_service.cancel_batch = AsyncMock()

    app.dependency_overrides[get_execution_service] = lambda: mock_exec_service

    response = client.post(
        "/api/sync/tasks/batch/cancel",
        params={"job_id": str(job_id), "task_type": task_type},
    )
    assert response.status_code == 200, response.json()
    assert task_type in response.json()["message"]
    mock_exec_service.cancel_batch.assert_called_once_with(job_id, task_type)

    app.dependency_overrides.pop(get_execution_service, None)


@patch("app.infrastructure.web.routes.SyncService")
def test_get_task_details_partial_single_task(mock_service_class, client):
    mock_service = mock_service_class.return_value
    task_id = uuid4()

    class MockTask:
        id = task_id
        status = "SUCCESS"
        error_message = None
        payload = {"key": "value"}

    mock_service.repo.get_task = AsyncMock(return_value=MockTask())

    # Import the dependency override for main.py where get_service is defined and used
    from app.api.endpoints import get_service as api_get_service

    app.dependency_overrides[api_get_service] = lambda: mock_service

    response = client.get(f"/partials/task-payload?task_id={task_id}")
    assert response.status_code == 200
    assert "value" in response.text
    assert "SUCCESS" in response.text

    app.dependency_overrides.pop(api_get_service, None)


@patch("app.infrastructure.web.routes.SyncService")
def test_get_task_details_partial_group_tasks(mock_service_class, client):
    mock_service = mock_service_class.return_value
    job_id = uuid4()
    task_type = AutomationTaskType.ADD_EMPLOYEE

    class MockGroupTask:
        id = uuid4()
        type = task_type.name
        status = AutomationTaskStatus.FAILED
        error_message = None
        payload = {"name": "123"}

    mock_service.get_automation_tasks = AsyncMock(return_value=[MockGroupTask()])

    from app.api.endpoints import get_service as api_get_service

    app.dependency_overrides[api_get_service] = lambda: mock_service

    response = client.get(
        f"/partials/task-details-inline?job_id={job_id}&task_type={task_type.name}"
    )  # Use .name not .value for FastAPI query match
    assert response.status_code == 200
    assert "123" in response.text
    assert "failed" in response.text.lower()

    app.dependency_overrides.pop(api_get_service, None)


@patch("app.infrastructure.web.routes.SyncService")
def test_get_task_log_partial(mock_service_class, client):
    mock_service = mock_service_class.return_value
    task_id = uuid4()
    task = AutomationTask(
        id=task_id,
        job_id=uuid4(),
        type=AutomationTaskType.ADD_EMPLOYEE,
        status=AutomationTaskStatus.PENDING,
        payload={},
    )
    log_entry = SyncLog(
        id=1,
        job_id=task.job_id,
        task_id=task_id,
        level="INFO",
        message="Executed step 1",
    )

    mock_service.repo.get_task = AsyncMock(return_value=task)
    mock_service.repo.get_task_logs = AsyncMock(return_value=[log_entry])

    from app.api.endpoints import get_service as api_get_service

    app.dependency_overrides[api_get_service] = lambda: mock_service

    response = client.get(f"/partials/task-log?task_id={task_id}")
    assert response.status_code == 200
    assert "Executed step 1" in response.text

    app.dependency_overrides.pop(api_get_service, None)
