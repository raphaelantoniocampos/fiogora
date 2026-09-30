from enum import StrEnum, auto


class SyncStatus(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    CANCELLED = auto()
    RETRYING = auto()


class AutomationTaskType(StrEnum):
    ADD_EMPLOYEE = auto()
    REMOVE_EMPLOYEE = auto()
    UPDATE_EMPLOYEE = auto()
    ADD_LEAVE = auto()

    @property
    def label(self) -> str:
        return {
            AutomationTaskType.ADD_EMPLOYEE: "Adicionar funcionário",
            AutomationTaskType.REMOVE_EMPLOYEE: "Remover funcionário",
            AutomationTaskType.UPDATE_EMPLOYEE: "Atualizar funcionário",
            AutomationTaskType.ADD_LEAVE: "Importar afastamentos",
        }[self]


class AutomationTaskStatus(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    FAILED = auto()
    CANCELLED = auto()
