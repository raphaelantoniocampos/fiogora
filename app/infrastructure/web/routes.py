from datetime import timedelta
from typing import Any, Dict, Optional
from uuid import UUID

import dotenv
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    create_access_token,
    decode_access_token,
    get_password_hash,
    verify_password,
)
from app.core.settings import settings
from app.domain.enums import SyncStatus
from app.infrastructure.db.sqlalchemy_repo import SqlAlchemyRepo
from app.services.credential_crypto import decrypt_password, encrypt_password
from app.services.sync_service import SyncService

router = APIRouter()
templates = Jinja2Templates(directory="app/infrastructure/web/templates")


def require_auth(request: Request):
    token = request.cookies.get("access_token")
    if not token or not decode_access_token(token):
        if request.headers.get("HX-Request"):
            raise HTTPException(
                status_code=status.HTTP_200_OK, headers={"HX-Redirect": "/login"}
            )
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )


def require_admin(request: Request):
    require_auth(request)
    if not request.state.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso negado. Apenas administradores podem acessar esta página.",
        )


def get_service(db: AsyncSession = Depends(get_service_db := get_db)):
    repo = SqlAlchemyRepo(db)
    return SyncService(repo=repo)


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
async def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    repo = SqlAlchemyRepo(db)
    user = await repo.get_user_by_username(username)

    is_valid = False
    is_admin = False
    if username == settings.ADMIN_USERNAME and password == settings.ADMIN_PASSWORD:
        is_valid = True
        is_admin = True
    elif user and verify_password(password, user.hashed_password):
        is_valid = True
        is_admin = user.is_admin

    if not is_valid:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Credenciais inválidas."}
        )

    # Generate token
    token = create_access_token(
        {"sub": username, "is_admin": is_admin},
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    expires = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=expires,
        samesite="lax",
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token")
    return response


@router.post("/create-user", dependencies=[Depends(require_admin)])
async def create_user_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: bool = Form(False),
    db: AsyncSession = Depends(get_db),
):
    repo = SqlAlchemyRepo(db)
    existing = await repo.get_user_by_username(username)

    context = {
        "request": request,
        "fiorilli_url": settings.FIORILLI_URL,
        "ahgora_url": settings.AHGORA_URL,
        "username": request.state.username,
        "exceptions_typos": settings.EXCEPTIONS_AND_TYPOS,
        "ignore_ids": settings.IGNORE_LOCATION_CHANGE_IDS,
    }

    if existing:
        context["create_user_error"] = "Usuário já existe."
        return templates.TemplateResponse("config.html", context)

    await repo.create_user(username, get_password_hash(password), is_admin=is_admin)
    context["create_user_success"] = "Usuário criado com sucesso!"
    return templates.TemplateResponse("config.html", context)


@router.get("/change-password", dependencies=[Depends(require_auth)])
async def change_password_page(request: Request):
    return templates.TemplateResponse(
        "change_password.html",
        {"request": request, "username": request.state.username},
    )


@router.post("/change-password", dependencies=[Depends(require_auth)])
async def change_password_post(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    username = request.state.username
    if username == settings.ADMIN_USERNAME:
        return templates.TemplateResponse(
            "change_password.html",
            {
                "request": request,
                "username": username,
                "error": "A senha do usuário administrador padrão só pode ser alterada modificando o arquivo .env",
            },
        )

    repo = SqlAlchemyRepo(db)
    user = await repo.get_user_by_username(username)
    if not user or not verify_password(current_password, user.hashed_password):
        return templates.TemplateResponse(
            "change_password.html",
            {
                "request": request,
                "username": username,
                "error": "A senha atual está incorreta.",
            },
        )

    await repo.update_user_password(username, get_password_hash(new_password))
    return templates.TemplateResponse(
        "change_password.html",
        {
            "request": request,
            "username": username,
            "success": "Sua senha foi alterada com sucesso!",
        },
    )


@router.get("/", dependencies=[Depends(require_auth)])
async def dashboard(request: Request, service: SyncService = Depends(get_service)):
    jobs = await service.list_jobs()
    jobs[0] if jobs else None

    employees_df = await service.repo.get_ahgora_employees_df()
    leaves_df = await service.repo.get_ahgora_leaves_df()

    active_employees = 0
    if not employees_df.empty:
        active_employees = int(employees_df["dismissal_date"].isna().sum())

    total_leaves = len(leaves_df)

    last_success = next((j for j in jobs if j.status == SyncStatus.SUCCESS), None)
    last_sync_date = "Nenhuma"
    if last_success and last_success.finished_at:
        last_sync_date = last_success.finished_at.strftime("%d/%m/%Y %H:%M")

    stats = {
        "active_employees": active_employees,
        "total_leaves": total_leaves,
        "last_sync_date": last_sync_date,
    }

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "headless_mode": settings.HEADLESS_MODE,
            "use_cached_files": settings.USE_CACHED_FILES,
            "is_docker": settings.IS_DOCKER,
        },
    )


