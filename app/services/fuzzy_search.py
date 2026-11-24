"""Fuzzy search shared by the list pages (employees, leaves).

A record has a matrícula (`id`), a `name` and other text and date fields, named by its
class. Terms are matched in any order against any field, tolerating small typos.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from collections.abc import Sequence
from typing import ClassVar, Optional, Protocol

# A hit on a main field (see Searchable.MAIN_FIELDS) counts in full, on the other fields
# 70%, so a name ranks above a department matched the same way
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


# (field, in the name, weight, first-name bonus, position, word)
SearchWord = tuple[str, bool, float, int, int, str]


class Searchable(Protocol):
    TEXT_FIELDS: ClassVar[tuple[str, ...]]  # searched word by word
    DATE_FIELDS: ClassVar[tuple[str, ...]]  # holding a dd/mm/YYYY date
    MAIN_FIELDS: ClassVar[frozenset[str]]  # weighted in full

    # Read-only, so that frozen dataclasses fit
    @property
    def id(self) -> str: ...  # matrícula

    @property
    def name(self) -> str: ...

    @property
    def search_words(self) -> tuple[SearchWord, ...]:
        """What the search goes through, worked out once per record with search_words()"""
        ...

    def value(self, field_name: str) -> str: ...


@dataclass
class Match[R: Searchable]:
    record: R
    score: float = 0
    # Matched (start, end) positions in each field
    spans: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # Words of the name the search did not reach (0 when it reached no word of the name)
    unmatched_name_words: int = 0

    def segments(self, field_name: str) -> list[tuple[str, bool]]:
        """The field split in pieces, each flagged whether the search matched it."""
        text = self.record.value(field_name)
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


def find[R: Searchable](records: Sequence[R], query: str) -> list[Match[R]]:
    """Records matching every term of the query, in any order and in any field, unsorted;
    without a query, all of them.

    Accents and case are ignored. Each term keeps its best hit:
    - on the words of the text fields: whole word, word start, inside a word, or one typo
      (a letter changed, missing, extra or two swapped; not in numbers); on the name also
      the letters in order inside a word, and beginnings of its words in order
      (initials, or words typed without spaces);
    - digits, with or without / . - separators, on the dates: complete (05/03/1980,
      5/3/80, 05031980, 1980-03-05), partial (05/03, 0503, 03/1980, 1980) or with day and
      month swapped; and on the matrícula (4930 -> 004930).
    """
    terms = _parse_query(query)
    if not terms:
        return [Match(record) for record in records]
    return [m for record in records if (m := _match(record, terms))]


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


def search_words(record: Searchable) -> tuple[SearchWord, ...]:
    return tuple(
        (
            field_name,
            field_name == "name",
            _weight(record, field_name),
            FIRST_NAME_BONUS if field_name == "name" and index == 0 else 0,
            start,
            word,
        )
        for field_name in record.TEXT_FIELDS
        if (value := getattr(record, field_name))
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
    # the words of all records repeat a lot
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


def _match[R: Searchable](record: R, terms: list[_Term]) -> Optional[Match[R]]:
    match = Match(record)
    for term in terms:
        score, field_name, spans = _best_hit(record, term)
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
            for word_start, word in _words(record.name)
        )
    return match


def _weight(record: Searchable, field_name: str) -> float:
    return 1.0 if field_name in record.MAIN_FIELDS else OTHER_FIELDS_WEIGHT


def _best_hit(
    record: Searchable, term: _Term
) -> tuple[float, str, list[tuple[int, int]]]:
    """(score, field, matched positions) of the best hit of the term on the record."""
    best: tuple[float, str, list[tuple[int, int]]] = (0, "", [])

    if term.optional:
        for start, word in _words(record.name):
            if word == term.text:
                return EXACT, "name", [(start, start + len(word))]
        return best

    for field_name in record.DATE_FIELDS if term.dates else ():
        value = getattr(record, field_name)
        if value:
            score = _date_score(term.dates, value) * _weight(record, field_name)
            if score > best[0]:
                best = (score, field_name, [(0, len(value))])

    if term.number:
        id_score, id_spans = _id_score(term.text, record.id)
        if id_score > best[0]:
            best = (id_score, "id", id_spans)

    if term.words:
        name_scores, other_scores = term.name_scores, term.other_scores
        for field_name, in_name, weight, bonus, start, word in record.search_words:
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
            spans = _word_starts(term.text, _words(record.name))
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


def _id_score(term: str, record_id: str) -> tuple[int, list[tuple[int, int]]]:
    significant = term.lstrip("0")
    id_significant = record_id.lstrip("0")
    zeros = len(record_id) - len(id_significant)
    if significant and significant == id_significant:
        return SAME_ID, [(0, len(record_id))]
    if len(term) >= MIN_LOOSE_LENGTH:
        if record_id.startswith(term):
            return ID_PREFIX, [(0, len(term))]
        if significant == term and id_significant.startswith(term):
            return ID_PREFIX, [(zeros, zeros + len(term))]
        index = record_id.find(term)
        if index >= 0:
            return ID_INSIDE, [(index, index + len(term))]
    return 0, []
