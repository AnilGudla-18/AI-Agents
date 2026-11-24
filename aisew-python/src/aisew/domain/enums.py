from enum import Enum


class Readiness(str, Enum):
    READY = "Ready"
    READY_WITH_CLARIFICATIONS = "ReadyWithClarifications"
    NOT_READY = "NotReady"
