"""Pipeline service: runs agents, stores artifacts, handles approval + clarification.

Orchestration is data-driven: PIPELINE lists every stage and the stages that must be
APPROVED before it may run. After every approval, all unblocked stages run in parallel.
"""
import asyncio
import time
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy import select

from ..agents.ba import BusinessAnalystAgent
from ..agents.base import FEEDBACK_MARKER
from ..agents.dba import DBAAgent
from ..agents.developer import DeveloperAgent
from ..agents.manager import ManagerAgent
from ..agents.qa import QAAgent
from ..domain.enums import Readiness
from ..agents.autofix import apply_autofix
from ..agents.base import AgentResult
from ..domain.outputs import DeveloperOutput, SandboxReport
from ..workspace.writer import write_files
from ..persistence.models import (
    Artifact, AuditLog, Clarification, LlmUsage, Project, Requirement,
)

AGENTS = {
    "BA": BusinessAnalystAgent,
    "DBA": DBAAgent,
    "Manager": ManagerAgent,
    "QA": QAAgent,
    "Developer": DeveloperAgent,
}

# stage -> stages that must be approved first. New agents are added here.
PIPELINE: dict[str, list[str]] = {
    "BA": [],
    "DBA": ["BA"],
    "Manager": ["BA"],
    "QA": ["BA"],
    "Developer": ["DBA", "Manager", "QA"],
}

MAX_AUTO_STEPS = 8  # rule-based fix passes per test run, a guard against loops
PLACEHOLDER_ANSWERS = {"string", "null", "none", "undefined"}


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidInputError(Exception):
    pass


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


