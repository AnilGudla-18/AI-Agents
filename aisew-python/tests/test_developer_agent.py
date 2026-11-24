import json

from aisew.agents.developer import (
    EXAMPLE_FILES, EXAMPLE_PLAN, EXAMPLE_TABLES, DeveloperAgent, _clean_file,
)
from aisew.domain.outputs import DeveloperOutput, DeveloperPlan
from aisew.llm.fake import FakeLLM

CONTEXT = {
    "requirement": "Customers can cancel a booking",
    "ba_output": {"user_stories": [], "business_rules": ["A cancelled booking is refunded"],
                  "acceptance_criteria": ["Given a booking, when cancelled, then refunded"], "open_questions": []},
    "dba_output": {"tables": [
        {"name": "bookings", "columns": [
            {"name": "id", "type": "uuid", "primary_key": True},
            {"name": "status", "type": "text", "nullable": False}]},
        {"name": "refunds", "columns": [
            {"name": "booking_id", "type": "uuid", "references": "bookings.id"}]}]},
    "manager_output": {"tasks": [{"title": "Build endpoint", "role": "backend developer", "effort": "M"}]},
    "qa_output": {"test_cases": [{"title": "Cancel a booking", "kind": "functional", "expected": "Refunded"}]},
    "ba_clarifications": [{"question": "How is the refund paid?", "answer": "Original payment method"}],
}
MODELS = ('class Booking:\n    __tablename__ = "bookings"\n\n\nclass Refund:\n    __tablename__ = "refunds"\n')
TESTS = "from app.models import Booking\n\n\ndef test_it():\n    assert Booking\n"
REQS = "sqlalchemy\npytest\n"
PLAN_FILES = [
    {"path": "app/models.py", "purpose": "SQLAlchemy models for bookings and refunds"},
    {"path": "tests/test_it.py", "purpose": "pytest tests for cancelling"},
    {"path": "requirements.txt", "purpose": "Python packages"},
]


def plan(files=None, **o) -> str:
    d = {"readiness": "Ready", "confidence": 80, "summary": "code", "open_questions": [], "risks": [],
         "files": files if files is not None else PLAN_FILES, "run_instructions": "pytest"}
    d.update(o)
    return json.dumps(d)


async def run(responses, ctx=CONTEXT, **kw):
    llm = FakeLLM(responses)
    return await DeveloperAgent(llm, **kw).run(ctx), llm


def file_calls(llm):
    return [c for c in llm.calls if c["schema"] is None]


def plan_calls(llm):
    return [c for c in llm.calls if c["schema"] is not None]


# ---------------- happy path ----------------
async def test_plan_then_one_call_per_file():
    result, llm = await run([plan(), MODELS, TESTS, REQS])
    assert len(plan_calls(llm)) == 1 and len(file_calls(llm)) == 3
    assert [f.path for f in result.output.files] == ["app/models.py", "tests/test_it.py", "requirements.txt"]
    assert result.output.files[0].content == MODELS.rstrip("\n")  # the writer adds the final newline
    assert result.quality_issues == [] and result.attempts == 4
    assert len(result.usage) == 4


async def test_file_calls_ask_for_plain_text_and_route_to_the_developer_profile():
    _, llm = await run([plan(), MODELS, TESTS, REQS])
    system = file_calls(llm)[0]["system"]
    assert system.split("\n\n")[0].startswith("You are a senior Software Developer")
    assert "exactly ONE file" in system and "No markdown fences" in system
    assert "<<< YOU ARE WRITING THIS FILE" in file_calls(llm)[0]["user"]


async def test_later_files_see_the_files_already_written():
    _, llm = await run([plan(), MODELS, TESTS, REQS])
    models_prompt, tests_prompt = file_calls(llm)[0]["user"], file_calls(llm)[1]["user"]
    assert "Files already written" not in models_prompt
    assert '__tablename__ = "bookings"' in tests_prompt and "--- app/models.py ---" in tests_prompt


