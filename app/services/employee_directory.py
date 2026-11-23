"""Employees of the "Funcionários" page and its fuzzy search.

The list is the Ahgora employees saved in the database (the ones the dashboard counts).
Birth date, binding and cost center only exist in the last Fiorilli export
(data/fiorilli_employees.txt, downloaded by every sync), so they are joined by matrícula.
"""

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Optional

import pandas as pd

from app.core.settings import settings
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

# A hit on the name, matrícula or birth date counts in full, on the other fields 70%, so a
# name ranks above a department matched the same way
MAIN_FIELDS = frozenset({"name", "id", "birth_date"})
OTHER_FIELDS_WEIGHT = 0.7

# Scores of a term against a word
EXACT, PREFIX, INSIDE, TYPO = 100, 80, 60, 35
# Only on the name: beginnings of its words in order, like initials or words typed
# without spaces ("mdss", "mariasilva" -> MARIA DA SILVA SANTOS), the letters in order
# inside one word ("mra" -> MARIA); and a bonus for the first name
WORD_STARTS, LETTERS_IN_WORD, FIRST_NAME_BONUS = 50, 45, 10
FULL_DATE, PARTIAL_DATE, SWAPPED_DATE = 100, 70, 30
SAME_ID, ID_PREFIX, ID_INSIDE = 100, 70, 50

# Shorter terms only match whole words or word starts (or the same matrícula number):
# one or two letters are inside almost every name
MIN_LOOSE_LENGTH = 3
MIN_TYPO_LENGTH = 4
# Name particles: they count when found in the name but never exclude anyone
OPTIONAL_TERMS = frozenset({"da", "de", "do", "das", "dos", "e"})

_WORD = re.compile(r"[^\W_]+")
_NUMERIC_TERM = re.compile(r"\d[\d/.\-]*")
_DATE_SEPARATOR = re.compile(r"[/.\-]")
_EDGE_PUNCTUATION = re.compile(r"^[\W_]+|[\W_]+$")


@dataclass(frozen=True)
class Employee:
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
    # What the search goes through, worked out once per employee:
    # (field, in the name, weight, first-name bonus, position, word)
    search_words: tuple[tuple[str, bool, float, int, int, str], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "search_words", _search_words(self))

    @property
    def dismissed(self) -> bool:
        return self.dismissal_date is not None

    def value(self, field_name: str) -> str:
        return getattr(self, field_name) or ""


@dataclass
class Match:
    employee: Employee
    score: float = 0
    # Matched (start, end) positions in each field
    spans: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # Words of the name the search did not reach (0 when it reached no word of the name)
    unmatched_name_words: int = 0

    def segments(self, field_name: str) -> list[tuple[str, bool]]:
        """The field split in pieces, each flagged whether the search matched it."""
        text = self.employee.value(field_name)
        segments: list[tuple[str, bool]] = []
        position = 0
        for start, end in _merge(self.spans.get(field_name, [])):
            if start > position:
                segments.append((text[position:start], False))
            segments.append((text[start:end], True))
            position = end
        if position < len(text):
            segments.append((text[position:], False))
        return segments


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


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
) -> list[Match]:
    """Employees matching every term of the query, in any order and in any field, best
    first; without a query, everyone by name.

    Accents and case are ignored. Each term keeps its best hit:
    - on the words of the text fields: whole word, word start, inside a word, or one typo
      (a letter changed, missing, extra or two swapped; not in numbers); on the name also
      the letters in order inside a word, and beginnings of its words in order
      (initials, or words typed without spaces);
    - digits, with or without / . - separators, on the dates: complete (05/03/1980,
      5/3/80, 05031980, 1980-03-05), partial (05/03, 0503, 03/1980, 1980) or with day and
      month swapped; and on the matrícula (4930 -> 004930).
    """
    pool = [e for e in employees if include_dismissed or not e.dismissed]
    terms = _parse_query(query)
    if terms:
        matches = [m for e in pool if (m := _match(e, terms))]
    else:
        matches = [Match(e) for e in pool]
    matches.sort(
        key=lambda m: (
            -m.score,
            m.unmatched_name_words,
            m.employee.name,
            m.employee.dismissed,
        )
    )
    return matches


@lru_cache(maxsize=65536)
def fold(text: str) -> str:
    """Lower case without accents, one character for each character of `text`, so that
    positions found in the result are positions in `text` too."""
    return "".join(_fold_char(char) for char in text)


@lru_cache(maxsize=None)
def _fold_char(char: str) -> str:
    base = "".join(
        c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c)
    )
    return (base or char).lower()[:1] or char


@lru_cache(maxsize=65536)
def _words(text: str) -> tuple[tuple[int, str], ...]:
    """(position, word) of the words of the folded text: runs of letters and digits."""
    return tuple((m.start(), m.group()) for m in _WORD.finditer(fold(text)))


def _search_words(
    employee: Employee,
) -> tuple[tuple[str, bool, float, int, int, str], ...]:
    return tuple(
        (
            field_name,
            field_name == "name",
            _weight(field_name),
            FIRST_NAME_BONUS if field_name == "name" and index == 0 else 0,
            start,
            word,
        )
        for field_name in TEXT_FIELDS
        if (value := getattr(employee, field_name))
        for index, (start, word) in enumerate(_words(value))
    )


