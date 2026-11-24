"""Developer agent: two steps, because small models cannot write code inside JSON.

Step 1  plan  : JSON answer with the list of files and a one-sentence purpose for each.
Step 2  write : one plain-text model call per file. Each file is checked on its own
                (syntax, placeholders, tables) and retried on its own if it is bad.
"""
import ast
import json
import re

from ..domain.enums import Readiness
from ..domain.outputs import CodeFile, DeveloperOutput, DeveloperPlan, PlannedFile
from ..llm.base import LLMResponse
from ..workspace.writer import MAX_FILE_BYTES, validate_relative_path
from .base import AgentResult, BaseAgent
from .context import format_analysis, format_plan, format_schema, format_tests

MAX_FILES_ALLOWED = 12
PRIOR_FILE_CHARS = 2500

EXAMPLE_FILES = [
    {
        "path": "app/export.py",
        "purpose": "Function orders_to_csv that turns a list of orders into CSV text",
        "content": (
            "import csv\nimport io\n\n\n"
            "def orders_to_csv(orders: list[dict]) -> str:\n"
            "    out = io.StringIO()\n"
            "    writer = csv.writer(out)\n"
            "    writer.writerow([\"id\", \"status\"])\n"
            "    for order in orders:\n"
            "        writer.writerow([order[\"id\"], order[\"status\"]])\n"
            "    return out.getvalue()\n"
        ),
    },
    {
        "path": "tests/test_export.py",
        "purpose": "pytest test that the CSV contains each order",
        "content": (
            "from app.export import orders_to_csv\n\n\n"
            "def test_export_contains_each_order():\n"
            "    text = orders_to_csv([{\"id\": \"1\", \"status\": \"paid\"}])\n"
            "    assert \"1,paid\" in text\n"
        ),
    },
    {"path": "requirements.txt", "purpose": "Python packages: pytest", "content": "pytest\n"},
]
EXAMPLE_PLAN = {
    "readiness": "ReadyWithClarifications",
    "confidence": 70,
    "summary": "A small module that exports orders as CSV, with a test.",
    "open_questions": [{"question": "Which order fields must the CSV contain?", "blocking": False}],
    "risks": [],
    "files": [{"path": f["path"], "purpose": f["purpose"]} for f in EXAMPLE_FILES],
    "run_instructions": "pip install -r requirements.txt, then run pytest",
}
EXAMPLE_TABLES = {"tables": [{"name": "orders"}]}

_PLACEHOLDER = re.compile(
    r"\bTODO\b|\bFIXME\b|NotImplementedError|^\s*\.\.\.\s*$|pass\s*#\s*(?:implement|todo)",
    re.I | re.M,
)
_FENCE = re.compile(r"```[A-Za-z0-9_+.-]*[ \t]*\n(.*?)```", re.S)


def _path_issue(path: str) -> str | None:
    reason = validate_relative_path(path)
    return f"Unsafe or invalid path {path!r}: {reason}." if reason else None


def _files_in_traceback(text: str, known: list[str]) -> list[str]:
    """Project files named in a pytest traceback, the deepest frame (the real error) first."""
    order: list[str] = []
    for match in re.finditer(r"([\w./\\:-]+\.py)", text):
        path = match.group(1).replace("\\", "/")
        for k in known:
            if path == k or path.endswith("/" + k):
                order.append(k)
    result: list[str] = []
    for k in reversed(order):
        if k not in result:
            result.append(k)
    return result


def _clean_file(text: str) -> str:
    """Models often wrap code in ``` fences or add chatter around it. Keep only the code."""
    text = text.strip("\r\n")
    block = _FENCE.search(text)
    if block:
        return block.group(1).strip("\r\n")
    if text.startswith("```"):  # opening fence but the model never closed it
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return text.strip("\r\n")


