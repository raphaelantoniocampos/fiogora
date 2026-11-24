from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.security import create_access_token
from app.core.settings import settings
from app.main import app
from app.services import employee_directory, leave_directory
from app.services.employee_directory import Employee
from app.services.leave_directory import (
    Leave,
    LeaveStatus,
    build_leaves,
    load_leaves,
    search,
)

TODAY = date(2026, 11, 24)


@pytest.fixture(autouse=True)
def empty_caches():
    caches = (
        employee_directory._fiorilli_cache,
        employee_directory._employees_cache,
        leave_directory._leaves_cache,
    )
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


def _leave(name, start, end=None, cod="164", cod_name="LICENCA MEDICA", **fields):
    return Leave(
        fields.pop("id", "000001"),
        cod,
        start,
        end,
        cod_name=cod_name,
        name=name,
        **fields,
    )


def _search(query, *leaves, include_ended=False):
    matches = search(list(leaves), query, TODAY, include_ended=include_ended)
    return [(match.record.name, match.record.start_date) for match in matches]


# --- leaves ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start, end, status",
    [
        (date(2026, 11, 1), date(2026, 11, 30), LeaveStatus.CURRENT),
        (date(2026, 11, 24), date(2026, 11, 24), LeaveStatus.CURRENT),
        (date(2026, 11, 1), None, LeaveStatus.CURRENT),
        (date(2026, 11, 25), date(2026, 12, 1), LeaveStatus.UPCOMING),
        (date(2026, 10, 1), date(2026, 11, 23), LeaveStatus.ENDED),
    ],
)
def test_status(start, end, status):
    assert _leave("ANA", start, end).status(TODAY) == status


def test_dates_are_formatted_for_the_search():
    leave = _leave("ANA", date(2026, 3, 5), date(2026, 4, 1))
    assert (leave.start_date, leave.end_date) == ("05/03/2026", "01/04/2026")
    assert _leave("ANA", date(2026, 3, 5)).end_date is None


def test_empty_query_lists_current_then_upcoming_latest_first():
    leaves = [
        _leave("ENCERRADA", date(2026, 1, 1), date(2026, 1, 10)),
        _leave("AGENDADA", date(2026, 12, 1), date(2026, 12, 10)),
        _leave("ANTIGA", date(2026, 10, 1), None),
        _leave("RECENTE", date(2026, 11, 20), date(2026, 11, 30)),
    ]
    assert [name for name, _ in _search("", *leaves)] == [
        "RECENTE",
        "ANTIGA",
        "AGENDADA",
    ]
    assert [name for name, _ in _search("", *leaves, include_ended=True)][-1] == (
        "ENCERRADA"
    )


def test_ended_are_searched_only_when_asked():
    ended = _leave("CARLA SOUZA", date(2026, 1, 1), date(2026, 1, 10))
    assert _search("carla", ended) == []
    assert _search("carla", ended, include_ended=True) == [
        ("CARLA SOUZA", "01/01/2026")
    ]


@pytest.mark.parametrize(
    "query",
    ["maria", "medica", "licenca medica", "164", "004930", "4930", "saude", "medico"],
)
def test_searches_every_field(query):
    leave = _leave(
        "MARIA DA SILVA",
        date(2026, 11, 1),
        id="004930",
        position="MEDICO",
        department="SECRETARIA DE SAUDE",
    )
    assert _search(query, leave) == [("MARIA DA SILVA", "01/11/2026")]


@pytest.mark.parametrize("query", ["01/11/2026", "1/11/26", "0111", "11/2026", "30/11"])
def test_start_and_end_dates(query):
    leave = _leave("ANA", date(2026, 11, 1), date(2026, 11, 30))
    assert _search(query, leave) == [("ANA", "01/11/2026")]


def test_mixes_fields_in_one_query():
    leaves = [
        _leave("ANA", date(2026, 11, 1), cod="164", cod_name="LICENCA MEDICA"),
        _leave("ANA", date(2026, 11, 10), cod="279", cod_name="FALTA ABONADA"),
        _leave("BIA", date(2026, 11, 5), cod="279", cod_name="FALTA ABONADA"),
    ]
    assert _search("ana falta", *leaves) == [("ANA", "10/11/2026")]