DateReading = tuple[Optional[int], Optional[int], Optional[str]]
WordScore = tuple[int, tuple[tuple[int, int], ...]]


@dataclass
class _Term:
    text: str  # folded
    optional: bool = False
    words: bool = True  # matched against the words of the text fields
    number: bool = False  # digits only: also a matrícula
    dates: list[DateReading] = field(default_factory=list)
    # Score of each word already seen in this search, in the name and in other fields:
    # the words of all employees repeat a lot
    name_scores: dict[str, WordScore] = field(default_factory=dict)
    other_scores: dict[str, WordScore] = field(default_factory=dict)


def _parse_query(query: str) -> list[_Term]:
    terms = []
    for token in query.split():
        token = _EDGE_PUNCTUATION.sub("", fold(token))
        if not token:
            continue
        if _NUMERIC_TERM.fullmatch(token):
            digits_only = token.isdigit()
            terms.append(
                _Term(
                    token,
                    words=digits_only,
                    number=digits_only,
                    dates=_date_readings(token),
                )
            )
        else:
            # "serv.urb-coleta" is three words, like in the fields
            terms.extend(
                _Term(word, optional=word in OPTIONAL_TERMS)
                for word in _WORD.findall(token)
            )
    return terms


def _date_readings(term: str) -> list[DateReading]:
    """The dates a term of digits and separators can be: (day, month, year), any of them
    unknown; the year keeps its 2 or 4 digits."""
    parts = [part for part in _DATE_SEPARATOR.split(term) if part]
    readings: list[tuple[Optional[str], Optional[str], Optional[str]]] = []
    if len(parts) == 1:
        digits = parts[0]
        if len(digits) == 8:
            readings.append((digits[:2], digits[2:4], digits[4:]))  # ddmmyyyy
            readings.append((digits[6:], digits[4:6], digits[:4]))  # yyyymmdd
        elif len(digits) == 6:
            readings.append((digits[:2], digits[2:4], digits[4:]))  # ddmmyy
        elif len(digits) == 4:
            readings.append((digits[:2], digits[2:], None))  # ddmm
            readings.append((None, None, digits))  # yyyy
    elif len(parts) == 2:
        first, second = parts
        if len(first) == 4:
            readings.append((None, second, first))  # yyyy-mm
        elif len(second) == 4:
            readings.append((None, first, second))  # mm/yyyy
        elif int(second) <= 12:
            readings.append((first, second, None))  # dd/mm
        else:
            readings.append((None, first, second))  # mm/yy
    elif len(parts) == 3:
        first, second, third = parts
        if len(first) == 4:
            readings.append((third, second, first))  # yyyy-mm-dd
        else:
            readings.append((first, second, third))  # dd/mm/yy(yy)
    return [reading for raw in readings if (reading := _valid_date(*raw))]


def _valid_date(
    day: Optional[str], month: Optional[str], year: Optional[str]
) -> Optional[DateReading]:
    if day is not None and not (len(day) <= 2 and 1 <= int(day) <= 31):
        return None
    if month is not None and not (len(month) <= 2 and 1 <= int(month) <= 12):
        return None
    if year is not None and len(year) not in (2, 4):
        return None
    return (
        int(day) if day is not None else None,
        int(month) if month is not None else None,
        year,
    )


def _match(employee: Employee, terms: list[_Term]) -> Optional[Match]:
    match = Match(employee)
    for term in terms:
        score, field_name, spans = _best_hit(employee, term)
        if not score:
            if term.optional:
                continue
            return None
        match.score += score
        match.spans.setdefault(field_name, []).extend(spans)
    if "name" in match.spans:
        # Ties go to the name the search covers best: "maria silva" ranks MARIA SILVA
        # above MARIA APARECIDA SILVA
        spans = match.spans["name"]
        match.unmatched_name_words = sum(
            not any(
                start < word_start + len(word) and word_start < end
                for start, end in spans
            )
            for word_start, word in _words(employee.name)
        )
    return match


def _weight(field_name: str) -> float:
    return 1.0 if field_name in MAIN_FIELDS else OTHER_FIELDS_WEIGHT


