import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import create_access_token
from app.core.settings import settings
from app.main import app
from app.services import employee_directory
from app.services.employee_directory import (
    Employee,
    build_employees,
    load_employees,
    search,
)
from app.services.fuzzy_search import fold
from app.services.sync_service import parse_ahgora_locations


@pytest.fixture(autouse=True)
def empty_caches():
    employee_directory._fiorilli_cache.clear()
    employee_directory._employees_cache.clear()
    yield
    employee_directory._fiorilli_cache.clear()
    employee_directory._employees_cache.clear()


def _search(query, *employees, include_dismissed=False):
    matches = search(list(employees), query, include_dismissed=include_dismissed)
    return [match.record.name for match in matches]


# --- search ----------------------------------------------------------------------------


def test_fold_drops_accents_and_keeps_positions():
    assert fold("JOÃO CONCEIÇÃO") == "joao conceicao"
    assert len(fold("ÁÉÍÕÜ Çç")) == len("ÁÉÍÕÜ Çç")


def test_empty_query_lists_the_active_by_name():
    employees = [
        Employee("000002", "BIA"),
        Employee("000003", "CARLA", dismissal_date="01/01/2020"),
        Employee("000001", "ANA"),
    ]
    assert _search("", *employees) == ["ANA", "BIA"]
    assert _search("", *employees, include_dismissed=True) == ["ANA", "BIA", "CARLA"]


def test_dismissed_are_searched_only_when_asked():
    carla = Employee("000003", "CARLA SOUZA", dismissal_date="10/10/2020")
    assert _search("carla", carla) == []
    assert _search("carla", carla, include_dismissed=True) == ["CARLA SOUZA"]
    # Also by the dismissal date
    assert _search("2020", carla, include_dismissed=True) == ["CARLA SOUZA"]


@pytest.mark.parametrize("query", ["joao", "JOÃO", "silva joao", "joão da silva"])
def test_ignores_accents_case_and_order(query):
    assert _search(query, Employee("1", "JOÃO DA SILVA")) == ["JOÃO DA SILVA"]


def test_every_term_must_match():
    assert _search("maria pereira", Employee("1", "MARIA DA SILVA")) == []


def test_particles_are_optional():
    assert _search("maria da silva", Employee("1", "MARIA SILVA")) == ["MARIA SILVA"]


@pytest.mark.parametrize(
    "query, fields",
    [
        ("prof", {"position": "PROFESSOR"}),
        ("coleta", {"department": "SERV.URB-COLETA LIXO"}),
        ("serv.urb-coleta", {"department": "SERV.URB-COLETA LIXO"}),
        ("rural", {"location": "ESCOLA A, TRANSPORTE-ZONA RURAL"}),
        ("12x36", {"scale": "12X36"}),
        ("efetivo", {"binding": "EFETIVO"}),
        ("saude", {"cost_center": "FUNDO MUNICIPAL DE SAÚDE"}),
        ("1980", {"birth_date": "05/03/1980"}),
        ("2010", {"admission_date": "01/02/2010"}),
    ],
)
def test_searches_every_field(query, fields):
    assert _search(query, Employee("000001", "FULANO", **fields)) == ["FULANO"]
    assert _search(query, Employee("000001", "FULANO")) == []


def test_mixes_fields_in_one_query():
    employees = [
        Employee("1", "MARIA DA SILVA", position="PROFESSOR", birth_date="05/03/1980"),
        Employee("2", "MARIA SOUZA", position="MOTORISTA", birth_date="05/03/1980"),
        Employee("3", "MARIA LIMA", position="PROFESSOR", birth_date="05/03/1981"),
        Employee("4", "JOSE COSTA", position="PROFESSOR", birth_date="05/03/1980"),
    ]
    assert _search("professor maria 1980", *employees) == ["MARIA DA SILVA"]


@pytest.mark.parametrize(
    "query",
    [
        "mar",  # start of a word
        "lva",  # inside a word
        "mra",  # letters in order inside a word
        "slva",  # a letter missing
        "silvz",  # a letter changed
        "silvva",  # an extra letter
        "sivla",  # two letters swapped
        "mdss",  # initials
        "mariasilva",  # no space
    ],
)
def test_fuzzy_name(query):
    maria = Employee("1", "MARIA DA SILVA SANTOS")
    assert _search(query, maria) == ["MARIA DA SILVA SANTOS"]


def test_short_terms_only_match_word_starts():
    maria = Employee("1", "MARIA DA SILVA")
    assert _search("si", maria) == ["MARIA DA SILVA"]
    assert _search("lv", maria) == []


