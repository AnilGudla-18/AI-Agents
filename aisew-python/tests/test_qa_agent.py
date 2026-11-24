import json

from aisew.agents.qa import EXAMPLE_ACCEPTANCE_CRITERIA, EXAMPLE_OUTPUT, QAAgent
from aisew.domain.outputs import QAOutput
from aisew.llm.fake import FakeLLM

BA = {
    "user_stories": [],
    "business_rules": [],
    "acceptance_criteria": ["Given X, then Y", "Given A, then B"],
    "open_questions": [],
}
CONTEXT = {"requirement": "Cancel a booking", "ba_output": BA}


def case(title, covers, kind="functional", steps=("do it",), expected="it works"):
    return {"title": title, "kind": kind, "covers": covers, "steps": list(steps), "expected": expected}


def reply(cases=None, **o) -> str:
    d = {
        "readiness": "Ready", "confidence": 80, "summary": "tests", "open_questions": [], "risks": [],
        "test_cases": cases if cases is not None else [
            case("t1", [1]), case("t2", [2]), case("t3", [], kind="negative")],
    }
    d.update(o)
    return json.dumps(d)


async def run(responses):
    llm = FakeLLM(responses)
    return await QAAgent(llm).run(CONTEXT), llm


async def test_valid_plan():
    result, _ = await run([reply()])
    assert result.attempts == 1
    assert len(result.output.test_cases) == 3


async def test_uncovered_acceptance_criterion_triggers_retry():
    partial = [case("t1", [1]), case("t3", [], kind="negative")]
    result, llm = await run([reply(cases=partial), reply()])
    assert result.attempts == 2
    assert "AC2" in llm.calls[1]["user"]


async def test_missing_negative_test_flagged():
    no_neg = [case("t1", [1]), case("t2", [2])]
    result, llm = await run([reply(cases=no_neg), reply()])
    assert result.attempts == 2
    assert "negative test" in llm.calls[1]["user"]


async def test_out_of_range_covers_flagged():
    bad = [case("t1", [1, 7]), case("t2", [2]), case("t3", [], kind="negative")]
    result, _ = await run([reply(cases=bad), reply(cases=bad)])
    assert any("do not exist" in i for i in result.quality_issues)


async def test_incomplete_test_case_flagged():
    bad = [case("t1", [1], steps=()), case("t2", [2]), case("t3", [], kind="negative")]
    result, llm = await run([reply(cases=bad), reply()])
    assert result.attempts == 2
    assert "t1" in llm.calls[1]["user"]


async def test_covers_and_kind_are_coerced():
    cases = [
        {"title": "t1", "kind": "Negative test", "covers": ["AC1", "2"], "steps": ["x"], "expected": "y"},
    ]
    result, _ = await run([reply(cases=cases)])
    tc = result.output.test_cases[0]
    assert tc.covers == [1, 2]
    assert tc.kind == "negative"


async def test_prompt_numbers_acceptance_criteria():
    _, llm = await run([reply()])
    assert "AC1: Given X, then Y" in llm.calls[0]["user"]
    assert "AC2: Given A, then B" in llm.calls[0]["user"]


def test_qa_example_is_valid_and_clean():
    out = QAOutput.model_validate(EXAMPLE_OUTPUT)
    ba = {"acceptance_criteria": EXAMPLE_ACCEPTANCE_CRITERIA}
    assert QAAgent(FakeLLM([])).check_quality(out, {"requirement": "x", "ba_output": ba}) == []
