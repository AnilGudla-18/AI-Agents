from .base import LLMResponse


class FakeLLM:
    """Scripted replies for tests (no Ollama needed).

    FakeLLM([r1, r2])                  -> replies are returned in call order
    FakeLLM(routes={"Business Analyst": [r1, r2], "QA Engineer": [r3]})
                                       -> reply chosen by the agent profile (first
                                          paragraph of the system prompt), so tests
                                          do not depend on the order of parallel calls
    """

    def __init__(self, responses: list[str] | None = None, routes: dict[str, list[str]] | None = None):
        self._responses = list(responses or [])
        self._routes = {k: list(v) for k, v in (routes or {}).items()}
        self.calls: list[dict] = []

    async def generate(self, *, system: str, user: str, schema: dict | None) -> LLMResponse:
        agent = None
        if self._routes:
            head = system.split("\n\n")[0]
            agent = next((k for k in self._routes if k in head), None)
            if agent is None:
                raise RuntimeError(f"FakeLLM has no route for: {head[:80]}")
            queue = self._routes[agent]
        else:
            queue = self._responses
        self.calls.append({"system": system, "user": user, "schema": schema, "agent": agent})
        if not queue:
            raise RuntimeError("FakeLLM ran out of scripted responses")
        return LLMResponse(text=queue.pop(0), model="fake")
