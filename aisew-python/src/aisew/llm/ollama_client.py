import time

import ollama

from ..config import Settings
from .base import LLMResponse


class OllamaClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        self._client = ollama.AsyncClient(host=self.settings.ollama_host, timeout=self.settings.timeout_seconds)

    async def generate(self, *, system: str, user: str, schema: dict | None) -> LLMResponse:
        start = time.perf_counter()
        resp = await self._client.chat(
            model=self.settings.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format=schema,  # a JSON schema constrains the answer; None means free text (used for code files)
            options={
                "temperature": self.settings.temperature,
                "num_ctx": self.settings.num_ctx,
                "num_predict": self.settings.max_tokens,  # a runaway answer is cut off, then retried
            },
        )
        return LLMResponse(
            text=resp["message"]["content"],
            model=self.settings.model,
            prompt_tokens=resp.get("prompt_eval_count") or 0,
            completion_tokens=resp.get("eval_count") or 0,
            latency_ms=int((time.perf_counter() - start) * 1000),
        )
