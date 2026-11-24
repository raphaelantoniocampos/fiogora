"""Employees of the "Funcionários" page and their search.

The list is the Ahgora employees saved in the database (the ones the dashboard counts).
Birth date, binding and cost center only exist in the last Fiorilli export
(data/fiorilli_employees.txt, downloaded by every sync), so they are joined by matrícula.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, ClassVar, Optional

import pandas as pd

from app.core.settings import settings
from app.services.fuzzy_search import Match, SearchWord, find, search_words
from app.services.sync_service import SyncService, parse_ahgora_locations

FIORILLI_FILE = "fiorilli_employees.txt"
FIORILLI_FIELDS = ("birth_date", "binding", "cost_center")

# Fields searched word by word, and fields holding a dd/mm/YYYY date
TEXT_FIELDS = (
    "name",
    "position",
    "department",
    "location",
    "scale",
    "binding",
    "cost_center",
)
DATE_FIELDS = ("birth_date", "admission_date", "dismissal_date")

# A hit on the name, matrícula or birth date counts in full, so a name ranks above a
# department matched the same way
MAIN_FIELDS = frozenset({"name", "id", "birth_date"})


@dataclass(frozen=True)
class Employee:
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = TEXT_FIELDS
    DATE_FIELDS: ClassVar[tuple[str, ...]] = DATE_FIELDS
    MAIN_FIELDS: ClassVar[frozenset[str]] = MAIN_FIELDS

    id: str
    name: str
    position: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None  # "A, B"
    scale: Optional[str] = None
    admission_date: Optional[str] = None  # dd/mm/YYYY
    dismissal_date: Optional[str] = None
    birth_date: Optional[str] = None
    binding: Optional[str] = None
    cost_center: Optional[str] = None
    # What the search goes through, worked out once per employee
    search_words: tuple[SearchWord, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "search_words", search_words(self))

    @property
    def dismissed(self) -> bool:
        return self.dismissal_date is not None

    def value(self, field_name: str) -> str:
        return getattr(self, field_name) or ""


# --- loading ---------------------------------------------------------------------------

_fiorilli_cache: dict[str, Any] = {}
_employees_cache: dict[str, Any] = {}


async def load_employees(
    service: SyncService,
) -> tuple[list[Employee], Optional[datetime]]:
    """Employees saved from Ahgora with the Fiorilli data, and when the Fiorilli file was
    downloaded (None while there is no file)."""
    fiorilli, fiorilli_updated_at = await _fiorilli_data(service._read_csv)
    # Loading and building thousands of employees takes longer than searching them, and
    # the page searches as the user types: reuse the list until an employee is saved or a
    # sync downloads a new Fiorilli file
    key = (await service.repo.get_ahgora_employees_version(), fiorilli_updated_at)
    if _employees_cache.get("key") != key:
        employees_df = await service.repo.get_ahgora_employees_df()
        _employees_cache.update(
            key=key, employees=build_employees(employees_df, fiorilli)
        )
    return _employees_cache["employees"], fiorilli_updated_at


async def _fiorilli_data(
    read_csv: Callable[[Any], pd.DataFrame],
) -> tuple[dict[str, dict[str, Optional[str]]], Optional[datetime]]:
    """Birth date, binding and cost center by matrícula. Parsing the file takes a second or
    two, so it is parsed again only when a sync downloads a new one."""
    path = settings.DATA_DIR / FIORILLI_FILE
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}, None
    if _fiorilli_cache.get("mtime") != mtime:
        df = await asyncio.to_thread(read_csv, path)
        _fiorilli_cache.update(mtime=mtime, data=_fiorilli_by_id(df))
    return _fiorilli_cache["data"], datetime.fromtimestamp(mtime)


def _fiorilli_by_id(df: pd.DataFrame) -> dict[str, dict[str, Optional[str]]]:
    if df.empty or "id" not in df:
        return {}
    columns = ["id", *(c for c in FIORILLI_FIELDS if c in df)]
    return {
        str(row["id"]).zfill(6): {c: _text(row.get(c)) for c in FIORILLI_FIELDS}
        for row in df[columns].to_dict("records")
    }


def build_employees(
    employees_df: pd.DataFrame, fiorilli: dict[str, dict[str, Optional[str]]]
) -> list[Employee]:
    if employees_df.empty:
        return []
    rows = zip(
        *(
            _column(employees_df, name)
            for name in (
                "id",
                "name",
                "position",
                "department",
                "location",
                "scale",
                "admission_date",
                "dismissal_date",
            )
        )
    )
    employees = []
    for (
        employee_id,
        name,
        position,
        department,
        location,
        scale,
        admission_date,
        dismissal_date,
    ) in rows:
        employee_id = str(employee_id or "").zfill(6)
        extra = fiorilli.get(employee_id, {})
        employees.append(
            Employee(
                id=employee_id,
                name=_text(name) or "",
                position=_text(position),
                department=_text(department),
                location=", ".join(parse_ahgora_locations(location)) or None,
                scale=_text(scale),
                admission_date=_date(admission_date),
                dismissal_date=_date(dismissal_date),
                birth_date=extra.get("birth_date"),
                binding=extra.get("binding"),
                cost_center=extra.get("cost_center"),
            )
        )
    return employees


def _column(df: pd.DataFrame, name: str) -> list[Any]:
    """The column as Python values, None where missing. Dates become dd/mm/YYYY all at
    once: formatting them one by one is what took longest."""
    if name not in df:
        return [None] * len(df)
    column = df[name]
    if pd.api.types.is_datetime64_any_dtype(column):
        column = column.dt.strftime("%d/%m/%Y")
    return column.astype(object).where(column.notna(), None).tolist()


def _text(value: Any) -> Optional[str]:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return str(value).strip() or None


def _date(value: Any) -> Optional[str]:
    """dd/mm/YYYY of a date: already formatted, a datetime, or missing."""
    if value is None or isinstance(value, str):
        return _text(value)
    if pd.isna(value):
        return None
    return value.strftime("%d/%m/%Y")


# --- search ----------------------------------------------------------------------------


def search(
    employees: list[Employee], query: str, include_dismissed: bool = False
) -> list[Match[Employee]]:
    """Employees matching every term of the query (see fuzzy_search.find), best first;
    without a query, everyone by name."""
    pool = [e for e in employees if include_dismissed or not e.dismissed]
    matches = find(pool, query)
    matches.sort(
        key=lambda m: (
            -m.score,
            m.unmatched_name_words,
            m.record.name,
            m.record.dismissed,
        )
    )
    return matches
