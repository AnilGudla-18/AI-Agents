import asyncio
import sys

from .agents.ba import BusinessAnalystAgent
from .llm.ollama_client import OllamaClient


async def _main(requirement: str) -> None:
    agent = BusinessAnalystAgent(OllamaClient())
    result = await agent.run({"requirement": requirement})
    print(result.output.model_dump_json(indent=2))
    tokens = sum(u.prompt_tokens + u.completion_tokens for u in result.usage)
    print(f"\n[attempts={result.attempts} tokens={tokens}]", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Usage: python -m aisew.cli "your requirement here"')
    asyncio.run(_main(" ".join(sys.argv[1:])))


if __name__ == "__main__":
    main()
