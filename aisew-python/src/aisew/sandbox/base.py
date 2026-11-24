from dataclasses import dataclass
from typing import Protocol


@dataclass
class SandboxResult:
    ran: bool                 # False: the tests could not be run at all (see reason)
    passed: bool | None = None
    output: str = ""
    reason: str = ""


class Sandbox(Protocol):
    async def run_tests(self, files: list[tuple[str, str]]) -> SandboxResult: ...
    async def status(self) -> dict: ...