def test_closer_matches_rank_first():
    employees = [
        Employee("1", "MAIRA REIS"),  # one typo
        Employee("2", "MARCIA DIAS"),  # letters in order
        Employee("3", "ROSEMARIA LIMA"),  # inside a word
        Employee("4", "MARIANA COSTA"),  # start of a word
        Employee("5", "ANA MARIA SOUZA"),  # whole word
        Employee("6", "MARIA SOUZA"),  # whole word, and the first name
    ]
    assert _search("maria", *employees) == [
        "MARIA SOUZA",
        "ANA MARIA SOUZA",
        "MARIANA COSTA",
        "ROSEMARIA LIMA",
        "MARCIA DIAS",
        "MAIRA REIS",
    ]


def test_ties_go_to_the_name_the_search_covers_best():
    longer = Employee("1", "MARIA APARECIDA SILVA SANTOS")
    exact = Employee("2", "MARIA SILVA SANTOS")
    assert _search("maria silva santos", longer, exact) == [
        "MARIA SILVA SANTOS",
        "MARIA APARECIDA SILVA SANTOS",
    ]


def test_ties_outside_the_name_stay_alphabetical():
    employees = [
        Employee("1", "BEATRIZ LIMA", position="PROFESSOR"),
        Employee("2", "ANA MARIA DE SOUZA", position="PROFESSOR"),
    ]
    assert _search("professor", *employees) == ["ANA MARIA DE SOUZA", "BEATRIZ LIMA"]


@pytest.mark.parametrize(
    "query",
    [
        "mdsa",  # initials
        "msa",  # initials skipping a word
        "mariasilva",  # words typed without spaces
        "marsil",  # beginnings of words
        "masialves",
    ],
)
def test_beginnings_of_words_in_order(query):
    maria = Employee("1", "MARIA DA SILVA ALVES")
    assert _search(query, maria) == ["MARIA DA SILVA ALVES"]


def test_letters_spread_inside_the_words_do_not_match():
    # m, d and s are in AMANDA DOS SANTOS, but not at the start of its words
    assert _search("mds", Employee("1", "AMANDA DOS SANTOS")) == []
    assert _search("ana", Employee("2", "MARCOS ANTONIO RIBEIRO")) == []
    assert _search("silvamaria", Employee("3", "MARIA DA SILVA")) == []  # out of order
    assert _search("ms", Employee("4", "MARIA SILVA")) == []  # too short


def test_name_ranks_above_other_fields():
    in_department = Employee("1", "ANA LIMA", department="SANTOS DUMONT")
    in_name = Employee("2", "JOSE SANTOS")
    assert _search("santos", in_department, in_name) == ["JOSE SANTOS", "ANA LIMA"]


@pytest.mark.parametrize(
    "query",
    [
        "05/03/1980",
        "5/3/1980",
        "5/3/80",
        "05.03.1980",
        "05-03-1980",
        "05031980",
        "050380",
        "1980-03-05",
        "19800305",
        "05/03",
        "5/3",
        "0503",
        "03/1980",
        "3/80",
        "1980",
    ],
)
def test_birth_date_formats(query):
    assert _search(query, Employee("1", "FULANO", birth_date="05/03/1980")) == [
        "FULANO"
    ]


@pytest.mark.parametrize(
    "query", ["06/03/1980", "05/04", "04/1980", "1981", "5/3/81", "99/99"]
)
def test_other_dates_do_not_match(query):
    assert _search(query, Employee("1", "FULANO", birth_date="05/03/1980")) == []


def test_numbers_are_not_typos_of_other_numbers():
    schedule = Employee("1", "FULANO", scale="180,1990,1981")
    assert _search("1980", schedule) == []
    assert _search("1990", schedule) == ["FULANO"]


def test_swapped_day_and_month_rank_below_the_exact_date():
    swapped = Employee("1", "BIA", birth_date="05/03/1980")
    exact = Employee("2", "ANA", birth_date="03/05/1980")
    assert _search("03/05/1980", swapped, exact) == ["ANA", "BIA"]


def test_birth_date_ranks_above_admission_date():
    admitted = Employee("1", "BIA", admission_date="05/03/1980")
    born = Employee("2", "ANA", birth_date="05/03/1980")
    assert _search("05/03/1980", admitted, born) == ["ANA", "BIA"]


@pytest.mark.parametrize(
    "query, found",
    [("4930", True), ("004930", True), ("493", True), ("930", True), ("49", False)],
)
def test_matricula(query, found):
    assert _search(query, Employee("004930", "FULANO")) == (["FULANO"] if found else [])


