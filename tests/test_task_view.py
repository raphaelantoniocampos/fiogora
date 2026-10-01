from uuid import uuid4

from app.domain.entities import AutomationTask
from app.domain.enums import AutomationTaskStatus, AutomationTaskType
from app.infrastructure.web.task_view import (
    task_changes,
    task_fields,
    task_name,
    task_status,
    task_summary,
)


def _task(type_, payload, status=AutomationTaskStatus.PENDING):
    return AutomationTask(job_id=uuid4(), type=type_, status=status, payload=payload)


def test_update_changes_follow_the_normalized_comparison():
    task = _task(
        AutomationTaskType.UPDATE_EMPLOYEE,
        {
            # Same after normalization (accents/case): the robot does not touch it
            "name_actual": "JOSÉ DA SILVA",
            "name_expected": "JOSE DA SILVA",
            "name_actual_norm": "jose da silva",
            "name_expected_norm": "jose da silva",
            "department_actual": "TRANSPORTE-ZONA RURAL",
            "department_expected": "SERV.URB-COLETA LIXO",
            "department_actual_norm": "transporte-zona rural",
            "department_expected_norm": "serv.urb-coleta lixo",
            # Older payload without *_norm: raw comparison
            "position_actual": None,
            "position_expected": "MOTORISTA",
        },
    )

    assert task_changes(task) == [
        {"label": "Cargo", "before": "—", "after": "MOTORISTA"},
        {
            "label": "Departamento",
            "before": "TRANSPORTE-ZONA RURAL",
            "after": "SERV.URB-COLETA LIXO",
        },
    ]


def test_only_updates_have_changes():
    task = _task(AutomationTaskType.ADD_EMPLOYEE, {"position": "MOTORISTA"})
    assert task_changes(task) == []


def test_fields_are_labeled_formatted_and_use_new_values():
    task = _task(
        AutomationTaskType.UPDATE_EMPLOYEE,
        {
            "id": "004930",
            "name": "OLD",
            "name_expected": "NOVO NOME",
            "cpf": "8599174606",
            "sex": "F",
            "scale": None,
        },
    )

    assert task_fields(task) == [
        ("Matrícula", "004930"),
        ("Nome", "NOVO NOME"),
        ("CPF", "085.991.746-06"),
        ("Sexo", "Feminino"),
    ]
    assert task_name(task) == "NOVO NOME"


def test_summary_per_type():
    add = _task(
        AutomationTaskType.ADD_EMPLOYEE,
        {
            "position": "MOTORISTA",
            "department": "TRANSPORTE",
            "admission_date": "09/09/2026",
        },
    )
    remove = _task(
        AutomationTaskType.REMOVE_EMPLOYEE,
        {"position": "PROFESSOR", "department": "CEIM", "dismissal_date": "21/09/2026"},
    )
    leaves = _task(AutomationTaskType.ADD_LEAVE, {"leaves": [{}, {}]})

    assert task_summary(add) == "MOTORISTA · TRANSPORTE · Admissão 09/09/2026"
    assert task_summary(remove) == "PROFESSOR · CEIM · Desligamento 21/09/2026"
    assert task_summary(leaves) == "2 afastamentos para importar"


def test_status_label_in_portuguese():
    task = _task(AutomationTaskType.ADD_EMPLOYEE, {}, AutomationTaskStatus.FAILED)
    assert task_status(task)["label"] == "Falhou"