def _best_hit(
    employee: Employee, term: _Term
) -> tuple[float, str, list[tuple[int, int]]]:
    """(score, field, matched positions) of the best hit of the term on the employee."""
    best: tuple[float, str, list[tuple[int, int]]] = (0, "", [])

    if term.optional:
        for start, word in _words(employee.name):
            if word == term.text:
                return EXACT, "name", [(start, start + len(word))]
        return best

    for field_name in DATE_FIELDS if term.dates else ():
        value = getattr(employee, field_name)
        if value:
            score = _date_score(term.dates, value) * _weight(field_name)
            if score > best[0]:
                best = (score, field_name, [(0, len(value))])

    if term.number:
        id_score, id_spans = _id_score(term.text, employee.id)
        if id_score > best[0]:
            best = (id_score, "id", id_spans)

    if term.words:
        name_scores, other_scores = term.name_scores, term.other_scores
        for field_name, in_name, weight, bonus, start, word in employee.search_words:
            scores = name_scores if in_name else other_scores
            word_score = scores.get(word)
            if word_score is None:
                word_score = scores[word] = _word_score(term.text, word, in_name)
            tier, word_spans = word_score
            if tier and (tier + bonus) * weight > best[0]:
                best = (
                    (tier + bonus) * weight,
                    field_name,
                    [(start + a, start + b) for a, b in word_spans],
                )
        if (
            best[0] < WORD_STARTS
            and not term.number
            and len(term.text) >= MIN_LOOSE_LENGTH
        ):
            spans = _word_starts(term.text, _words(employee.name))
            if spans:
                best = (WORD_STARTS, "name", spans)

    return best


def _word_starts(
    term: str, words: tuple[tuple[int, str], ...]
) -> Optional[list[tuple[int, int]]]:
    """Matched positions when the term is made of beginnings of the words, in order and
    maybe skipping some: "mdss" and "mariasilva" -> MARIA DA SILVA SANTOS."""
    if not any(word[0] == term[0] for _, word in words):
        return None
    dead_ends: set[tuple[int, int]] = set()

    # Not annotated: annotations of a nested function are evaluated on every call
    def match(position, first_word):
        if position == len(term):
            return []
        if (position, first_word) in dead_ends:
            return None
        for index in range(first_word, len(words)):
            start, word = words[index]
            length = 0
            while (
                length < len(word)
                and position + length < len(term)
                and word[length] == term[position + length]
            ):
                length += 1
            # Take as much of the word as possible, less if the rest does not fit
            for taken in range(length, 0, -1):
                rest = match(position + taken, index + 1)
                if rest is not None:
                    return [(start, start + taken), *rest]
        dead_ends.add((position, first_word))
        return None

    return match(0, 0)


def _word_score(
    term: str, word: str, in_name: bool
) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Score of a term against a word, and the matched positions in the word."""
    if word == term:
        return EXACT, ((0, len(term)),)
    if word.startswith(term):
        return PREFIX, ((0, len(term)),)
    if len(term) < MIN_LOOSE_LENGTH:
        return 0, ()
    index = word.find(term)
    if index >= 0:
        return INSIDE, ((index, index + len(term)),)
    if in_name:
        positions = _letters_in_order(term, word)
        if positions:
            return LETTERS_IN_WORD, tuple((p, p + 1) for p in positions)
    # A typo in a number is another number
    if (
        len(term) >= MIN_TYPO_LENGTH
        and not term.isdigit()
        and _one_edit_apart(term, word)
    ):
        return TYPO, ((0, len(word)),)
    return 0, ()


def _letters_in_order(term: str, text: str) -> Optional[list[int]]:
    positions = []
    position = -1
    for char in term:
        position = text.find(char, position + 1)
        if position < 0:
            return None
        positions.append(position)
    return positions


def _one_edit_apart(a: str, b: str) -> bool:
    """A letter changed, missing or extra, or two neighbours swapped."""
    if abs(len(a) - len(b)) > 1 or a == b:
        return False
    if len(a) == len(b):
        diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        return len(diff) == 1 or (
            len(diff) == 2
            and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]]
            and a[diff[1]] == b[diff[0]]
        )
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(shorter) and shorter[i] == longer[i]:
        i += 1
    return shorter[i:] == longer[i + 1 :]


@lru_cache(maxsize=65536)
def _split_date(value: str) -> Optional[tuple[int, int, str]]:
    parts = value.split("/")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), parts[2]


def _date_score(readings: list[DateReading], value: str) -> int:
    date = _split_date(value)
    if not date:
        return 0
    best = 0
    for day, month, year in readings:
        complete = day is not None and month is not None and year is not None
        if _date_fits(day, month, year, date):
            best = max(best, FULL_DATE if complete else PARTIAL_DATE)
        elif complete and day != month and _date_fits(month, day, year, date):
            best = max(best, SWAPPED_DATE)
    return best


def _date_fits(
    day: Optional[int],
    month: Optional[int],
    year: Optional[str],
    date: tuple[int, int, str],
) -> bool:
    return (
        (day is None or day == date[0])
        and (month is None or month == date[1])
        and (year is None or date[2][-len(year) :] == year)
    )


def _id_score(term: str, employee_id: str) -> tuple[int, list[tuple[int, int]]]:
    significant = term.lstrip("0")
    id_significant = employee_id.lstrip("0")
    zeros = len(employee_id) - len(id_significant)
    if significant and significant == id_significant:
        return SAME_ID, [(0, len(employee_id))]
    if len(term) >= MIN_LOOSE_LENGTH:
        if employee_id.startswith(term):
            return ID_PREFIX, [(0, len(term))]
        if significant == term and id_significant.startswith(term):
            return ID_PREFIX, [(zeros, zeros + len(term))]
        index = employee_id.find(term)
        if index >= 0:
            return ID_INSIDE, [(index, index + len(term))]
    return 0, []