async def test_qa_cases_and_confirmed_facts_go_to_the_right_prompts():
    _, llm = await run([plan(), MODELS, TESTS, REQS])
    models_prompt, tests_prompt = file_calls(llm)[0]["user"], file_calls(llm)[1]["user"]
    assert "[functional] Cancel a booking: Refunded" in tests_prompt
    assert "[functional] Cancel a booking" not in models_prompt
    assert "Original payment method" in models_prompt
    assert "- bookings: id uuid PK; status text NOT NULL" in models_prompt


# ---------------- cleaning the model's text ----------------
def test_clean_file_removes_fences_and_chatter():
    assert _clean_file("```python\nx = 1\n```") == "x = 1"
    assert _clean_file("Sure! Here is the file:\n```py\nx = 1\ny = 2\n```\nHope it helps") == "x = 1\ny = 2"
    assert _clean_file("```\nx = 1") == "x = 1"  # fence never closed
    assert _clean_file("\n\nx = 1\n\n") == "x = 1"


async def test_fenced_replies_are_accepted():
    result, _ = await run([plan(), f"```python\n{MODELS}```", TESTS, REQS])
    assert result.output.files[0].content.strip() == MODELS.strip()
    assert result.quality_issues == []


# ---------------- each file is checked and retried on its own ----------------
async def test_a_syntax_error_retries_only_that_file():
    result, llm = await run([plan(), MODELS + "def oops(:\n", MODELS, TESTS, REQS])
    assert len(file_calls(llm)) == 4 and len(plan_calls(llm)) == 1
    retry = file_calls(llm)[1]["user"]
    assert "Python syntax error on line" in retry and "app/models.py" in retry
    assert result.quality_issues == [] and result.output.files[0].content == MODELS.rstrip("\n")


async def test_empty_reply_and_placeholders_are_retried():
    for bad in ("   ", "def f():\n    # TODO implement\n    return 1\n", "def f():\n    raise NotImplementedError\n"):
        result, llm = await run([plan(), MODELS, bad, TESTS, REQS])
        assert len(file_calls(llm)) == 4
        assert result.output.files[1].content == TESTS.rstrip("\n")


async def test_models_file_must_define_every_table():
    only_one = 'class Booking:\n    __tablename__ = "bookings"\n'
    result, llm = await run([plan(), only_one, MODELS, TESTS, REQS])
    assert "does not define these tables: refunds" in file_calls(llm)[1]["user"]
    assert result.quality_issues == []


async def test_a_file_that_stays_broken_is_kept_and_flagged_and_the_others_are_still_written():
    broken = "def oops(:\n"
    result, llm = await run([plan(), broken, broken, TESTS, REQS])
    assert len(file_calls(llm)) == 4
    assert [f.path for f in result.output.files] == ["app/models.py", "tests/test_it.py", "requirements.txt"]
    assert result.output.files[1].content == TESTS.rstrip("\n")
    assert result.output.files[2].content == REQS.rstrip("\n")
    assert any("syntax error" in i and "app/models.py" in i for i in result.quality_issues)
    assert result.output.confidence <= 60
    assert any(r.startswith("Quality check failed") for r in result.output.risks)


# ---------------- step 1 (the plan) ----------------
async def test_plan_missing_tests_models_or_requirements_is_retried():
    bad = [{"path": "app/main.py", "purpose": "main"}]
    result, llm = await run([plan(files=bad), plan(), MODELS, TESTS, REQS])
    assert len(plan_calls(llm)) == 2
    retry = plan_calls(llm)[1]["user"]
    assert "app/models.py is missing" in retry and "no pytest tests" in retry and "requirements.txt is missing" in retry


async def test_plan_with_a_missing_purpose_is_retried():
    bad = [dict(f) for f in PLAN_FILES]
    bad[0]["purpose"] = ""
    _, llm = await run([plan(files=bad), plan(), MODELS, TESTS, REQS])
    assert "has no purpose" in plan_calls(llm)[1]["user"]


async def test_too_many_files_in_the_plan_is_retried():
    many = PLAN_FILES + [{"path": f"app/m{i}.py", "purpose": "x"} for i in range(10)]
    _, llm = await run([plan(files=many), plan(), MODELS, TESTS, REQS])
    assert len(plan_calls(llm)) == 2


