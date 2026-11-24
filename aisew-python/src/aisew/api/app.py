import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..agents.base import AgentOutputError
from ..config import Settings
from ..llm.ollama_client import OllamaClient
from ..orchestration.pipeline import (
    ConflictError, InvalidInputError, NotFoundError, PipelineService,
)
from ..persistence.db import make_engine, make_session_factory
from ..sandbox.docker import DockerSandbox

_DEFAULT = object()


class ProjectIn(BaseModel):
    name: str = Field(min_length=1)


class RequirementIn(BaseModel):
    text: str = Field(min_length=5)
    project_id: int | None = None


class AnswerIn(BaseModel):
    question: str
    answer: str


class FeedbackIn(BaseModel):
    feedback: str = Field(min_length=5)


class ReplyIn(BaseModel):
    answers: list[AnswerIn] = Field(min_length=1)


def create_app(
    llm=None,
    database_url: str | None = None,
    max_parallel: int | None = None,
    workspace_dir: str | None = None,
    background: bool | None = None,
    sandbox=_DEFAULT,
) -> FastAPI:
    settings = Settings.from_env()
    if sandbox is _DEFAULT:
        sandbox = DockerSandbox(timeout=settings.test_timeout) if settings.sandbox == "docker" else None
    engine = make_engine(database_url or settings.database_url)
    service = PipelineService(
        make_session_factory(engine),
        llm or OllamaClient(settings),
        max_parallel=max_parallel or settings.max_parallel,
        workspace_dir=workspace_dir or settings.workspace_dir,
        sandbox=sandbox,
        fix_rounds=settings.fix_rounds,
    )

    # background=True: the request returns at once and the agents keep working. Poll GET /requirements/{id}.
    wait = not (settings.background if background is None else background)

    app = FastAPI(title="AISEW", description="AI Software Engineering Workforce")

    @app.exception_handler(NotFoundError)
    async def _nf(_: Request, e: NotFoundError):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, e: ConflictError):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(InvalidInputError)
    async def _invalid(_: Request, e: InvalidInputError):
        return JSONResponse({"detail": str(e)}, status_code=422)

    @app.exception_handler(AgentOutputError)
    async def _bad_output(_: Request, e: AgentOutputError):
        return JSONResponse({"detail": str(e)}, status_code=502)

    build_tasks: set = set()

    @app.get("/sandbox")
    async def sandbox_status():
        """Is Docker ready to run the generated tests safely?"""
        return await sandbox.status() if sandbox else {"mode": "off"}

    @app.post("/sandbox/build")
    async def sandbox_build():
        """Build the sandbox image once (a few minutes). Runs in the background."""
        if sandbox is None:
            return {"mode": "off"}
        if not sandbox.building:
            sandbox.building = True
            task = asyncio.get_running_loop().create_task(sandbox.build())
            build_tasks.add(task)
            task.add_done_callback(build_tasks.discard)
        return {**await sandbox.status(), "building": True}

    @app.post("/projects")
    async def create_project(body: ProjectIn):
        return service.create_project(body.name)

    @app.post("/requirements")
    async def create_requirement(body: RequirementIn):
        rid = await service.create_requirement(body.text, body.project_id, wait=wait)
        return service.get_requirement(rid)

    @app.get("/requirements/{rid}")
    async def get_requirement(rid: int):
        return service.get_requirement(rid)

    @app.post("/requirements/{rid}/run")
    async def run_pending(rid: int):
        """Retry stages that could run but have no result (for example after a failure)."""
        await service.run_pending(rid, wait=wait)
        return service.get_requirement(rid)

    @app.get("/artifacts/{artifact_id}/code", response_class=PlainTextResponse)
    async def code(artifact_id: int):
        """Read a Developer artifact's files as plain text (easier than the JSON)."""
        return service.get_code(artifact_id)

    @app.post("/artifacts/{artifact_id}/retest")
    async def retest(artifact_id: int):
        """Run a Developer artifact's tests again (rule-based fixes first). No model rewrite is needed."""
        rid = await service.retest(artifact_id, wait=wait)
        return service.get_requirement(rid)

    @app.post("/artifacts/{artifact_id}/approve")
    async def approve(artifact_id: int):
        rid = await service.approve(artifact_id, wait=wait)
        return service.get_requirement(rid)

    @app.post("/artifacts/{artifact_id}/feedback")
    async def feedback(artifact_id: int, body: FeedbackIn):
        """Correct a Pending artifact. The agent revises its previous answer."""
        rid = await service.feedback(artifact_id, body.feedback, wait=wait)
        return service.get_requirement(rid)

    @app.post("/artifacts/{artifact_id}/reply")
    async def reply(artifact_id: int, body: ReplyIn):
        rid = await service.reply(artifact_id, [a.model_dump() for a in body.answers], wait=wait)
        return service.get_requirement(rid)

    return app