@router.get("/config", dependencies=[Depends(require_auth)])
async def config_page(request: Request, db: AsyncSession = Depends(get_db)):
    repo = SqlAlchemyRepo(db)
    user = await repo.get_user_by_username(request.state.username)
    fiorilli_user = ""
    ahgora_user = ""
    ahgora_company = ""
    if user:
        creds = await repo.get_user_credentials(user.id)
        if creds:
            fiorilli_user = creds.get("fiorilli_user", "")
            ahgora_user = creds.get("ahgora_user", "")
            ahgora_company = creds.get("ahgora_company", "")

    return templates.TemplateResponse(
        "config.html",
        {
            "request": request,
            "fiorilli_url": settings.FIORILLI_URL,
            "fiorilli_user": fiorilli_user,
            "ahgora_url": settings.AHGORA_URL,
            "ahgora_user": ahgora_user,
            "ahgora_company": ahgora_company,
            "username": request.state.username,
            "exceptions_typos": settings.EXCEPTIONS_AND_TYPOS,
            "ignore_ids": settings.IGNORE_LOCATION_CHANGE_IDS,
            "use_cached_files": settings.USE_CACHED_FILES,
            "update_locations": settings.UPDATE_LOCATIONS,
            "headless_mode": settings.HEADLESS_MODE,
            "headless_mode_tasks": settings.HEADLESS_MODE_TASKS,
            "is_docker": settings.IS_DOCKER,
        },
    )


@router.post("/api/config/exceptions/typo", dependencies=[Depends(require_admin)])
async def add_typo(
    request: Request, mistake: str = Form(...), correction: str = Form(...)
):
    settings.EXCEPTIONS_AND_TYPOS[mistake.strip()] = correction.strip()
    settings.save_exceptions()
    return templates.TemplateResponse(
        "partials/typos_list.html",
        {"request": request, "exceptions_typos": settings.EXCEPTIONS_AND_TYPOS},
    )


@router.delete(
    "/api/config/exceptions/typo/{mistake}", dependencies=[Depends(require_admin)]
)
async def delete_typo(request: Request, mistake: str):
    if mistake in settings.EXCEPTIONS_AND_TYPOS:
        del settings.EXCEPTIONS_AND_TYPOS[mistake]
        settings.save_exceptions()
    return templates.TemplateResponse(
        "partials/typos_list.html",
        {"request": request, "exceptions_typos": settings.EXCEPTIONS_AND_TYPOS},
    )


@router.post("/api/config/exceptions/ignore-id", dependencies=[Depends(require_admin)])
async def add_ignore_id(request: Request, ignore_id: str = Form(...)):
    val = ignore_id.strip()
    if val and val not in settings.IGNORE_LOCATION_CHANGE_IDS:
        settings.IGNORE_LOCATION_CHANGE_IDS.append(val)
        settings.save_exceptions()
    return templates.TemplateResponse(
        "partials/ignore_ids_list.html",
        {"request": request, "ignore_ids": settings.IGNORE_LOCATION_CHANGE_IDS},
    )


@router.delete(
    "/api/config/exceptions/ignore-id/{ignore_id}",
    dependencies=[Depends(require_admin)],
)
async def delete_ignore_id(request: Request, ignore_id: str):
    if ignore_id in settings.IGNORE_LOCATION_CHANGE_IDS:
        settings.IGNORE_LOCATION_CHANGE_IDS.remove(ignore_id)
        settings.save_exceptions()
    return templates.TemplateResponse(
        "partials/ignore_ids_list.html",
        {"request": request, "ignore_ids": settings.IGNORE_LOCATION_CHANGE_IDS},
    )


