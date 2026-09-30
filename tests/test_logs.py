import asyncio
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import create_access_token
from app.core.logging import (
    ContextFilter,
    QuietPollingFilter,
    get_log_context,
    log_context,
)
from app.domain.entities import AutomationTask, SyncJob, SyncLog
from app.domain.enums import AutomationTaskType, SyncStatus
from app.infrastructure.db.models import SyncJobModel
from app.infrastructure.db.sqlalchemy_repo import SqlAlchemyRepo
from app.infrastructure.web.routes import _log_view, group_logs_chronologically
from app.main import app
from app.services.sync_service import SyncService


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _log(message, level="INFO", task_id=None, username=None, when=None):
    return SyncLog(
        id=None,
        job_id=uuid4(),
        level=level,
        message=message,
        task_id=task_id,
        username=username,
        timestamp=when or datetime(2026, 9, 30, 10, 0),
    )


# --- job owner (user_id) -------------------------------------------------------


@pytest.mark.asyncio
async def test_save_job_keeps_owner_when_job_was_loaded_without_it():
    user_id = uuid4()
    db_job = SyncJobModel(id=uuid4(), status=SyncStatus.RUNNING, user_id=user_id)
    session = MagicMock()
    session.get = AsyncMock(return_value=db_job)
    session.commit = AsyncMock()

    await SqlAlchemyRepo(session).save_job(
        SyncJob(id=db_job.id, status=SyncStatus.SUCCESS, user_id=None)
    )

    assert db_job.user_id == user_id
    assert db_job.status == SyncStatus.SUCCESS


@pytest.mark.asyncio
async def test_get_job_includes_owner():
    user_id = uuid4()
    db_job = SyncJobModel(
        id=uuid4(),
        status="success",
        triggered_by="api",
        user_id=user_id,
        metadata_info={},
        retry_count=0,
    )
    session = MagicMock()
    session.get = AsyncMock(return_value=db_job)

    job = await SqlAlchemyRepo(session).get_job(db_job.id)

    assert job.user_id == user_id


# --- who wrote each log entry -------------------------------------------------------


@pytest.mark.asyncio
async def test_add_log_records_username_from_context():
    session = MagicMock()
    session.commit = AsyncMock()
    repo = SqlAlchemyRepo(session)

    with log_context(username="maria"):
        await repo.add_log(uuid4(), "INFO", "dentro")
    await repo.add_log(uuid4(), "INFO", "fora")

    inside, outside = [call.args[0] for call in session.add.call_args_list]
    assert inside.username == "maria"
    assert outside.username is None


@pytest.mark.asyncio
async def test_log_context_reaches_browser_thread_callbacks():
    """Browser threads log through run_coroutine_threadsafe; the user must survive the hop."""
    loop = asyncio.get_running_loop()
    seen = []

    async def record():
        seen.append(get_log_context().get("username"))

    def browser_thread():
        asyncio.run_coroutine_threadsafe(record(), loop).result(timeout=5)

    with log_context(username="joao", job_id=uuid4()):
        await asyncio.to_thread(browser_thread)

    assert seen == ["joao"]


@pytest.mark.asyncio
async def test_sync_run_binds_the_job_owner():
    owner_id = uuid4()
    job = SyncJob(id=uuid4(), user_id=owner_id)
    repo = MagicMock()
    repo.get_job = AsyncMock(return_value=job)
    repo.get_usernames = AsyncMock(return_value={owner_id: "ana"})
    repo.update_job_status = AsyncMock()
    repo.evaluate_and_update_job_status = AsyncMock()
    seen = []
    repo.add_log = AsyncMock(
        side_effect=lambda *a, **k: seen.append(get_log_context().get("username"))
    )
    service = SyncService(repo=repo)

    with patch.object(
        service,
        "_execute_sync_logic",
        new_callable=AsyncMock,
        return_value=MagicMock(success=True, message="ok"),
    ):
        await service.run_sync_background(job.id, *["x"] * 7)

    assert seen and set(seen) == {"ana"}


# --- retries never repeat a wrong password ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        "Usuário ou senha Ahgora inválido",
        "Usuário ou senha Fiorilli inválido",
        "Credenciais do Ahgora não configuradas (usuário, senha e empresa)",
    ],
)
async def test_auth_errors_fail_without_retry(error):
    job = SyncJob(id=uuid4(), retry_count=0)
    repo = MagicMock()
    repo.update_job_status = AsyncMock()
    repo.increment_job_retry = AsyncMock()
    repo.add_log = AsyncMock()

    await SyncService(repo=repo)._handle_job_retry(job, error_msg=error)

    repo.increment_job_retry.assert_not_called()
    repo.update_job_status.assert_called_once_with(job.id, SyncStatus.FAILED, error)