def test_ties_go_to_current_then_latest():
    leaves = [
        _leave("ANA", date(2026, 11, 1)),
        _leave("ANA", date(2026, 12, 1)),
        _leave("ANA", date(2026, 11, 20)),
    ]
    assert [start for _, start in _search("ana", *leaves)] == [
        "20/11/2026",
        "01/11/2026",
        "01/12/2026",
    ]


def test_segments_mark_what_matched():
    leave = _leave("ANA", date(2026, 11, 1), cod_name="LICENÇA MÉDICA")
    (match,) = search([leave], "medica", TODAY)
    assert match.segments("cod_name") == [("LICENÇA ", False), ("MÉDICA", True)]


def test_build_leaves_joins_the_employees():
    leaves_df = pd.DataFrame(
        [
            {
                "id": "4930",
                "cod": "164",
                "cod_name": "LICENCA MEDICA",
                "start_date": datetime(2026, 11, 1),
                "end_date": datetime(2026, 11, 30),
                "duration": 30,
            },
            {
                "id": "000007",
                "cod": "279",
                "cod_name": None,
                "start_date": datetime(2026, 11, 5),
                "end_date": None,
                "duration": None,
            },
        ]
    )
    employees = [
        Employee("004930", "MARIA", position="MEDICO", department="SAUDE"),
    ]

    maria, unknown = build_leaves(leaves_df, employees)

    assert maria == Leave(
        "004930",
        "164",
        date(2026, 11, 1),
        date(2026, 11, 30),
        cod_name="LICENCA MEDICA",
        duration=30,
        name="MARIA",
        position="MEDICO",
        department="SAUDE",
    )
    assert unknown == Leave("000007", "279", date(2026, 11, 5))


def test_build_leaves_works_out_the_duration_and_reads_dates_as_text():
    leaves_df = pd.DataFrame(
        [
            {
                "id": "1",
                "cod": "1",
                "start_date": "05/03/2026",
                "end_date": "07/03/2026",
            },
            {"id": "2", "cod": "1", "start_date": None, "end_date": "07/03/2026"},
        ]
    )
    (leave,) = build_leaves(leaves_df, [])
    assert (leave.start, leave.end, leave.duration) == (
        date(2026, 3, 5),
        date(2026, 3, 7),
        3,
    )
    assert build_leaves(pd.DataFrame(), []) == []


def _service(employees_df, leaves_df):
    service = MagicMock()
    service.repo.get_ahgora_employees_version = AsyncMock(return_value=(1, None))
    service.repo.get_ahgora_employees_df = AsyncMock(return_value=employees_df)
    service.repo.get_ahgora_leaves_version = AsyncMock(return_value=(1, None))
    service.repo.get_ahgora_leaves_df = AsyncMock(return_value=leaves_df)
    return service


def _leaves_df(*rows):
    return pd.DataFrame(
        [
            {"id": employee_id, "cod": "164", "start_date": start, "end_date": end}
            for employee_id, start, end in rows
        ]
    )