@router.post("/api/settings/toggle-headless", dependencies=[Depends(require_auth)])
async def toggle_headless(request: Request, target: str = Form(...)):
    if settings.IS_DOCKER:
        return {"error": "Ação não permitida em produção"}

    env_path = str(settings.BASE_DIR / ".env")

    if target == "sync":
        settings.HEADLESS_MODE = not settings.HEADLESS_MODE
        dotenv.set_key(env_path, "HEADLESS_MODE", str(settings.HEADLESS_MODE))
    elif target == "tasks":
        settings.HEADLESS_MODE_TASKS = not settings.HEADLESS_MODE_TASKS
        dotenv.set_key(
            env_path, "HEADLESS_MODE_TASKS", str(settings.HEADLESS_MODE_TASKS)
        )

    response = templates.TemplateResponse(
        f"partials/headless_{target}_toggle.html",
        {
            "request": request,
            "headless_mode": settings.HEADLESS_MODE,
            "headless_mode_tasks": settings.HEADLESS_MODE_TASKS,
            "use_cached_files": settings.USE_CACHED_FILES,
            "is_docker": settings.IS_DOCKER,
        },
    )
    response.headers["HX-Trigger"] = "refresh"
    return response


@router.post("/api/settings/toggle-cached", dependencies=[Depends(require_auth)])
async def toggle_cached_files(request: Request):
    env_path = str(settings.BASE_DIR / ".env")
    settings.USE_CACHED_FILES = not settings.USE_CACHED_FILES
    dotenv.set_key(env_path, "USE_CACHED_FILES", str(settings.USE_CACHED_FILES))

    response = templates.TemplateResponse(
        "partials/use_cached_files_toggle.html",
        {
            "request": request,
            "use_cached_files": settings.USE_CACHED_FILES,
            "is_docker": settings.IS_DOCKER,
        },
    )
    response.headers["HX-Trigger"] = "refresh"
    return response


@router.post("/api/settings/toggle-locations", dependencies=[Depends(require_auth)])
async def toggle_location_updates(request: Request):
    env_path = str(settings.BASE_DIR / ".env")
    settings.UPDATE_LOCATIONS = not settings.UPDATE_LOCATIONS
    dotenv.set_key(env_path, "UPDATE_LOCATIONS", str(settings.UPDATE_LOCATIONS))

    response = templates.TemplateResponse(
        "partials/location_updates_toggle.html",
        {
            "request": request,
            "update_locations": settings.UPDATE_LOCATIONS,
        },
    )
    response.headers["HX-Trigger"] = "refresh"
    return response


@router.get("/partials/jobs", dependencies=[Depends(require_auth)])
async def get_jobs_partial(
    request: Request, service: SyncService = Depends(get_service)
):
    jobs = await service.list_jobs()
    return templates.TemplateResponse(
        "jobs_partial.html", {"request": request, "jobs": jobs}
    )


@router.get("/jobs/{job_id}/tasks", dependencies=[Depends(require_auth)])
async def get_task_groups_page(
    request: Request, job_id: UUID, service: SyncService = Depends(get_service)
):
    from collections import defaultdict

    from app.domain.enums import AutomationTaskStatus

    tasks = await service.get_automation_tasks(job_id)

    def _default_group() -> Dict[str, Any]:
        return {
            "type": "",
            "total": 0,
            "pending": 0,
            "running": 0,
            "success": 0,
            "failed": 0,
            "cancelled": 0,
        }

    groups: Dict[Any, Dict[str, Any]] = defaultdict(_default_group)

    for t in tasks:
        group = groups[t.type]
        group["type"] = t.type
        group["total"] += 1

        if t.status == AutomationTaskStatus.PENDING:
            group["pending"] += 1
        elif t.status == AutomationTaskStatus.RUNNING:
            group["running"] += 1
        elif t.status == AutomationTaskStatus.SUCCESS:
            group["success"] += 1
        elif t.status == AutomationTaskStatus.FAILED:
            group["failed"] += 1
        elif t.status == AutomationTaskStatus.CANCELLED:
            group["cancelled"] += 1

    return templates.TemplateResponse(
        "task_groups_page.html",
        {
            "request": request,
            "task_groups": list(groups.values()),
            "job_id": str(job_id),
            "headless_mode_tasks": settings.HEADLESS_MODE_TASKS,
            "is_docker": settings.IS_DOCKER,
        },
    )