def test_segments_mark_what_matched():
    employee = Employee("004930", "JOÃO DA SILVA", position="PROFESSOR")
    match = search([employee], "joao prof 4930")[0]
    assert match.segments("name") == [("JOÃO", True), (" DA SILVA", False)]
    assert match.segments("position") == [("PROF", True), ("ESSOR", False)]
    assert match.segments("id") == [("004930", True)]
    assert match.segments("department") == []

    initials = search([Employee("1", "MARIA DA SILVA")], "mds")[0]
    assert initials.segments("name") == [
        ("M", True),
        ("ARIA ", False),
        ("D", True),
        ("A ", False),
        ("S", True),
        ("ILVA", False),
    ]


# --- loading ---------------------------------------------------------------------------


def test_build_employees_joins_the_fiorilli_data():
    df = pd.DataFrame(
        [
            {
                "id": "004930",
                "name": "FULANO",
                "position": "PROFESSOR",
                "scale": None,
                "department": "EDUCACAO",
                "location": "['ESCOLA B', 'ESCOLA A']",
                "admission_date": datetime(2010, 2, 1),
                "dismissal_date": None,
            },
            {
                "id": "000002",
                "name": "BELTRANO",
                "position": None,
                "scale": "12X36",
                "department": None,
                "location": None,
                "admission_date": None,
                "dismissal_date": datetime(2020, 10, 10),
            },
        ]
    )
    fiorilli = {
        "004930": {
            "birth_date": "05/03/1980",
            "binding": "EFETIVO",
            "cost_center": "SAUDE",
        }
    }

    fulano, beltrano = build_employees(df, fiorilli)

    assert fulano == Employee(
        "004930",
        "FULANO",
        position="PROFESSOR",
        department="EDUCACAO",
        location="ESCOLA A, ESCOLA B",
        admission_date="01/02/2010",
        birth_date="05/03/1980",
        binding="EFETIVO",
        cost_center="SAUDE",
    )
    assert not fulano.dismissed
    assert beltrano == Employee(
        "000002", "BELTRANO", scale="12X36", dismissal_date="10/10/2020"
    )
    assert beltrano.dismissed


def test_build_employees_without_dates_or_with_dates_as_text():
    df = pd.DataFrame(
        [{"id": "1", "name": " ANA ", "admission_date": None, "dismissal_date": None}]
    )
    assert build_employees(df, {}) == [Employee("000001", "ANA")]

    df = pd.DataFrame([{"id": "1", "name": "ANA", "dismissal_date": "10/10/2020"}])
    assert build_employees(df, {})[0].dismissal_date == "10/10/2020"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("['ESCOLA B', 'ESCOLA A']", ["ESCOLA A", "ESCOLA B"]),
        ("escola b; Escola A", ["ESCOLA A", "ESCOLA B"]),
        ("ESCOLA A", ["ESCOLA A"]),
        ("", []),
        (None, []),
        (float("nan"), []),
    ],
)
def test_parse_ahgora_locations(value, expected):
    assert parse_ahgora_locations(value) == expected


def _service(employees_df, fiorilli_df=None):
    service = MagicMock()
    service.repo.get_ahgora_employees_version = AsyncMock(return_value=(1, None))
    service.repo.get_ahgora_employees_df = AsyncMock(return_value=employees_df)
    service._read_csv = MagicMock(return_value=fiorilli_df)
    return service