class PipelineService:
    def __init__(self, session_factory, llm, max_parallel: int = 3, workspace_dir: str = "workspaces",
                 sandbox=None, fix_rounds: int = 2):
        self.sf = session_factory
        self.llm = llm
        self.sandbox = sandbox
        self.fix_rounds = fix_rounds
        self.workspace_dir = workspace_dir
        self._sem = asyncio.Semaphore(max(1, max_parallel))
        self._running: dict[tuple[int, str], float] = {}  # (requirement, stage) -> start time
        self._tasks: set[asyncio.Task] = set()

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _audit(s, rid: int, event: str, detail: str = "") -> None:
        s.add(AuditLog(requirement_id=rid, event=event, detail=detail))

    @staticmethod
    def _latest(s, rid: int, agent: str, status: str | None = None):
        q = select(Artifact).where(Artifact.requirement_id == rid, Artifact.agent == agent)
        if status:
            q = q.where(Artifact.status == status)
        return s.scalars(q.order_by(Artifact.version.desc())).first()

    @staticmethod
    def _runnable(s, rid: int) -> list[str]:
        """Stages with no artifact yet whose dependencies are all approved."""
        arts = s.scalars(select(Artifact).where(Artifact.requirement_id == rid)).all()
        have = {a.agent for a in arts}
        approved = {a.agent for a in arts if a.status == "Approved"}
        return [
            stage for stage, deps in PIPELINE.items()
            if stage not in have and all(d in approved for d in deps)
        ]

    @staticmethod
    def _sync_status(s, rid: int) -> None:
        arts = s.scalars(select(Artifact).where(Artifact.requirement_id == rid)).all()
        approved = {a.agent for a in arts if a.status == "Approved"}
        if approved >= set(PIPELINE):
            status = "Complete"
        elif any(a.status == "Pending" for a in arts):
            status = "InReview"
        else:
            status = "NeedsAttention"
        s.get(Requirement, rid).status = status

    def _workspace_root(self, project_id: int, rid: int) -> Path:
        return (Path(self.workspace_dir) / f"project-{project_id}" / f"requirement-{rid}" / "backend").resolve()

    @staticmethod
    def _ancestors(stage: str) -> list[str]:
        """All stages this stage depends on, directly or indirectly."""
        found: list[str] = []
        stack = list(PIPELINE[stage])
        while stack:
            dep = stack.pop()
            if dep not in found:
                found.append(dep)
                stack.extend(PIPELINE[dep])
        return found

    @staticmethod
    def _awaiting(a) -> dict:
        prefix = "Quality check failed: "
        issues = [r[len(prefix):] for r in a.content.get("risks", []) if r.startswith(prefix)]
        action = "reply" if a.readiness == "NotReady" else "review" if issues else "approve"
        return {"artifact_id": a.id, "agent": a.agent, "readiness": a.readiness,
                "action": action, "quality_issues": issues}

    # ---- commands ------------------------------------------------------
    def create_project(self, name: str) -> dict:
        with self.sf() as s:
            p = Project(name=name)
            s.add(p)
            s.commit()
            return {"id": p.id, "name": p.name}

    async def create_requirement(self, text: str, project_id: int | None = None, wait: bool = True) -> int:
        with self.sf() as s:
            if project_id is None:
                project = s.scalars(select(Project).where(Project.name == "Default")).first()
                if not project:
                    project = Project(name="Default")
                    s.add(project)
                    s.flush()
            else:
                project = s.get(Project, project_id)
                if not project:
                    raise NotFoundError(f"Project {project_id} not found")
            req = Requirement(project_id=project.id, text=text)
            s.add(req)
            s.flush()
            self._audit(s, req.id, "RequirementCreated")
            rid = req.id
            s.commit()
        await self._dispatch_advance(rid, wait)
        return rid

    async def approve(self, artifact_id: int, approved_by: str = "human", wait: bool = True) -> int:
        with self.sf() as s:
            art = s.get(Artifact, artifact_id)
            if not art:
                raise NotFoundError(f"Artifact {artifact_id} not found")
            if art.status != "Pending":
                raise ConflictError(f"Artifact is {art.status}; only Pending can be approved")
            if art.readiness == "NotReady":
                raise ConflictError("Artifact is NotReady: answer its open questions first")
            art.status = "Approved"
            art.approved_by = approved_by
            art.approved_at = datetime.now(timezone.utc)
            rid, agent = art.requirement_id, art.agent
            files = art.content.get("files", []) if agent == "Developer" else []
            project_id = s.get(Requirement, rid).project_id
            self._audit(s, rid, f"{agent}Approved", f"v{art.version} by {approved_by}")
            s.flush()
            self._sync_status(s, rid)
            s.commit()
        if agent == "Developer":
            self._write_workspace(rid, project_id, files)
        await self._dispatch_advance(rid, wait)  # start every stage this approval unblocked
        return rid

    def _write_workspace(self, rid: int, project_id: int, files: list[dict]) -> None:
        """Nothing touches the disk until a human approves the Developer artifact."""
        root = self._workspace_root(project_id, rid)
        try:
            report = write_files(root, [(f.get("path", ""), f.get("content", "")) for f in files])
        except OSError as e:
            with self.sf() as s:
                self._audit(s, rid, "DeveloperWriteFailed", f"{type(e).__name__}: {e}"[:500])
                s.commit()
            return
        with self.sf() as s:
            self._audit(s, rid, "DeveloperFilesWritten", f"{len(report.written)} files in {root}")
            for path, reason in report.skipped:
                self._audit(s, rid, "DeveloperFileSkipped", f"{path}: {reason}")
            s.commit()

    async def reply(self, artifact_id: int, answers: list[dict], wait: bool = True) -> int:
        with self.sf() as s:
            art = s.get(Artifact, artifact_id)
            if not art:
                raise NotFoundError(f"Artifact {artifact_id} not found")
            if art.status != "Pending":
                raise ConflictError(f"Artifact is {art.status}; only Pending can be answered")
            rid, agent = art.requirement_id, art.agent
            self._ensure_idle(rid, agent)

            asked = {_norm(q["question"]): q["question"] for q in art.content.get("open_questions", [])}
            if not asked:
                raise InvalidInputError("This artifact has no open questions to answer.")
            clean: list[dict] = []
            for a in answers:
                key = _norm(a["question"])
                if key not in asked:
                    raise InvalidInputError(
                        f"The agent did not ask: {a['question']!r}. Questions asked: {list(asked.values())}"
                    )
                if not a["answer"].strip() or _norm(a["answer"]) in PLACEHOLDER_ANSWERS:
                    raise InvalidInputError(
                        f"The answer to {asked[key]!r} is empty or a placeholder. Write a real answer."
                    )
                clean.append({"question": asked[key], "answer": a["answer"].strip()})

            for a in clean:
                s.add(Clarification(requirement_id=rid, agent=agent,
                                    question=a["question"], answer=a["answer"]))
            self._audit(s, rid, f"{agent}ClarificationsAnswered", f"{len(clean)} answers")
            s.commit()
        await self._dispatch_one(rid, agent, wait)
        return rid

    async def feedback(self, artifact_id: int, text: str, wait: bool = True) -> int:
        """Send a correction to a Pending artifact (even one with no open questions)."""
        text = text.strip()
        if len(text) < 5 or _norm(text) in PLACEHOLDER_ANSWERS:
            raise InvalidInputError("Feedback is empty or a placeholder. Describe what must change.")
        with self.sf() as s:
            art = s.get(Artifact, artifact_id)
            if not art:
                raise NotFoundError(f"Artifact {artifact_id} not found")
            if art.status != "Pending":
                raise ConflictError(f"Artifact is {art.status}; only Pending can receive feedback")
            rid, agent = art.requirement_id, art.agent
            self._ensure_idle(rid, agent)
            s.add(Clarification(requirement_id=rid, agent=agent, question=FEEDBACK_MARKER, answer=text))
            self._audit(s, rid, f"{agent}FeedbackGiven", text[:300])
            s.commit()
        await self._dispatch_one(rid, agent, wait)
        return rid

    async def run_pending(self, rid: int, wait: bool = True) -> int:
        """Run every unblocked stage that has no artifact yet (retry after a failure)."""
        with self.sf() as s:
            if not s.get(Requirement, rid):
                raise NotFoundError(f"Requirement {rid} not found")
        await self._dispatch_advance(rid, wait)
        return rid

    # ---- orchestration -------------------------------------------------
    # Work is planned synchronously (stages are marked "running" immediately) and then either
    # awaited (wait=True, used by tests) or handed to a background task (wait=False, the server).
    def _ensure_idle(self, rid: int, agent: str) -> None:
        if (rid, agent) in self._running:
            raise ConflictError(f"{agent} is already running for this requirement. Wait for it to finish.")

    def _mark_running(self, rid: int, agent: str) -> None:
        self._ensure_idle(rid, agent)
        self._running[(rid, agent)] = time.monotonic()

    def _unmark(self, rid: int, agent: str) -> None:
        self._running.pop((rid, agent), None)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _plan(self, rid: int) -> list[str]:
        """Stages that can start now and are not already running; marks them running."""
        with self.sf() as s:
            runnable = self._runnable(s, rid)
        stages = [st for st in runnable if (rid, st) not in self._running]
        for st in stages:
            self._mark_running(rid, st)
        return stages

    async def _dispatch_advance(self, rid: int, wait: bool) -> None:
        stages = self._plan(rid)
        if not stages:
            return
        work = self._execute(rid, stages)
        if wait:
            await work
        else:
            self._spawn(work)

    async def _dispatch_one(self, rid: int, agent: str, wait: bool) -> None:
        self._mark_running(rid, agent)
        work = self._run_one(rid, agent, swallow=not wait)
        if wait:
            await work
        else:
            self._spawn(work)

    async def _execute(self, rid: int, stages: list[str]) -> None:
        async def guarded(agent: str) -> int:
            try:
                async with self._sem:
                    return await self._run_agent(rid, agent)
            finally:
                self._unmark(rid, agent)

        results = await asyncio.gather(*(guarded(a) for a in stages), return_exceptions=True)
        failures = [(a, r) for a, r in zip(stages, results) if isinstance(r, Exception)]
        if failures:
            self._record_failures(rid, failures)

    async def _run_one(self, rid: int, agent: str, swallow: bool) -> None:
        try:
            async with self._sem:
                await self._run_agent(rid, agent)
        except Exception as e:
            if not swallow:
                raise  # the API turns this into an error response
            self._record_failures(rid, [(agent, e)])
        finally:
            self._unmark(rid, agent)

    def _record_failures(self, rid: int, failures: list[tuple[str, Exception]]) -> None:
        with self.sf() as s:
            for agent, err in failures:
                self._audit(s, rid, f"{agent}Failed", f"{type(err).__name__}: {err}"[:500])
            self._sync_status(s, rid)
            s.commit()

    # ---- test and fix loop (Developer only) -------------------------------
    @staticmethod
    def _first_error(output: str) -> str:
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        errors = [ln for ln in lines if ln.startswith("E ")]
        return (errors[-1] if errors else (lines[-1] if lines else "tests failed"))[:200]

    async def _verify_developer(self, agent, result, context: dict) -> None:
        """Run the generated tests in the sandbox and fix what fails.

        Order of attack on a failure: 1. rule-based fixes (missing import, misspelled argument),
        which cost nothing; 2. the model rewrites only the files named in the traceback.
        The code is only ever written to a temporary sandbox folder here, never to the workspace.
        """
        out = result.output
        if not out.files or out.readiness == Readiness.NOT_READY:
            return  # nothing to test yet (the Developer is waiting for an answer)
        if self.sandbox is None:
            out.test_report = SandboxReport(ran=False, summary="The sandbox is switched off (AISEW_SANDBOX=off).")
            return
        rounds = 0
        auto_steps = 0
        auto_notes: list[str] = []
        while True:
            run = await self.sandbox.run_tests([(f.path, f.content) for f in out.files])
            if not run.ran:
                out.test_report = SandboxReport(ran=False, rounds=rounds, summary=run.reason, auto_fixes=auto_notes)
                out.risks.append(f"Tests were not run: {run.reason}")
                return
            if run.passed:
                out.test_report = SandboxReport(ran=True, passed=True, rounds=rounds, summary="All tests passed.",
                                                output=run.output, auto_fixes=auto_notes)
                return
            if auto_steps < MAX_AUTO_STEPS:
                fixed, notes = apply_autofix([(f.path, f.content) for f in out.files], run.output)
                if notes:
                    auto_steps += 1
                    auto_notes.extend(notes)
                    out = out.model_copy(update={"files": [
                        f.model_copy(update={"content": content}) for f, (_, content) in zip(out.files, fixed)]})
                    result.output = out
                    continue  # run the tests again; this costs no model call and no fix round
            if rounds >= self.fix_rounds:
                summary = self._first_error(run.output)
                out.test_report = SandboxReport(ran=True, passed=False, rounds=rounds, summary=summary,
                                                output=run.output, auto_fixes=auto_notes)
                issue = f"The tests still fail after {rounds} automatic fix rounds: {summary}"
                agent._flag_issues(out, [issue])
                result.quality_issues.append(issue)
                return
            rounds += 1
            out, usage = await agent.repair(context, out, run.output)
            result.output = out
            result.usage.extend(usage)

    # ---- core: run one agent and store its artifact ---------------------
    def _build_context(self, s, rid: int, agent_name: str) -> dict:
        req = s.get(Requirement, rid)
        clar = s.scalars(
            select(Clarification)
            .where(Clarification.requirement_id == rid, Clarification.agent == agent_name)
            .order_by(Clarification.id)
        ).all()
        context: dict = {
            "requirement": req.text,
            "clarifications": [
                {"question": c.question, "answer": c.answer}
                for c in clar if c.question != FEEDBACK_MARKER
            ],
        }
        feedback = [c.answer for c in clar if c.question == FEEDBACK_MARKER]
        if feedback:
            context["feedback"] = feedback
            current = self._latest(s, rid, agent_name)
            if current:
                context["previous_output"] = current.content
        needed = self._ancestors(agent_name)
        for dep in needed:
            done = self._latest(s, rid, dep, status="Approved")
            if not done:
                raise ConflictError(f"{agent_name} needs an approved {dep} artifact")
            context[f"{dep.lower()}_output"] = done.content
        if "BA" in needed:
            # what the business already told the BA: every downstream agent must respect it
            ba_clar = s.scalars(
                select(Clarification)
                .where(Clarification.requirement_id == rid, Clarification.agent == "BA",
                       Clarification.question != FEEDBACK_MARKER)
                .order_by(Clarification.id)
            ).all()
            context["ba_clarifications"] = [{"question": c.question, "answer": c.answer} for c in ba_clar]
        return context

    async def _run_agent(self, rid: int, agent_name: str) -> int:
        with self.sf() as s:
            context = self._build_context(s, rid, agent_name)
        agent = AGENTS[agent_name](self.llm)
        result = await agent.run(context)  # may raise AgentOutputError
        if agent_name == "Developer":
            await self._verify_developer(agent, result, context)
        return self._store(rid, agent_name, result)

    def _store(self, rid: int, agent_name: str, result, event: str | None = None) -> int:
        with self.sf() as s:
            prev = self._latest(s, rid, agent_name)
            version = prev.version + 1 if prev else 1
            for old in s.scalars(select(Artifact).where(
                Artifact.requirement_id == rid, Artifact.agent == agent_name,
                Artifact.status == "Pending",
            )):
                old.status = "Superseded"
            out = result.output
            art = Artifact(
                requirement_id=rid, agent=agent_name, version=version, status="Pending",
                readiness=out.readiness.value, confidence=out.confidence,
                content=out.model_dump(mode="json"),
            )
            s.add(art)
            for u in result.usage:
                s.add(LlmUsage(requirement_id=rid, agent=agent_name, model=u.model,
                               prompt_tokens=u.prompt_tokens,
                               completion_tokens=u.completion_tokens,
                               latency_ms=u.latency_ms))
            s.flush()
            self._sync_status(s, rid)
            self._audit(s, rid, event or f"{agent_name}Generated",
                        f"v{version} {out.readiness.value} confidence={out.confidence} "
                        f"attempts={result.attempts}")
            if result.quality_issues:
                self._audit(s, rid, f"{agent_name}QualityIssues", "; ".join(result.quality_issues))
            report = getattr(out, "test_report", None)
            if report is not None:
                if not report.ran:
                    self._audit(s, rid, "DeveloperTestsNotRun", report.summary)
                elif report.passed:
                    self._audit(s, rid, "DeveloperTestsPassed", f"after {report.rounds} fix rounds")
                else:
                    self._audit(s, rid, "DeveloperTestsFailed", f"after {report.rounds} fix rounds: {report.summary}")
                if report.auto_fixes:
                    self._audit(s, rid, "DeveloperAutoFix", "; ".join(report.auto_fixes)[:500])
            aid = art.id
            s.commit()
        return aid

    # ---- retest: run the stored code's tests again, with the automatic fixes, no model rewrite first
    async def retest(self, artifact_id: int, wait: bool = True) -> int:
        with self.sf() as s:
            art = s.get(Artifact, artifact_id)
            if not art:
                raise NotFoundError(f"Artifact {artifact_id} not found")
            if art.agent != "Developer" or not art.content.get("files"):
                raise InvalidInputError("Only a Developer artifact with code files can be retested.")
            if art.status != "Pending":
                raise ConflictError(f"Artifact is {art.status}; only Pending can be retested")
            rid = art.requirement_id
            self._ensure_idle(rid, "Developer")
        self._mark_running(rid, "Developer")
        work = self._retest_one(rid, artifact_id, swallow=not wait)
        if wait:
            await work
        else:
            self._spawn(work)
        return rid

    async def _retest_one(self, rid: int, artifact_id: int, swallow: bool) -> None:
        try:
            async with self._sem:
                with self.sf() as s:
                    context = self._build_context(s, rid, "Developer")
                    content = s.get(Artifact, artifact_id).content
                out = DeveloperOutput.model_validate(content)
                # notes about the previous test run are about to be redone
                out.risks = [r for r in out.risks
                             if not r.startswith(("Quality check failed: The tests still fail", "Tests were not run"))]
                out.test_report = None
                agent = DeveloperAgent(self.llm)
                result = AgentResult(output=out, attempts=0, usage=[], quality_issues=[])
                await self._verify_developer(agent, result, context)
                self._store(rid, "Developer", result, event="DeveloperRetested")
        except Exception as e:
            if not swallow:
                raise
            self._record_failures(rid, [("Developer", e)])
        finally:
            self._unmark(rid, "Developer")

    # ---- queries -------------------------------------------------------
    def get_code(self, artifact_id: int) -> str:
        with self.sf() as s:
            art = s.get(Artifact, artifact_id)
            if not art:
                raise NotFoundError(f"Artifact {artifact_id} not found")
            files = art.content.get("files")
            if not files:
                raise InvalidInputError("This artifact has no code files (only Developer artifacts do).")
            parts = [f"===== {f['path']} =====\n{f['content']}" for f in files]
            return "\n\n".join(parts) + "\n"

    def get_requirement(self, rid: int) -> dict:
        with self.sf() as s:
            req = s.get(Requirement, rid)
            if not req:
                raise NotFoundError(f"Requirement {rid} not found")
            arts = s.scalars(select(Artifact).where(Artifact.requirement_id == rid)
                             .order_by(Artifact.id)).all()
            clar = s.scalars(select(Clarification).where(Clarification.requirement_id == rid)
                             .order_by(Clarification.id)).all()
            log = s.scalars(select(AuditLog).where(AuditLog.requirement_id == rid)
                            .order_by(AuditLog.id)).all()
            developer_done = any(a.agent == "Developer" and a.status == "Approved" for a in arts)
            now = time.monotonic()
            running = [{"agent": a, "seconds": int(now - t0)}
                       for (r, a), t0 in self._running.items() if r == rid]
            return {
                "id": req.id, "text": req.text, "status": "Running" if running else req.status,
                "running": running,
                "workspace": str(self._workspace_root(req.project_id, rid)) if developer_done else None,
                # what YOU need to do next
                "awaiting": [self._awaiting(a) for a in arts if a.status == "Pending"],
                # stages that could run but have no result (a failed stage): POST /requirements/{id}/run
                "needs_retry": [st for st in self._runnable(s, rid) if (rid, st) not in self._running],
                "artifacts": [
                    {"id": a.id, "agent": a.agent, "version": a.version, "status": a.status,
                     "readiness": a.readiness, "confidence": a.confidence,
                     "approved_by": a.approved_by, "content": a.content}
                    for a in arts
                ],
                "clarifications": [
                    {"agent": c.agent, "question": c.question, "answer": c.answer} for c in clar
                ],
                "audit_log": [
                    {"event": e.event, "detail": e.detail, "at": e.created_at} for e in log
                ],
            }
