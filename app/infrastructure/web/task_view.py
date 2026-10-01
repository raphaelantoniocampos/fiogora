"""How automation tasks are shown in the web UI: status, name, changes and fields of their payload."""

from typing import Any

from app.domain.entities import AutomationTask
from app.domain.enums import AutomationTaskStatus, AutomationTaskType

STATUS_LABELS = {
    AutomationTaskStatus.PENDING: "Pendente",
    AutomationTaskStatus.RUNNING: "Executando",
    AutomationTaskStatus.SUCCESS: "Concluída",
    AutomationTaskStatus.FAILED: "Falhou",
    AutomationTaskStatus.CANCELLED: "Cancelada",
}

STATUS_STYLES = {
    AutomationTaskStatus.PENDING: "bg-indigo-50 text-indigo-700 ring-indigo-200",
    AutomationTaskStatus.RUNNING: "bg-blue-50 text-blue-700 ring-blue-200 animate-pulse",
    AutomationTaskStatus.SUCCESS: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    AutomationTaskStatus.FAILED: "bg-rose-50 text-rose-700 ring-rose-200",
    AutomationTaskStatus.CANCELLED: "bg-slate-100 text-slate-600 ring-slate-200",
}

# Fields an update writes to Ahgora, compared the same way as AhgoraBrowser.update_employee
UPDATED_FIELDS = (
    ("name", "Nome"),
    ("position", "Cargo"),
    ("admission_date", "Admissão"),
    ("department", "Departamento"),
)

EMPLOYEE_FIELDS = (
    ("id", "Matrícula"),
    ("name", "Nome"),
    ("cpf", "CPF"),
    ("sex", "Sexo"),
    ("birth_date", "Nascimento"),
    ("pis_pasep", "PIS/PASEP"),
    ("position", "Cargo"),
    ("department", "Departamento"),
    ("location", "Localização"),
    ("cost_center", "Centro de custo"),
    ("binding", "Vínculo"),
    ("scale", "Escala"),
    ("admission_date", "Admissão"),
    ("dismissal_date", "Desligamento"),
)


def task_status(task: AutomationTask) -> dict[str, str]:
    return {
        "label": STATUS_LABELS.get(task.status, str(task.status)),
        "style": STATUS_STYLES.get(
            task.status, "bg-slate-100 text-slate-600 ring-slate-200"
        ),
    }


def task_name(task: AutomationTask) -> str:
    return task.payload.get("name_expected") or task.payload.get("name") or "—"


def task_changes(task: AutomationTask) -> list[dict[str, Any]]:
    """What an update changes in Ahgora: [{"label", "before", "after"}]."""
    if task.type != AutomationTaskType.UPDATE_EMPLOYEE:
        return []
    payload = task.payload
    changes = []
    for field, label in UPDATED_FIELDS:
        before = payload.get(f"{field}_actual")
        after = payload.get(f"{field}_expected")
        # Older payloads have no normalized values; compare the raw ones then
        if after and payload.get(f"{field}_expected_norm", after) != payload.get(
            f"{field}_actual_norm", before
        ):
            changes.append({"label": label, "before": before or "—", "after": after})
    return changes


def task_fields(task: AutomationTask) -> list[tuple[str, str]]:
    """Employee data of the task, labeled. For updates, the new values (from Fiorilli)."""
    payload = task.payload
    fields = []
    for key, label in EMPLOYEE_FIELDS:
        value = payload.get(f"{key}_expected", payload.get(key))
        if value in (None, ""):
            continue
        fields.append((label, _format_field(key, value)))
    return fields


def task_summary(task: AutomationTask) -> str:
    """One line under the employee name in the task list."""
    payload = task.payload
    if task.type == AutomationTaskType.ADD_LEAVE:
        count = len(payload.get("leaves", []))
        return f"{count} afastamento{'' if count == 1 else 's'} para importar"
    date_key, date_label = (
        ("dismissal_date", "Desligamento")
        if task.type == AutomationTaskType.REMOVE_EMPLOYEE
        else ("admission_date", "Admissão")
    )
    parts = [
        payload.get("position") or payload.get("position_expected"),
        payload.get("department") or payload.get("department_expected"),
    ]
    date = payload.get(date_key) or payload.get(f"{date_key}_expected")
    if date:
        parts.append(f"{date_label} {date}")
    return " · ".join(str(part) for part in parts if part)


def _format_field(key: str, value: Any) -> str:
    if key == "cpf":
        digits = "".join(ch for ch in str(value) if ch.isdigit()).zfill(11)
        if len(digits) == 11:
            return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    if key == "sex":
        return {"M": "Masculino", "F": "Feminino"}.get(str(value), str(value))
    return str(value)