class DeveloperAgent(BaseAgent[DeveloperOutput]):
    name = "Developer"
    output_model = DeveloperPlan  # the schema of step 1 (the plan)
    profile = (
        "You are a senior Software Developer. You turn an approved analysis, database "
        "design, delivery plan and test plan into small, complete, working backend code."
    )
    core_rules = [
        "In this step you only PLAN the files. Do not write any code yet: give each file a path and a one-sentence purpose.",
        "Use Python 3.11, FastAPI, Pydantic and SQLAlchemy 2.0. Put application code under app/ and tests under tests/.",
        "Plan the smallest set of files that implements the approved design: at most 8 files.",
        "Always include app/models.py (one SQLAlchemy model per table of the design), at least one pytest file under tests/, and requirements.txt.",
        "Implement ONLY the behaviour in the business rules and acceptance criteria. Plan no extra features, endpoints or fields.",
        "The purpose must say exactly what the file contains, for example 'function cancel_booking that applies the 24 hour refund rule'.",
        "Paths are relative, for example app/models.py. Never use '..', absolute paths or drive letters.",
        "Put short run instructions in run_instructions.",
        "If an unanswered question changes the code, raise it in open_questions instead of guessing.",
    ]
    examples = (
        "Design: table orders (id, status)\n"
        f"Answer:\n{json.dumps(EXAMPLE_PLAN, indent=2)}"
    )
    file_rules = [
        "Output only the raw content of the file. No markdown fences and no explanation before or after.",
        "The file must be complete and runnable: no TODO, no placeholders, no '...' and no unimplemented functions.",
        "Python files: valid Python 3.11 with every import they need. Use only FastAPI, Pydantic, SQLAlchemy 2.0, pytest and the standard library.",
        "app/models.py: one SQLAlchemy 2.0 model per table of the approved design. Set __tablename__ to the exact table name and use the exact column names.",
        "Implement ONLY the business rules and acceptance criteria. Apply time rules exactly as stated.",
        "Tests: pytest, import from the app package, and cover the QA test cases.",
        "requirements.txt: one package name per line, for every third-party package the project imports.",
        "Import from the other project files using exactly the paths listed in the file list.",
    ]

    # ------------------------------------------------------------------ step 1: plan
    def build_user_prompt(self, context: dict) -> str:
        slim = dict(context)
        previous = context.get("previous_output")
        if previous:  # the plan step needs the file list, not all the old code
            slim["previous_output"] = {
                "summary": previous.get("summary", ""),
                "files": [{"path": f["path"], "purpose": f.get("purpose", "")} for f in previous.get("files", [])],
            }
        base = super().build_user_prompt(slim)
        return (
            f"{base}\n\nApproved business analysis:\n{format_analysis(context['ba_output'])}"
            f"\n\nApproved database design (implement exactly these tables and columns):\n{format_schema(context['dba_output'])}"
            f"\n\nApproved delivery plan, in order:\n{format_plan(context['manager_output'])}"
            f"\n\nApproved QA test cases (the tests must cover these):\n{format_tests(context['qa_output'])}"
        )

    def check_quality(self, output, context: dict) -> list[str]:
        if isinstance(output, DeveloperPlan):
            return self._check_plan(output)
        return self._check_code(output, context)

    def _check_plan(self, plan: DeveloperPlan) -> list[str]:
        if not plan.files:
            return ["No files were planned."]
        issues: list[str] = []
        paths = [f.path for f in plan.files]
        if len(paths) > MAX_FILES_ALLOWED:
            issues.append(f"Too many files ({len(paths)}). Keep it to 8 or fewer.")
        for f in plan.files:
            issue = _path_issue(f.path)
            if issue:
                issues.append(issue)
            elif not f.purpose.strip():
                issues.append(f"File {f.path} has no purpose sentence.")
        dupes = sorted({p for p in paths if paths.count(p) > 1})
        if dupes:
            issues.append(f"Duplicate file paths: {', '.join(dupes)}.")
        if "app/models.py" not in paths:
            issues.append("app/models.py is missing. It must hold one model per table of the design.")
        if not any(p.startswith("tests/") and p.endswith(".py") for p in paths):
            issues.append("There are no pytest tests. Plan a file such as tests/test_main.py.")
        if "requirements.txt" not in paths:
            issues.append("requirements.txt is missing.")
        return issues

    # ------------------------------------------------------------------ step 2: write
    async def run(self, context: dict) -> AgentResult[DeveloperOutput]:
        plan_result = await super().run(context)  # step 1, with the normal retry + quality loop
        plan: DeveloperPlan = plan_result.output  # type: ignore[assignment]
        usage: list[LLMResponse] = list(plan_result.usage)
        known_issues = list(plan_result.quality_issues)

        if plan.readiness == Readiness.NOT_READY:  # a blocking question: do not waste time writing code
            output = self._assemble(plan, [])
            return AgentResult(output=output, attempts=len(usage), usage=usage, quality_issues=known_issues)

        previous = {
            f["path"]: f.get("content", "")
            for f in (context.get("previous_output") or {}).get("files", [])
        }
        files: list[CodeFile] = []
        written: dict[str, str] = {}
        seen: set[str] = set()
        for planned in plan.files:
            if planned.path in seen:
                continue
            seen.add(planned.path)
            if validate_relative_path(planned.path):
                # never generated; kept (empty) so the safe writer rejects it and logs it
                files.append(CodeFile(path=planned.path, content="", purpose=planned.purpose))
                continue
            text = await self._write_file(planned, plan, context, written, previous.get(planned.path), usage)
            files.append(CodeFile(path=planned.path, content=text, purpose=planned.purpose))
            written[planned.path] = text

        output = self._assemble(plan, files)
        fresh = [i for i in self._check_code(output, context) if i not in known_issues]
        if fresh:
            self._flag_issues(output, fresh)
        return AgentResult(output=output, attempts=len(usage), usage=usage, quality_issues=known_issues + fresh)

    @staticmethod
    def _assemble(plan: DeveloperPlan, files: list[CodeFile]) -> DeveloperOutput:
        return DeveloperOutput(
            readiness=plan.readiness,
            confidence=plan.confidence,
            summary=plan.summary,
            open_questions=[q.model_dump() for q in plan.open_questions],
            risks=list(plan.risks),
            files=files,
            run_instructions=plan.run_instructions,
        )

    def _file_system_prompt(self) -> str:
        rules = "\n".join(f"- {r}" for r in self.file_rules)
        return (
            f"{self.profile}\n\nYou now write exactly ONE file of the project. Reply with the complete "
            f"raw content of that file and nothing else.\nRules:\n{rules}"
        )

    def _file_user_prompt(self, planned: PlannedFile, plan: DeveloperPlan, context: dict,
                          written: dict[str, str], previous_text: str | None) -> str:
        parts = [f"Requirement:\n{context['requirement']}"]
        facts = context.get("ba_clarifications") or []
        if facts:
            parts.append("Facts confirmed by the business (ground truth):\n"
                         + "\n".join(f"- {c['question']} -> {c['answer']}" for c in facts))
        parts.append(f"Approved business analysis:\n{format_analysis(context['ba_output'])}")
        parts.append(f"Approved database design:\n{format_schema(context['dba_output'])}")
        listing = "\n".join(
            f"- {f.path}: {f.purpose}" + ("   <<< YOU ARE WRITING THIS FILE" if f.path == planned.path else "")
            for f in plan.files
        )
        parts.append(f"Files in this project:\n{listing}")
        if written:
            shown = "\n\n".join(
                f"--- {path} ---\n{text[:PRIOR_FILE_CHARS]}" for path, text in written.items()
            )
            parts.append(f"Files already written (import from them exactly as they are):\n{shown}")
        if planned.path.startswith("tests/"):
            parts.append(f"QA test cases to cover:\n{format_tests(context['qa_output'])}")
        feedback = context.get("feedback") or []
        if feedback:
            parts.append("Reviewer feedback. Apply EVERY point:\n" + "\n".join(f"- {f}" for f in feedback))
            if previous_text:
                parts.append(f"Your previous version of this file (revise it):\n{previous_text[:6000]}")
        parts.append(f"Write the file {planned.path} now.\nPurpose: {planned.purpose}")
        return "\n\n".join(parts)

    async def _write_file(self, planned: PlannedFile, plan: DeveloperPlan, context: dict,
                          written: dict[str, str], previous_text: str | None,
                          usage: list[LLMResponse]) -> str:
        system = self._file_system_prompt()
        user = self._file_user_prompt(planned, plan, context, written, previous_text)
        feedback = ""
        text = ""
        for _ in range(self.max_retries + 1):
            resp = await self.llm.generate(system=system, user=user + feedback, schema=None)
            usage.append(resp)
            text = _clean_file(resp.text)
            problem = self._file_problem(planned.path, text, context)
            if not problem:
                return text
            feedback = (
                f"\n\nYour previous version had this problem: {problem}\n"
                "Return the corrected, complete file."
            )
        return text  # still imperfect: the final quality check will flag it

    @staticmethod
    def _file_problem(path: str, text: str, context: dict) -> str | None:
        if not text.strip():
            return "the file is empty."
        if _PLACEHOLDER.search(text):
            return "it contains a TODO or placeholder. Write the real code."
        if path.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError as e:
                return f"Python syntax error on line {e.lineno}: {e.msg}."
        if path == "app/models.py":
            lowered = text.lower()
            missing = [t["name"] for t in (context.get("dba_output") or {}).get("tables", [])
                       if t["name"].lower() not in lowered]
            if missing:
                return (f"it does not define these tables: {', '.join(missing)}. "
                        "Set __tablename__ to the exact table name.")
        return None

    # ------------------------------------------------------------------ repair after a failed test run
    async def repair(self, context: dict, output: DeveloperOutput, test_output: str):
        """Rewrite only the files that the failing traceback points at. Returns (output, usage)."""
        usage: list[LLMResponse] = []
        known = [f.path for f in output.files if not validate_relative_path(f.path)]
        targets = _files_in_traceback(test_output, known)[:3]
        if not targets:
            tests = [p for p in known if p.startswith("tests/") and p.endswith(".py")]
            targets = (tests or known)[:1]
        plan = DeveloperPlan(
            readiness=output.readiness, confidence=output.confidence, summary=output.summary,
            files=[{"path": f.path, "purpose": f.purpose} for f in output.files],
            run_instructions=output.run_instructions,
        )
        contents = {f.path: f.content for f in output.files}
        purposes = {f.path: f.purpose for f in output.files}
        tail = test_output[-2500:]
        for path in targets:
            ctx = dict(context, feedback=[
                f"The tests were run and FAILED. Fix {path} so that they pass. Test output:\n{tail}"])
            others = {p: c for p, c in contents.items() if p != path and c.strip()}
            contents[path] = await self._write_file(
                PlannedFile(path=path, purpose=purposes[path]), plan, ctx, others, contents[path], usage)
        files = [CodeFile(path=f.path, content=contents[f.path], purpose=f.purpose) for f in output.files]
        return output.model_copy(update={"files": files}), usage

    # ------------------------------------------------------------------ final check
    def _check_code(self, output: DeveloperOutput, context: dict) -> list[str]:
        if not output.files:
            return ["No files were written."]
        issues: list[str] = []
        if len(output.files) > MAX_FILES_ALLOWED:
            issues.append(f"Too many files ({len(output.files)}). Keep it to 8 or fewer.")

        paths = [f.path for f in output.files]
        for f in output.files:
            issue = _path_issue(f.path)
            if issue:
                issues.append(issue)
                continue
            if not f.content.strip():
                issues.append(f"File {f.path} is empty.")
                continue
            if len(f.content.encode("utf-8")) > MAX_FILE_BYTES:
                issues.append(f"File {f.path} is too large.")
            if _PLACEHOLDER.search(f.content):
                issues.append(f"File {f.path} contains a TODO or placeholder. Write the real code.")
            if f.path.endswith(".py"):
                try:
                    ast.parse(f.content)
                except SyntaxError as e:
                    issues.append(f"File {f.path} has a Python syntax error on line {e.lineno}: {e.msg}.")
        dupes = sorted({p for p in paths if paths.count(p) > 1})
        if dupes:
            issues.append(f"Duplicate file paths: {', '.join(dupes)}.")
        if not any(p.startswith("tests/") and p.endswith(".py") for p in paths):
            issues.append("There are no pytest tests. Add a file such as tests/test_main.py.")
        if "requirements.txt" not in paths:
            issues.append("requirements.txt is missing.")

        code = "\n".join(f.content for f in output.files).lower()
        for table in (context.get("dba_output") or {}).get("tables", []):
            if table["name"].lower() not in code:
                issues.append(f"The design has table '{table['name']}' but no file mentions it.")
        return issues
