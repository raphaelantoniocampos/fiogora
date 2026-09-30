from app.domain.entities import SyncJob, SyncStatus
from app.core.settings import settings
from app.infrastructure.automation.web.fiorilli_browser import FiorilliBrowser


def test_domain_imports():
    job = SyncJob()
    assert job.status == SyncStatus.PENDING


def test_settings_load():
    assert settings.APP_NAME == "FioGora"


def test_browser_import():
    # Only verify we can access the class
    assert FiorilliBrowser is not None
