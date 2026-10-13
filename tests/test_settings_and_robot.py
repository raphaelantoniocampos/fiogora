import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import create_access_token
from app.core.settings import settings
from app.infrastructure.automation.web.ahgora_browser import AhgoraBrowser
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with TestClient(app) as c:
        c.cookies.set(
            "access_token", create_access_token({"sub": "testuser", "is_admin": False})
        )
        yield c
    app.dependency_overrides.clear()


def test_location_switch_is_saved_to_the_runtime_env_file(client, tmp_path):
    runtime_env = tmp_path / "runtime.env"

    with (
        patch.object(settings, "RUNTIME_ENV_PATH", runtime_env),
        patch.object(settings, "UPDATE_LOCATIONS", False),
    ):
        response = client.post("/api/settings/toggle-locations")
        assert response.status_code == 200
        assert settings.UPDATE_LOCATIONS is True

    assert runtime_env.read_text().strip() == "UPDATE_LOCATIONS='True'"


def test_in_docker_the_runtime_file_lives_in_the_data_volume_and_wins():
    """Values set in the UI must survive a container restart (env_file would reset them)."""
    runtime_env = settings.BASE_DIR / "data" / "runtime.env"
    if runtime_env.exists():
        pytest.skip("data/runtime.env already exists here")
    runtime_env.write_text("UPDATE_LOCATIONS='True'\n")
    try:
        output = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app.core.settings import settings;"
                "print(settings.RUNTIME_ENV_PATH.name, settings.UPDATE_LOCATIONS)",
            ],
            env={"IS_DOCKER": "True", "UPDATE_LOCATIONS": "False", "PATH": ""},
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    finally:
        runtime_env.unlink()

    assert output.split() == ["runtime.env", "True"]


def _robot():
    browser = AhgoraBrowser.__new__(AhgoraBrowser)
    browser.driver = MagicMock()
    browser.driver.current_url = "https://app.ahgora.com.br/home"
    for method in (
        "wait",
        "_log",
        "send_keys",
        "click_element",
        "_set_autocomplete_select",
    ):
        setattr(browser, method, MagicMock())
    browser._update_location_multiselect = MagicMock(return_value=True)
    return browser


PAYLOAD = {
    "id": "1234",
    "name_expected": "ANGELICA",
    "department_expected": "DELEGACIA",
    "location_actual": ["PONTO DEPARTAMENTO DE PESSOAL"],
    "location_expected": ["PONTO DELEGACIA"],
}


def test_robot_updates_location_and_logs_from_and_to():
    browser = _robot()

    with (
        patch.object(settings, "UPDATE_LOCATIONS", True),
        patch.object(settings, "IGNORE_LOCATION_CHANGE_IDS", []),
    ):
        browser.update_employee(dict(PAYLOAD))

    browser._update_location_multiselect.assert_called_once_with("DELEGACIA")
    browser._log.assert_any_call(
        "INFO",
        "Localização alterada: PONTO DEPARTAMENTO DE PESSOAL → PONTO DELEGACIA",
    )


@pytest.mark.parametrize(
    "update_locations, ignored_ids",
    [(True, ["001234"]), (False, [])],
)
def test_robot_leaves_location_alone(update_locations, ignored_ids):
    browser = _robot()

    with (
        patch.object(settings, "UPDATE_LOCATIONS", update_locations),
        patch.object(settings, "IGNORE_LOCATION_CHANGE_IDS", ignored_ids),
    ):
        browser.update_employee(dict(PAYLOAD))

    browser._update_location_multiselect.assert_not_called()
    browser.click_element.assert_not_called()  # nothing changed, nothing saved
