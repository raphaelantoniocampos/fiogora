import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

# Who and what the running code is working for (username, sync job). Read by the server log
# formatter and by SqlAlchemyRepo.add_log, which records the user on each execution log entry.
# asyncio.to_thread and run_coroutine_threadsafe copy it, so browser threads inherit it too.
_log_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})

# Dashboard polling endpoints that would otherwise flood the access log every few seconds
QUIET_ACCESS_PATHS = ("/partials/jobs", "/partials/log-entries", "/health")


def get_log_context() -> dict[str, Any]:
    return _log_context.get()


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    """Bind values (username, job_id) to every log written inside the block."""
    bound = {key: value for key, value in values.items() if value is not None}
    token = _log_context.set({**_log_context.get(), **bound})
    try:
        yield
    finally:
        _log_context.reset(token)


class ContextFilter(logging.Filter):
    """Prefix records with the bound context, e.g. "[raphaelinfo sync=35acb2f2] "."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _log_context.get()
        parts = []
        if context.get("username"):
            parts.append(context["username"])
        if context.get("job_id"):
            parts.append(f"sync={str(context['job_id'])[:8]}")
        record.context = f"[{' '.join(parts)}] " if parts else ""
        return True


class QuietPollingFilter(logging.Filter):
    """Drop successful uvicorn access lines for the polling endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            path = str(args[2]).split("?")[0]
            status = args[4]
            if isinstance(status, int) and status < 400:
                if path in QUIET_ACCESS_PATHS or path.endswith("/tasks/summary"):
                    return False
        return True


def setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(context)s%(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("uvicorn.access").addFilter(QuietPollingFilter())
    for noisy in ("selenium", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
