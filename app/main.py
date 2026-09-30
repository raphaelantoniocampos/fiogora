import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.endpoints import router as api_router
from app.core.logging import log_context, setup_logging
from app.core.scheduler import scheduler
from app.core.settings import settings
from app.infrastructure.web.routes import router as web_router

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Cleanup any jobs/tasks stuck as RUNNING due to a system crash
    from app.core.database import async_session_factory
    from app.infrastructure.db.sqlalchemy_repo import SqlAlchemyRepo

    try:
        async with async_session_factory() as session:
            repo = SqlAlchemyRepo(session)
            await repo.cleanup_stuck_executions()
            logger.info("Cleaned up stuck RUNNING jobs/tasks successfully.")
    except Exception as e:
        logger.error(f"Error cleaning up stuck executions on startup: {e}")

    # Startup: Start the retry scheduler
    await scheduler.start()
    yield
    # Shutdown: Stop the retry scheduler
    await scheduler.stop()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description="RPA Integration service for Fiorilli and Ahgora employee data synchronization.",
    lifespan=lifespan,
    openapi_tags=[
        {
            "name": "sync",
            "description": "Core synchronization operations and job management.",
        },
        {
            "name": "Automation Tasks",
            "description": "Endpoints to manage and review tasks identified during sync analysis.",
        },
    ],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global exception caught: {exc}")
    logger.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )


# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Include routers
app.include_router(web_router)
app.include_router(api_router, prefix="/api/sync", tags=["sync"])


@app.get("/health")
def health_check():
    return {"status": "ok", "version": settings.VERSION}


@app.middleware("http")
async def extend_auth_cookie_middleware(request: Request, call_next):
    from app.core.security import decode_access_token

    token = request.cookies.get("access_token")
    request.state.is_admin = False
    request.state.username = None

    if token:
        payload = decode_access_token(token)
        if payload:
            request.state.is_admin = payload.get("is_admin", False)
            request.state.username = payload.get("sub", None)

    with log_context(username=request.state.username):
        response = await call_next(request)

    if token and request.url.path != "/logout":
        # Ensure we set cookie on response
        expires = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            max_age=expires,
            samesite="lax",
        )

    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