@router.get("/jobs/{job_id}/tasks/summary", dependencies=[Depends(require_auth)])
async def get_task_groups_summary(
    job_id: UUID, service: SyncService = Depends(get_service)
):
    from collections import defaultdict

    from app.domain.enums import AutomationTaskStatus

    tasks = await service.get_automation_tasks(job_id)

    def _default_group() -> Dict[str, Any]:
        return {
            "type": "",
            "total": 0,
            "pending": 0,
            "running": 0,
            "success": 0,
            "failed": 0,
            "cancelled": 0,
        }

    groups: Dict[Any, Dict[str, Any]] = defaultdict(_default_group)

    for t in tasks:
        group = groups[t.type]
        group["type"] = str(t.type) if t.type else ""  # ensures JSON serialization
        if hasattr(t.type, "name"):
            group["type"] = t.type.name
        elif hasattr(t.type, "value"):
            group["type"] = t.type.value

        group["total"] += 1

        if t.status == AutomationTaskStatus.PENDING:
            group["pending"] += 1
        elif t.status == AutomationTaskStatus.RUNNING:
            group["running"] += 1
        elif t.status == AutomationTaskStatus.SUCCESS:
            group["success"] += 1
        elif t.status == AutomationTaskStatus.FAILED:
            group["failed"] += 1
        elif t.status == AutomationTaskStatus.CANCELLED:
            group["cancelled"] += 1

    return {"groups": list(groups.values())}


@router.get("/partials/task-details-inline")
async def get_task_details_inline_partial(
    request: Request,
    job_id: UUID,
    task_type: str,
    service: SyncService = Depends(get_service),
):
    tasks = await service.get_automation_tasks(job_id)
    # Filter by Enum name or value to be safe
    filtered_tasks = [
        t
        for t in tasks
        if str(t.type) == task_type
        or getattr(t.type, "value", str(t.type)) == task_type
        or getattr(t.type, "name", str(t.type)) == task_type
    ]

    return templates.TemplateResponse(
        "task_details_inline_partial.html",
        {
            "request": request,
            "tasks": filtered_tasks,
            "task_type": task_type,
            "job_id": str(job_id),
        },
    )


@router.get("/partials/task-payload")
async def get_task_details_partial(
    request: Request,
    task_id: Optional[UUID] = None,
    service: SyncService = Depends(get_service),
):
    if task_id:
        task = await service.repo.get_task(task_id)
        if task:
            return templates.TemplateResponse(
                "task_payload.html",
                {
                    "request": request,
                    "task": task,
                    "task_id": str(task_id) if task_id else None,
                },
            )


def group_logs_chronologically(logs):
    grouped: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None
    for log in logs:
        if not log.task_id:
            grouped.append({"task_id": None, "is_job_log": True, "logs": [log]})
            current_group = None
        else:
            if current_group and current_group["task_id"] == log.task_id:
                logs_list = current_group["logs"]
                if isinstance(logs_list, list):
                    logs_list.append(log)
            else:
                current_group = {
                    "task_id": log.task_id,
                    "is_job_log": False,
                    "logs": [log],
                }
                grouped.append(current_group)
    return grouped


@router.get("/partials/task-log")
async def get_task_log_partial(
    request: Request, task_id: UUID, service: SyncService = Depends(get_service)
):
    task = await service.repo.get_task(task_id)
    logs = await service.repo.get_task_logs(task_id)
    return templates.TemplateResponse(
        "task_log_partial.html",
        {
            "request": request,
            "task": task,
            "logs": logs,
            "grouped_logs": group_logs_chronologically(logs),
            "task_id": str(task_id),
            "job_status": task.status if task else None,
        },
    )


@router.get("/partials/logs")
async def get_logs_partial(
    request: Request,
    job_id: UUID,
    task_type: Optional[str] = None,
    service: SyncService = Depends(get_service),
):
    logs = await service.get_job_logs(job_id)
    if task_type:
        tasks = await service.get_automation_tasks(job_id)
        valid_task_ids = {
            str(t.id)
            for t in tasks
            if str(t.type).upper() == task_type.upper()
            or getattr(t.type, "name", str(t.type)).upper() == task_type.upper()
        }
        logs = [
            log
            for log in logs
            if (log.task_id and str(log.task_id) in valid_task_ids) or not log.task_id
        ]

    job_status = await service.get_job_status(job_id)
    return templates.TemplateResponse(
        "logs_partial.html",
        {
            "request": request,
            "logs": logs,
            "grouped_logs": group_logs_chronologically(logs),
            "job_id": str(job_id),
            "job_status": job_status,
            "task_type": task_type,
        },
    )


