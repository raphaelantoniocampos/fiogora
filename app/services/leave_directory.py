"""Leaves of the "Afastamentos" page and their search.

The list is the Ahgora leaves saved in the database (the ones the dashboard counts): the
leaves imported by the syncs and the ones Ahgora already had. They only hold the
matrícula, so the name, position and department come from the employees of the
"Funcionários" page.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum
from typing import Any, ClassVar, Optional

import pandas as pd

from app.services import employee_directory
from app.services.employee_directory import Employee
from app.services.fuzzy_search import Match, SearchWord, find, search_words
from app.services.sync_service import SyncService

# Fields searched word by word, and fields holding a dd/mm/YYYY date
TEXT_FIELDS = ("name", "cod_name", "cod", "position", "department")
DATE_FIELDS = ("start_date", "end_date")

# A hit on the employee, the kind of leave or its dates counts in full, so a leave named
# like the search ranks above a department matched the same way
MAIN_FIELDS = frozenset({"name", "id", "cod_name", "cod", "start_date", "end_date"})


class LeaveStatus(IntEnum):
    """In the order of the list."""

    CURRENT = 0
    UPCOMING = 1
    ENDED = 2


@dataclass(frozen=True)
class Leave:
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = TEXT_FIELDS
    DATE_FIELDS: ClassVar[tuple[str, ...]] = DATE_FIELDS
    MAIN_FIELDS: ClassVar[frozenset[str]] = MAIN_FIELDS

    id: str  # matrícula
    cod: str
    start: date
    end: Optional[date] = None
    cod_name: Optional[str] = None
    duration: Optional[int] = None  # days
    # Of the employee; empty when the matrícula is not among the saved employees
    name: str = ""
    position: Optional[str] = None
    department: Optional[str] = None
    # dd/mm/YYYY of start and end, which the search goes through
    start_date: str = field(init=False, compare=False)
    end_date: Optional[str] = field(init=False, compare=False)
    # What the search goes through, worked out once per leave
    search_words: tuple[SearchWord, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_date", self.start.strftime("%d/%m/%Y"))
        object.__setattr__(
            self, "end_date", self.end.strftime("%d/%m/%Y") if self.end else None
        )
        object.__setattr__(self, "search_words", search_words(self))

    def status(self, today: date) -> LeaveStatus:
        if self.start > today:
            return LeaveStatus.UPCOMING
        if self.end is not None and self.end < today:
            return LeaveStatus.ENDED
        return LeaveStatus.CURRENT

    def value(self, field_name: str) -> str:
        return getattr(self, field_name) or ""


# --- loading ---------------------------------------------------------------------------

_leaves_cache: dict[str, Any] = {}


async def load_leaves(service: SyncService) -> list[Leave]:
    """Leaves saved from Ahgora with the data of their employee."""
    employees, _ = await employee_directory.load_employees(service)
    # Like the employees: the page searches as the user types, so reuse the list until a
    # leave is saved or the employees change (load_employees then builds a new list)
    version = await service.repo.get_ahgora_leaves_version()
    if (
        _leaves_cache.get("version") != version
        or _leaves_cache.get("employees") is not employees
    ):
        leaves_df = await service.repo.get_ahgora_leaves_df()
        _leaves_cache.update(
            version=version,
            employees=employees,
            leaves=build_leaves(leaves_df, employees),
        )
    return _leaves_cache["leaves"]


def build_leaves(leaves_df: pd.DataFrame, employees: list[Employee]) -> list[Leave]:
    if leaves_df.empty:
        return []
    by_id = {employee.id: employee for employee in employees}
    leaves = []
    for row in leaves_df.to_dict("records"):
        start = _day(row.get("start_date"))
        if start is None:
            continue
        end = _day(row.get("end_date"))
        employee_id = str(row.get("id") or "").zfill(6)
        employee = by_id.get(employee_id)
        leaves.append(
            Leave(
                id=employee_id,
                cod=str(row.get("cod") or "").strip(),
                start=start,
                end=end,
                cod_name=employee_directory._text(row.get("cod_name")),
                duration=_duration(row.get("duration"), start, end),
                name=employee.name if employee else "",
                position=employee.position if employee else None,
                department=employee.department if employee else None,
            )
        )
    return leaves


def _day(value: Any) -> Optional[date]:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, str):
        parsed = pd.to_datetime(value, format="%d/%m/%Y", errors="coerce")
        return None if pd.isna(parsed) else parsed.date()
    return value.date() if hasattr(value, "date") else value


def _duration(value: Any, start: date, end: Optional[date]) -> Optional[int]:
    """Days of the leave, counting both ends; worked out from the dates when missing."""
    if value is not None and not pd.isna(value):
        return int(value)
    return (end - start).days + 1 if end else None


# --- search ----------------------------------------------------------------------------


def search(
    leaves: list[Leave], query: str, today: date, include_ended: bool = False
) -> list[Match[Leave]]:
    """Leaves matching every term of the query (see fuzzy_search.find), best first;
    without a query, the current ones, then the upcoming, then the ended, the latest
    first."""
    pool = [
        leave
        for leave in leaves
        if include_ended or leave.status(today) != LeaveStatus.ENDED
    ]
    matches = find(pool, query)
    matches.sort(
        key=lambda m: (
            -m.score,
            m.unmatched_name_words,
            m.record.status(today),
            -m.record.start.toordinal(),
            m.record.name,
        )
    )
    return matches
