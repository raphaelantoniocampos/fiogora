import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Base
    APP_NAME: str = "Fiogora"
    VERSION: str = "1.0.0"

    # Paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = BASE_DIR / "data"
    DOWNLOADS_DIR: Path = BASE_DIR / "downloads"
    CORE_DIR_PATH: Path = Path(__file__).resolve().parent
    EXCEPTIONS_JSON_PATH: Path = DATA_DIR / "exceptions.json"
    MAPPINGS_DIR: Path = DATA_DIR / "mappings"
    CONSTANTS_JSON_PATH: Path = MAPPINGS_DIR / "constants.json"

    # Browser / Automation
    IS_DOCKER: bool = os.getenv("IS_DOCKER", "False").lower() == "true"
    HEADLESS_MODE: bool = (
        True if IS_DOCKER else os.getenv("HEADLESS_MODE", "True").lower() == "true"
    )
    HEADLESS_MODE_TASKS: bool = (
        True
        if IS_DOCKER
        else os.getenv("HEADLESS_MODE_TASKS", "True").lower() == "true"
    )
    FIORILLI_URL: str = os.getenv("FIORILLI_URL", "")
    AHGORA_URL: str = os.getenv("AHGORA_URL", "")
    LEAVES_MONTHS_AGO: int = int(os.getenv("LEAVES_MONTHS_AGO", "3"))
    MAX_AGE_MINUTES: int = int(os.getenv("MAX_AGE_MINUTES", "60"))
    SYNC_TIMEOUT_MAX: int = int(os.getenv("SYNC_TIMEOUT_MAX", "30"))
    USE_CACHED_FILES: bool = os.getenv("USE_CACHED_FILES", "True").lower() == "true"
    UPDATE_LOCATIONS: bool = os.getenv("UPDATE_LOCATIONS", "True").lower() == "true"

    # Credentials
    FIORILLI_USER: str = os.getenv("FIORILLI_USER", "")
    AHGORA_USER: str = os.getenv("AHGORA_USER", "")
    AHGORA_COMPANY: str = os.getenv("AHGORA_COMPANY", "")

    # Authentication
    ADMIN_USERNAME: str = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "changeme123")
    SECRET_KEY: str = os.getenv(
        "SECRET_KEY", "b39dc1fdb917c0df61bb8160abdfdffc9641d401340156d9be72aa0639d67119"
    )
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/fiogora"
    )

    def __init__(self):
        self._constants = self._load_json(self.CONSTANTS_JSON_PATH)
        self.UPLOAD_LEAVES_COLUMNS = self._constants.get("upload_leaves_columns", [])
        self.LEAVES_COLUMNS = self._constants.get("leaves_columns", [])
        self.AHGORA_EMPLOYEES_COLUMNS = self._constants.get(
            "ahgora_employees_columns", []
        )
        self.FIORILLI_EMPLOYEES_COLUMNS = self._constants.get(
            "fiorilli_employees_columns", []
        )
        self.COLUMNS_TO_VERIFY_CHANGE = self._constants.get(
            "columns_to_verify_change", []
        )
        self.PT_MONTHS = self._constants.get("pt_months", {})

        self.reload_exceptions()

    def _load_json(self, path: Path):
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def reload_exceptions(self):
        exceptions_data = self._load_json(self.EXCEPTIONS_JSON_PATH)
        self.EXCEPTIONS_AND_TYPOS = exceptions_data.get("typos", {})
        self.IGNORE_LOCATION_CHANGE_IDS = exceptions_data.get(
            "ignore_location_change_ids", []
        )

    def save_exceptions(self):
        data = {
            "typos": self.EXCEPTIONS_AND_TYPOS,
            "ignore_location_change_ids": self.IGNORE_LOCATION_CHANGE_IDS,
        }
        with open(self.EXCEPTIONS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)


settings = Settings()
