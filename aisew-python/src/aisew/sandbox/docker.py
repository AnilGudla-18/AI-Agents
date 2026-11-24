"""Runs generated tests inside a locked-down Docker container.

AI-written code is never run on your own machine. The container has no network, little memory,
no capabilities, a read-only root filesystem and a time limit. The code is copied into a temporary
folder (through the safe writer), never into your workspace.
"""
import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from ..workspace.writer import write_files
from .base import SandboxResult

IMAGE = "aisew-sandbox:1"
# Installed in the image once. The container has no network, so nothing else can be installed.
ALLOWED_PACKAGES = {"sqlalchemy", "fastapi", "pydantic", "pytest", "pytest-asyncio", "httpx", "anyio"}
DOCKERFILE = """FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install "sqlalchemy>=2" fastapi "pydantic>=2" pytest pytest-asyncio httpx anyio
RUN useradd --create-home --uid 10001 runner
USER runner
WORKDIR /work
"""
OUTPUT_CHARS = 3500


def unsupported_packages(requirements: str) -> list[str]:
    bad = []
    for line in requirements.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].strip().lower().replace("_", "-")
        if name and name not in ALLOWED_PACKAGES:
            bad.append(name)
    return bad


class DockerSandbox:
    def __init__(self, timeout: int = 120, run=subprocess.run, which=shutil.which):
        self.timeout = timeout
        self._run = run
        self._which = which
        self.building = False
        self.last_error = ""

    # ---- docker helpers (blocking; always called through a thread) ----
    def _docker(self, args: list[str], timeout: float):
        return self._run(["docker", *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)

    def docker_available(self) -> bool:
        if self._which("docker") is None:
            return False
        try:
            return self._docker(["info"], 20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def image_ready(self) -> bool:
        try:
            return self._docker(["image", "inspect", IMAGE], 20).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _build_blocking(self) -> str | None:
        """Build the sandbox image. Returns an error message, or None on success."""
        folder = tempfile.mkdtemp(prefix="aisew-image-")
        try:
            (Path(folder) / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
            result = self._docker(["build", "-t", IMAGE, folder], 900)
            if result.returncode != 0:
                return (result.stdout + result.stderr)[-800:] or "docker build failed"
            return None
        except subprocess.TimeoutExpired:
            return "docker build took longer than 15 minutes"
        except (OSError, subprocess.SubprocessError) as e:
            return f"{type(e).__name__}: {e}"
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # ---- public API ----
    async def build(self) -> dict:
        self.building = True
        try:
            if not await asyncio.to_thread(self.docker_available):
                self.last_error = "Docker is not installed or not running."
            else:
                self.last_error = await asyncio.to_thread(self._build_blocking) or ""
        finally:
            self.building = False
        return await self.status()

    async def status(self) -> dict:
        available = await asyncio.to_thread(self.docker_available)
        ready = await asyncio.to_thread(self.image_ready) if available else False
        return {"mode": "docker", "docker_available": available, "image": IMAGE,
                "image_ready": ready, "building": self.building, "last_error": self.last_error}

    async def run_tests(self, files: list[tuple[str, str]]) -> SandboxResult:
        return await asyncio.to_thread(self._run_tests_blocking, files)

    def _run_tests_blocking(self, files: list[tuple[str, str]]) -> SandboxResult:
        if not self.docker_available():
            return SandboxResult(ran=False, reason="Docker is not installed or not running, so the tests were not run.")
        if not self.image_ready():
            if self.building:
                return SandboxResult(ran=False, reason="The sandbox image is still being built, so the tests were not run.")
            self.building = True
            try:
                error = self._build_blocking()
            finally:
                self.building = False
            if error:
                self.last_error = error
                return SandboxResult(ran=False, reason=f"Could not build the sandbox image: {error[-300:]}")

        folder = tempfile.mkdtemp(prefix="aisew-sandbox-")
        name = f"aisew-{uuid.uuid4().hex[:12]}"
        try:
            os.chmod(folder, 0o755)  # the container user must be able to read it
            write_files(folder, files)  # the safe writer: bad paths are skipped, never written outside
            requirements = next((c for p, c in files if p == "requirements.txt"), "")
            command = [
                "run", "--rm", "--name", name,
                "--network", "none",
                "--memory", "768m", "--memory-swap", "768m", "--cpus", "1", "--pids-limit", "128",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:rw,size=64m",
                "--mount", f"type=bind,source={folder},target=/work",
                "-w", "/work", "-e", "PYTHONPATH=/work", "-e", "PYTHONDONTWRITEBYTECODE=1",
                IMAGE, "python", "-m", "pytest", "-q", "--maxfail=3", "--tb=short", "-p", "no:cacheprovider",
            ]
            try:
                result = self._docker(command, self.timeout)
            except subprocess.TimeoutExpired:
                self._docker(["rm", "-f", name], 30)  # killing the docker client does not stop the container
                return SandboxResult(ran=True, passed=False,
                                     output=f"The tests did not finish within {self.timeout} seconds (infinite loop?).")
            output = (result.stdout + result.stderr).strip()
            missing = unsupported_packages(requirements)
            if missing:
                output += ("\n\nNOTE: these packages from requirements.txt are not available in the sandbox "
                           f"(it has no network): {', '.join(missing)}. Use only: {', '.join(sorted(ALLOWED_PACKAGES))}.")
            return SandboxResult(ran=True, passed=result.returncode == 0, output=output[-OUTPUT_CHARS:])
        except (OSError, subprocess.SubprocessError) as e:
            return SandboxResult(ran=False, reason=f"The sandbox could not run: {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)