async def test_unsafe_planned_paths_never_get_code_and_are_flagged():
    evil = PLAN_FILES + [
        {"path": "../evil.py", "purpose": "bad"},
        {"path": "C:\\Windows\\evil.py", "purpose": "bad"},
        {"path": "app/run.exe", "purpose": "bad"},
    ]
    result, llm = await run([plan(files=evil), plan(files=evil), MODELS, TESTS, REQS])
    assert len(file_calls(llm)) == 3  # nothing was generated for the unsafe paths
    by_path = {f.path: f.content for f in result.output.files}
    assert by_path["../evil.py"] == "" and by_path["C:/Windows/evil.py"] == "" and by_path["app/run.exe"] == ""
    assert sum("Unsafe or invalid path" in i for i in result.quality_issues) == 3  # reported once each
    assert result.output.confidence <= 60


async def test_duplicate_planned_paths_are_written_once():
    dup = PLAN_FILES + [{"path": "requirements.txt", "purpose": "again"}]
    result, llm = await run([plan(files=dup), plan(files=dup), MODELS, TESTS, REQS])
    assert len(file_calls(llm)) == 3
    assert [f.path for f in result.output.files].count("requirements.txt") == 1


async def test_a_blocking_question_skips_writing_code():
    q = [{"question": "Refund to card or wallet?", "blocking": True}]
    result, llm = await run([plan(open_questions=q)])
    assert file_calls(llm) == [] and result.output.files == []
    assert result.output.readiness.value == "NotReady"
    assert result.output.open_questions[0].question == "Refund to card or wallet?"


async def test_an_unfixable_plan_still_produces_files_for_what_is_valid():
    bad = [{"path": "app/models.py", "purpose": "models"}]  # no tests, no requirements.txt, twice
    result, llm = await run([plan(files=bad), plan(files=bad), MODELS])
    assert len(file_calls(llm)) == 1
    joined = " ".join(result.quality_issues)
    assert "no pytest tests" in joined and "requirements.txt is missing" in joined


# ---------------- feedback ----------------
async def test_feedback_revises_files_using_their_previous_version():
    old = "OLD_MARKER = 1\n" + MODELS
    ctx = dict(CONTEXT, feedback=["Add a status column to bookings"],
               previous_output={"summary": "old", "files": [
                   {"path": "app/models.py", "purpose": "models", "content": old}]})
    result, llm = await run([plan(), MODELS, TESTS, REQS], ctx=ctx)
    plan_prompt = plan_calls(llm)[0]["user"]
    assert "Add a status column to bookings" in plan_prompt and "OLD_MARKER" not in plan_prompt  # plan step stays small
    models_prompt = file_calls(llm)[0]["user"]
    assert "Add a status column to bookings" in models_prompt
    assert "Your previous version of this file (revise it)" in models_prompt and "OLD_MARKER" in models_prompt
    assert "Your previous version of this file" not in file_calls(llm)[1]["user"]  # no old version of the tests file
    assert result.quality_issues == []


# ---------------- final check, example ----------------
def test_final_check_flags_a_design_table_that_no_file_mentions():
    out = DeveloperOutput.model_validate({
        "readiness": "Ready", "confidence": 80,
        "files": [{"path": "app/models.py", "content": 'class B:\n    __tablename__ = "bookings"\n'},
                  {"path": "tests/test_a.py", "content": "def test_a():\n    pass\n"},
                  {"path": "requirements.txt", "content": "pytest\n"}]})
    issues = DeveloperAgent(FakeLLM([])).check_quality(out, CONTEXT)
    assert issues == ["The design has table 'refunds' but no file mentions it."]


def test_developer_example_is_valid_and_clean():
    agent = DeveloperAgent(FakeLLM([]))
    planned = DeveloperPlan.model_validate(EXAMPLE_PLAN)
    assert agent.check_quality(planned, {}) == [] or agent.check_quality(planned, {}) == [
        "app/models.py is missing. It must hold one model per table of the design."]
    code = DeveloperOutput.model_validate(
        {"readiness": "Ready", "confidence": 70, "files": EXAMPLE_FILES})
    assert agent.check_quality(code, {"dba_output": EXAMPLE_TABLES}) == []
    for f in EXAMPLE_FILES:  # every example file passes the per-file check
        assert agent._file_problem(f["path"], f["content"], {}) is None
