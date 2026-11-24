from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class LLMClient(Protocol):
    async def generate(self, *, system: str, user: str, schema: dict | None) -> LLMResponse: ...