@router.get("/partials/log-entries", dependencies=[Depends(require_auth)])
async def get_log_entries_partial(
    request: Request,
    job_id: Optional[UUID] = None,
    task_id: Optional[UUID] = None,
    task_type: Optional[str] = None,
    service: SyncService = Depends(get_service),
):
    if task_id:
        logs = await service.repo.get_task_logs(task_id)
        return templates.TemplateResponse(
            "log_entries_partial.html",
            {
                "request": request,
                "grouped_logs": group_logs_chronologically(logs),
                "task_id": str(task_id),
            },
        )
    if job_id:
        logs = await service.get_job_logs(job_id)
        job_status = await service.get_job_status(job_id)
        if task_type:
            tasks = await service.get_automation_tasks(job_id)
            valid_task_ids = {
                str(t.id)
                for t in tasks
                if str(t.type).upper() == task_type.upper()
                or getattr(t.type, "name", str(t.type)).upper() == task_type.upper()
            }
            logs = [
                log
                for log in logs
                if (log.task_id and str(log.task_id) in valid_task_ids)
                or not log.task_id
            ]

        return templates.TemplateResponse(
            "log_entries_partial.html",
            {
                "request": request,
                "grouped_logs": group_logs_chronologically(logs),
                "job_id": str(job_id),
                "task_type": task_type,
                "job_status": job_status,
            },
        )

    return templates.TemplateResponse(
        "log_entries_partial.html", {"request": request, "grouped_logs": []}
    )


@router.get("/api/user/credentials", dependencies=[Depends(require_auth)])
async def get_user_credentials(request: Request, db: AsyncSession = Depends(get_db)):
    """Get the credentials for the current user."""
    username = request.state.username
    repo = SqlAlchemyRepo(db)
    user = await repo.get_user_by_username(username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    credentials = await repo.get_user_credentials(user.id)
    if credentials is None:
        # Return empty dict if no credentials set
        return {}

    # Decrypt passwords for form display
    fiorilli_password = ""
    ahgora_password = ""
    if credentials.get("fiorilli_password_encrypted"):
        try:
            fiorilli_password = decrypt_password(
                credentials["fiorilli_password_encrypted"]
            )
        except Exception:
            pass
    if credentials.get("ahgora_password_encrypted"):
        try:
            ahgora_password = decrypt_password(credentials["ahgora_password_encrypted"])
        except Exception:
            pass

    return {
        "fiorilli_url": credentials.get("fiorilli_url"),
        "fiorilli_user": credentials.get("fiorilli_user"),
        "fiorilli_password": fiorilli_password,
        "ahgora_url": credentials.get("ahgora_url"),
        "ahgora_user": credentials.get("ahgora_user"),
        "ahgora_password": ahgora_password,
        "ahgora_company": credentials.get("ahgora_company"),
    }


@router.put("/api/user/credentials", dependencies=[Depends(require_auth)])
async def save_user_credentials(
    request: Request,
    fiorilli_url: str = Form(None),
    fiorilli_user: str = Form(None),
    fiorilli_password: str = Form(None),
    ahgora_url: str = Form(None),
    ahgora_user: str = Form(None),
    ahgora_password: str = Form(None),
    ahgora_company: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Save or update the credentials for the current user."""
    username = request.state.username
    repo = SqlAlchemyRepo(db)
    user = await repo.get_user_by_username(username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    existing = await repo.get_user_credentials(user.id) or {}

    def get_valid_value(form_value, key):
        if form_value and form_value.strip():
            return form_value.strip()
        return existing.get(key)

    # Encrypt passwords before storing
    fiorilli_password_encrypted = None
    if existing:
        fiorilli_password_encrypted = existing.get("fiorilli_password_encrypted")
    if fiorilli_password:
        fiorilli_password_encrypted = encrypt_password(fiorilli_password)

    ahgora_password_encrypted = None
    if existing:
        ahgora_password_encrypted = existing.get("ahgora_password_encrypted")
    if ahgora_password:
        ahgora_password_encrypted = encrypt_password(ahgora_password)

    credentials_dict = {
        "fiorilli_url": get_valid_value(fiorilli_url, "fiorilli_url"),
        "fiorilli_user": get_valid_value(fiorilli_user, "fiorilli_user"),
        "fiorilli_password_encrypted": fiorilli_password_encrypted,
        "ahgora_url": get_valid_value(ahgora_url, "ahgora_url"),
        "ahgora_user": get_valid_value(ahgora_user, "ahgora_user"),
        "ahgora_password_encrypted": ahgora_password_encrypted,
        "ahgora_company": get_valid_value(ahgora_company, "ahgora_company"),
    }

    await repo.save_user_credentials(user.id, credentials_dict)

    # Keep legacy API response for tests: return {"status": "ok"}
    return {"status": "ok"}
