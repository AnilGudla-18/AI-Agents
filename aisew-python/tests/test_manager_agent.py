import json

from aisew.agents.manager import EXAMPLE_OUTPUT, EXAMPLE_REQUIREMENT, ManagerAgent
from aisew.domain.outputs import ManagerOutput
from aisew.llm.fake import FakeLLM

BA = {
    "user_stories": [{"as_a": "customer", "i_want": "cancel", "so_that": "refund"}],
    "business_rules": ["A cancelled booking is refunded"],
    "acceptance_criteria": ["Given a booking, when cancelled, then refunded"],
    "open_questions": [{"question": "Full or partial refund?", "blocking": False}],
}
CONTEXT = {"requirement": "Customers can cancel a booking", "ba_output": BA}


def task(title, deps=(), effort="M"):
    return {"title": title, "role": "backend developer", "effort": effort, "depends_on": list(deps)}


def reply(tasks=None, **o) -> str:
    d = {
        "readiness": "Ready", "confidence": 80, "summary": "plan", "open_questions": [], "risks": [],
        "tasks": tasks if tasks is not None else [task("Build API"), task("Build UI", ["Build API"])],
        "milestones": ["Works end to end"],
    }
    d.update(o)
    return json.dumps(d)


async def run(responses):
    llm = FakeLLM(responses)
    return await ManagerAgent(llm).run(CONTEXT), llm


async def test_valid_plan():
    result, _ = await run([reply()])
    assert result.attempts == 1
    assert [t.title for t in result.output.tasks] == ["Build API", "Build UI"]


async def test_effort_is_normalised_to_s_m_l():
    tasks = [task("a", effort="small"), task("b", effort="Large"), task("c", effort="2 days")]
    result, _ = await run([reply(tasks=tasks)])
    assert [t.effort for t in result.output.tasks] == ["S", "L", "M"]


async def test_dangling_dependency_triggers_retry():
    bad = [task("Build API", ["Ghost task"])]
    result, llm = await run([reply(tasks=bad), reply()])
    assert result.attempts == 2
    assert "not a task title" in llm.calls[1]["user"]


async def test_cycle_is_flagged():
    cyc = [task("A", ["B"]), task("B", ["A"])]
    result, _ = await run([reply(tasks=cyc), reply(tasks=cyc)])
    assert any("cycle" in i for i in result.quality_issues)
    assert result.output.confidence <= 60


async def test_duplicate_titles_flagged():
    dup = [task("A"), task("A")]
    result, llm = await run([reply(tasks=dup), reply()])
    assert result.attempts == 2
    assert "unique" in llm.calls[1]["user"]


async def test_no_tasks_flagged():
    result, _ = await run([reply(tasks=[]), reply()])
    assert result.attempts == 2


async def test_prompt_contains_numbered_criteria_and_unanswered_questions():
    _, llm = await run([reply()])
    user = llm.calls[0]["user"]
    assert "AC1: Given a booking" in user
    assert "Full or partial refund?" in user
    assert "do NOT assume answers" in user


def test_manager_example_is_valid_and_clean():
    out = ManagerOutput.model_validate(EXAMPLE_OUTPUT)
    assert ManagerAgent(FakeLLM([])).check_quality(out, CONTEXT) == []
    assert EXAMPLE_REQUIREMENT in ManagerAgent.examples


def role_task(title, role, deps=()):
    return {"title": title, "role": role, "effort": "M", "depends_on": list(deps)}


async def test_implementation_depending_on_testing_is_flagged():
    wrong = [
        role_task("Build endpoint", "backend developer"),
        role_task("Test the endpoint", "QA engineer", ["Build endpoint"]),
        role_task("Implement refunds", "backend developer", ["Test the endpoint"]),
    ]
    result, llm = await run([reply(tasks=wrong), reply()])
    assert result.attempts == 2
    assert "Testing must come after implementation" in llm.calls[1]["user"]


async def test_testing_depending_on_implementation_is_fine():
    right = [
        role_task("Build endpoint", "backend developer"),
        role_task("Test the endpoint", "QA engineer", ["Build endpoint"]),
        role_task("End to end tests", "QA engineer", ["Test the endpoint"]),
    ]
    result, _ = await run([reply(tasks=right)])
    assert result.attempts == 1