@pytest.mark.asyncio
async def test_other_errors_are_retried():
    job = SyncJob(id=uuid4(), retry_count=0)
    repo = MagicMock()
    repo.increment_job_retry = AsyncMock()
    repo.add_log = AsyncMock()

    await SyncService(repo=repo)._handle_job_retry(job, error_msg="Timeout na página")

    repo.increment_job_retry.assert_called_once()


# --- viewer ---------------------------------------------------------------------------


def test_task_describe():
    update = AutomationTask(
        job_id=uuid4(),
        type=AutomationTaskType.UPDATE_EMPLOYEE,
        payload={"id": "123", "name_expected": "FULANO DE TAL"},
    )
    leaves = AutomationTask(
        job_id=uuid4(),
        type=AutomationTaskType.ADD_LEAVE,
        payload={"name": "AFASTAMENTOS", "leaves": [{}, {}]},
    )
    assert update.describe() == "Atualizar funcionário: FULANO DE TAL (123)"
    assert leaves.describe() == "Importar afastamentos (2)"


def test_group_logs_titles_tasks_and_marks_days():
    task = AutomationTask(
        job_id=uuid4(),
        type=AutomationTaskType.REMOVE_EMPLOYEE,
        payload={"id": "7", "name": "CICLANO"},
    )
    day1, day2 = datetime(2026, 9, 29, 23, 59), datetime(2026, 9, 30, 0, 1)
    logs = [
        _log("inicio", when=day1),
        _log("a", task_id=task.id, username="ana", when=day1),
        _log("b", task_id=task.id, username="bia", when=day1),
        _log("c", task_id=task.id, username="ana", when=day1),
        _log("fim", when=day2),
    ]

    groups = group_logs_chronologically(logs, {task.id: task})

    assert [g.get("label") for g in groups if g.get("is_date")] == [
        "29/09/2026",
        "30/09/2026",
    ]
    task_group = next(g for g in groups if g.get("task_id"))
    assert task_group["title"] == "Remover funcionário: CICLANO (7)"
    assert task_group["users"] == ["ana", "bia"]
    assert len(task_group["logs"]) == 3


def test_log_view_level_filter_and_counts():
    logs = [_log("ok"), _log("hmm", "WARNING"), _log("erro", "ERROR")]

    view = _log_view(logs, [], "warning")

    assert view["level_counts"] == {"all": 3, "warning": 2, "error": 1}
    shown = [log.message for g in view["grouped_logs"] for log in g.get("logs", [])]
    assert shown == ["hmm", "erro"]
    assert _log_view(logs, [], "bogus")["level"] == "all"


@patch("app.infrastructure.web.routes.SyncService")
@pytest.mark.parametrize(
    "owner, expected", [("mine", "testuser"), ("all", None), ("bia", "bia")]
)
def test_jobs_partial_owner_filter(mock_service_class, owner, expected, client):
    service = mock_service_class.return_value
    service.list_jobs = AsyncMock(return_value=[])
    service.repo.get_usernames = AsyncMock(return_value={})
    # A real token: "mine" is resolved by the auth middleware, not by require_auth
    token = create_access_token({"sub": "testuser", "is_admin": False})
    client.cookies.set("access_token", token)

    response = client.get(f"/partials/jobs?owner={owner}")

    assert response.status_code == 200
    service.list_jobs.assert_called_once_with(expected)


@pytest.mark.parametrize("path", ["/partials/logs", "/partials/task-log"])
def test_log_partials_require_login(path, client):
    response = client.get(
        f"{path}?job_id={uuid4()}&task_id={uuid4()}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- server log -------------------------------------------------------------------------


def _record(msg="x", args=None):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_context_filter_prefixes_user_and_sync():
    record = _record()
    job_id = uuid4()
    with log_context(username="ana", job_id=job_id):
        ContextFilter().filter(record)
    assert record.context == f"[ana sync={str(job_id)[:8]}] "

    record = _record()
    ContextFilter().filter(record)
    assert record.context == ""


@pytest.mark.parametrize(
    "path, status, shown",
    [
        ("/partials/jobs?owner=mine", 200, False),
        (f"/jobs/{uuid4()}/tasks/summary", 200, False),
        ("/partials/jobs", 500, True),
        ("/api/sync/run", 200, True),
    ],
)
def test_access_log_hides_successful_polling(path, status, shown):
    record = _record(
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1", "GET", path, "1.1", status)
    )
    assert QuietPollingFilter().filter(record) is shown