@pytest.mark.asyncio
async def test_load_employees_parses_the_fiorilli_file_once_per_download(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    path = tmp_path / "fiorilli_employees.txt"
    path.write_text("")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    service = _service(
        pd.DataFrame([{"id": "004930", "name": "FULANO"}]),
        pd.DataFrame(
            [{"id": "004930", "birth_date": "05/03/1980", "binding": "EFETIVO"}]
        ),
    )

    employees, updated_at = await load_employees(service)
    await load_employees(service)

    assert employees[0].birth_date == "05/03/1980"
    assert employees[0].binding == "EFETIVO"
    assert employees[0].cost_center is None
    assert updated_at == datetime.fromtimestamp(1_700_000_000)
    service._read_csv.assert_called_once_with(path)

    os.utime(path, (1_700_000_100, 1_700_000_100))  # a sync downloaded a new file
    await load_employees(service)
    assert service._read_csv.call_count == 2


@pytest.mark.asyncio
async def test_load_employees_without_the_fiorilli_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    service = _service(pd.DataFrame([{"id": "004930", "name": "FULANO"}]))

    employees, updated_at = await load_employees(service)

    assert employees == [Employee("004930", "FULANO")]
    assert updated_at is None
    service._read_csv.assert_not_called()


@pytest.mark.asyncio
async def test_load_employees_reuses_the_list_until_an_employee_is_saved(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    service = _service(pd.DataFrame([{"id": "004930", "name": "FULANO"}]))

    first, _ = await load_employees(service)
    again, _ = await load_employees(service)
    assert again is first
    service.repo.get_ahgora_employees_df.assert_called_once()

    # Saving an employee sets its last_synced_at
    service.repo.get_ahgora_employees_version.return_value = (1, datetime(2026, 1, 1))
    service.repo.get_ahgora_employees_df.return_value = pd.DataFrame(
        [{"id": "004930", "name": "FULANO DE TAL"}]
    )
    updated, _ = await load_employees(service)
    assert updated[0].name == "FULANO DE TAL"


@pytest.mark.asyncio
async def test_load_employees_without_employees(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    employees, _ = await load_employees(_service(pd.DataFrame()))
    assert employees == []


# --- pages -----------------------------------------------------------------------------


@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: MagicMock()
    with TestClient(app) as c:
        c.cookies.set(
            "access_token", create_access_token({"sub": "testuser", "is_admin": False})
        )
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def web_service(tmp_path, monkeypatch):
    """The SyncService of the web routes; there is no Fiorilli file."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    with patch("app.infrastructure.web.routes.SyncService") as service_class:
        service = service_class.return_value
        service.repo.get_ahgora_employees_version = AsyncMock(return_value=(0, None))
        yield service


def _employees_df(*rows):
    return pd.DataFrame(
        [
            {"id": employee_id, "name": name, "dismissal_date": dismissed}
            for employee_id, name, dismissed in rows
        ]
    )


@pytest.mark.parametrize("path", ["/employees", "/partials/employees"])
def test_employee_pages_require_login(path, client):
    client.cookies.clear()
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_employees_page_shows_the_dismissed_only_when_asked(client, web_service):
    web_service.repo.get_ahgora_employees_df = AsyncMock(
        return_value=_employees_df(
            ("000001", "ANA ATIVA", None),
            ("000002", "BIA DESLIGADA", datetime(2020, 10, 10)),
        )
    )

    html = client.get("/employees").text
    assert "ANA ATIVA" in html
    assert "BIA DESLIGADA" not in html
    assert "Arquivo do Fiorilli não encontrado" in html

    html = client.get("/employees?dismissed=on").text
    assert "ANA ATIVA" in html
    assert "BIA DESLIGADA" in html
    assert "10/10/2020" in html


def test_employees_partial_searches_and_keeps_the_search_in_the_url(
    client, web_service
):
    web_service.repo.get_ahgora_employees_df = AsyncMock(
        return_value=_employees_df(
            ("000001", "ANA SOUZA", None), ("000002", "BIA LIMA", None)
        )
    )

    response = client.get("/partials/employees?q=souza&dismissed=on")

    assert response.status_code == 200
    assert '<mark class="bg-amber-200/70 text-inherit rounded-sm">SOUZA</mark>' in (
        response.text
    )
    assert "BIA LIMA" not in response.text
    assert response.headers["HX-Replace-Url"] == "/employees?q=souza&dismissed=on"
    assert client.get("/partials/employees").headers["HX-Replace-Url"] == "/employees"


def test_employees_partial_loads_the_list_in_pages(client, web_service):
    web_service.repo.get_ahgora_employees_df = AsyncMock(
        return_value=_employees_df(
            *((f"{i:06d}", f"FUNCIONARIO {i:03d}", None) for i in range(150))
        )
    )

    first = client.get("/partials/employees").text
    assert first.count("data-employee") == 100
    assert 'hx-get="/partials/employees?offset=100"' in first

    rest = client.get("/partials/employees?offset=100")
    assert rest.text.count("data-employee") == 50
    assert "hx-get" not in rest.text
    assert "HX-Replace-Url" not in rest.headers


def test_dashboard_card_opens_the_employees(client, web_service):
    web_service.list_jobs = AsyncMock(return_value=[])
    web_service.repo.get_ahgora_employees_df = AsyncMock(return_value=pd.DataFrame())
    web_service.repo.get_ahgora_leaves_df = AsyncMock(return_value=pd.DataFrame())
    web_service.repo.get_usernames = AsyncMock(return_value={})

    html = client.get("/").text

    # The card and the menu
    assert html.count('href="/employees"') == 2
