import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    ollama_host: str = "http://localhost:11434"
    model: str = "llama3.1:8b"
    temperature: float = 0.2
    database_url: str = "sqlite:///aisew.db"
    max_parallel: int = 3
    num_ctx: int = 8192
    workspace_dir: str = "workspaces"
    background: bool = True
    max_tokens: int = 6000
    timeout_seconds: float = 900.0
    sandbox: str = "docker"
    fix_rounds: int = 2
    test_timeout: int = 120

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            ollama_host=os.getenv("OLLAMA_HOST", cls.ollama_host),
            model=os.getenv("AISEW_MODEL", cls.model),
            temperature=float(os.getenv("AISEW_TEMPERATURE", cls.temperature)),
            database_url=os.getenv("AISEW_DATABASE_URL", cls.database_url),
            max_parallel=int(os.getenv("AISEW_MAX_PARALLEL", cls.max_parallel)),
            num_ctx=int(os.getenv("AISEW_NUM_CTX", cls.num_ctx)),
            workspace_dir=os.getenv("AISEW_WORKSPACE_DIR", cls.workspace_dir),
            background=os.getenv("AISEW_BACKGROUND", "1").lower() not in ("0", "false", "no"),
            max_tokens=int(os.getenv("AISEW_MAX_TOKENS", cls.max_tokens)),
            timeout_seconds=float(os.getenv("AISEW_TIMEOUT", cls.timeout_seconds)),
            sandbox=os.getenv("AISEW_SANDBOX", cls.sandbox).lower(),
            fix_rounds=int(os.getenv("AISEW_FIX_ROUNDS", cls.fix_rounds)),
            test_timeout=int(os.getenv("AISEW_TEST_TIMEOUT", cls.test_timeout)),
        )