@pytest.mark.asyncio
async def test_load_leaves_reuses_the_list_until_a_leave_is_saved(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    service = _service(
        pd.DataFrame([{"id": "004930", "name": "FULANO"}]),
        _leaves_df(("004930", datetime(2026, 11, 1), None)),
    )

    first = await load_leaves(service)
    assert await load_leaves(service) is first
    assert first[0].name == "FULANO"
    service.repo.get_ahgora_leaves_df.assert_called_once()

    service.repo.get_ahgora_leaves_version.return_value = (2, datetime(2026, 11, 24))
    service.repo.get_ahgora_leaves_df.return_value = _leaves_df(
        ("004930", datetime(2026, 11, 1), None),
        ("004930", datetime(2026, 11, 20), None),
    )
    assert len(await load_leaves(service)) == 2


@pytest.mark.asyncio
async def test_load_leaves_follows_the_employees(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    service = _service(
        pd.DataFrame([{"id": "004930", "name": "FULANO"}]),
        _leaves_df(("004930", datetime(2026, 11, 1), None)),
    )
    await load_leaves(service)

    # Saving an employee sets its last_synced_at
    service.repo.get_ahgora_employees_version.return_value = (1, datetime(2026, 1, 1))
    service.repo.get_ahgora_employees_df.return_value = pd.DataFrame(
        [{"id": "004930", "name": "FULANO DE TAL"}]
    )
    assert (await load_leaves(service))[0].name == "FULANO DE TAL"


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
    """The SyncService of the web routes, with two employees and no Fiorilli file."""
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    with (
        patch("app.infrastructure.web.routes.SyncService") as service_class,
        patch("app.infrastructure.web.routes.date") as date_class,
    ):
        date_class.today.return_value = TODAY
        service = service_class.return_value
        service.repo.get_ahgora_employees_version = AsyncMock(return_value=(0, None))
        service.repo.get_ahgora_employees_df = AsyncMock(
            return_value=pd.DataFrame(
                [
                    {"id": "000001", "name": "ANA SOUZA"},
                    {"id": "000002", "name": "BIA LIMA"},
                ]
            )
        )
        service.repo.get_ahgora_leaves_version = AsyncMock(return_value=(0, None))
        yield service


@pytest.mark.parametrize("path", ["/leaves", "/partials/leaves"])
def test_leave_pages_require_login(path, client):
    client.cookies.clear()
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_leaves_page_shows_the_ended_only_when_asked(client, web_service):
    web_service.repo.get_ahgora_leaves_df = AsyncMock(
        return_value=_leaves_df(
            ("000001", datetime(2026, 11, 1), datetime(2026, 11, 30)),
            ("000002", datetime(2026, 10, 1), datetime(2026, 10, 10)),
        )
    )

    html = client.get("/leaves").text
    assert "ANA SOUZA" in html
    assert "Em\n            andamento" in html
    assert "BIA LIMA" not in html
    assert "Situação em 24/11/2026" in html

    html = client.get("/leaves?ended=on").text
    assert "ANA SOUZA" in html
    assert "BIA LIMA" in html
    assert "10/10/2026" in html
    assert "Encerrado" in html


def test_leaves_partial_searches_and_keeps_the_search_in_the_url(client, web_service):
    web_service.repo.get_ahgora_leaves_df = AsyncMock(
        return_value=_leaves_df(
            ("000001", datetime(2026, 11, 1), None),
            ("000002", datetime(2026, 11, 5), None),
        )
    )

    response = client.get("/partials/leaves?q=souza&ended=on")

    assert response.status_code == 200
    assert '<mark class="bg-amber-200/70 text-inherit rounded-sm">SOUZA</mark>' in (
        response.text
    )
    assert "BIA LIMA" not in response.text
    assert response.headers["HX-Replace-Url"] == "/leaves?q=souza&ended=on"
    assert client.get("/partials/leaves").headers["HX-Replace-Url"] == "/leaves"


def test_leaves_partial_loads_the_list_in_pages(client, web_service):
    web_service.repo.get_ahgora_leaves_df = AsyncMock(
        return_value=_leaves_df(
            *(("000001", datetime(2026, 11, 1 + i % 20), None) for i in range(150))
        )
    )

    first = client.get("/partials/leaves").text
    assert first.count("data-leave") == 100
    assert 'hx-get="/partials/leaves?offset=100"' in first

    rest = client.get("/partials/leaves?offset=100")
    assert rest.text.count("data-leave") == 50
    assert "hx-get" not in rest.text
    assert "HX-Replace-Url" not in rest.headers


def test_dashboard_card_opens_the_leaves(client, web_service):
    web_service.list_jobs = AsyncMock(return_value=[])
    web_service.repo.get_ahgora_employees_df = AsyncMock(return_value=pd.DataFrame())
    web_service.repo.get_ahgora_leaves_df = AsyncMock(return_value=pd.DataFrame())
    web_service.repo.get_usernames = AsyncMock(return_value={})

    html = client.get("/").text

    # The card and the menu
    assert html.count('href="/leaves"') == 2
