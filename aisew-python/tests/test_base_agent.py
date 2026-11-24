import json

import pytest

from aisew.agents.ba import EXAMPLE_OUTPUT, EXAMPLE_REQUIREMENT, BusinessAnalystAgent
from aisew.agents.base import AgentOutputError
from aisew.domain.enums import Readiness
from aisew.domain.outputs import BAOutput
from aisew.llm.fake import FakeLLM

LONG_REQ = (
    "Customers must be able to cancel an existing booking from their account page "
    "and the system must then return the money they paid for that booking"
)


def reply(**overrides) -> str:
    data = {
        "readiness": "Ready",
        "confidence": 90,
        "summary": "Cancel a booking and get a refund",
        "open_questions": [],
        "risks": [],
        "user_stories": [{"as_a": "user", "i_want": "to cancel", "so_that": "I get a refund"}],
        "business_rules": ["A cancelled booking is refunded"],
        "acceptance_criteria": ["Given a booking, when it is cancelled, then a refund is issued"],
    }
    data.update(overrides)
    return json.dumps(data)


async def run(responses, requirement=LONG_REQ, **kw):
    llm = FakeLLM(responses)
    agent = BusinessAnalystAgent(llm, **kw)
    return await agent.run({"requirement": requirement}), llm


# ---------- JSON handling ----------
async def test_valid_json():
    result, _ = await run([reply()])
    assert result.attempts == 1
    assert result.output.readiness == Readiness.READY
    assert result.output.user_stories[0].i_want == "to cancel"


async def test_code_fenced_json_is_parsed():
    result, _ = await run([f"```json\n{reply()}\n```"])
    assert result.output.confidence == 90


async def test_malformed_then_valid_retries_once():
    result, llm = await run(["not json at all", reply()])
    assert result.attempts == 2
    assert "previous reply was invalid" in llm.calls[1]["user"]


async def test_malformed_twice_raises():
    with pytest.raises(AgentOutputError):
        await run(["nope", "still nope"])


# ---------- confidence + readiness rules ----------
async def test_confidence_is_clamped_and_capped():
    result, _ = await run([reply(confidence=150)])
    assert result.output.confidence == 95  # nobody claims certainty
    result, _ = await run([reply(confidence=-5)])
    assert result.output.confidence == 0


async def test_blocking_question_forces_not_ready_and_low_confidence():
    qs = [{"question": "Which gateway?", "blocking": True}]
    result, _ = await run([reply(open_questions=qs)])
    assert result.output.readiness == Readiness.NOT_READY
    assert result.output.confidence <= 50


async def test_non_blocking_question_downgrades_ready():
    result, _ = await run([reply(open_questions=["Refund to wallet or card?"])])
    assert result.output.readiness == Readiness.READY_WITH_CLARIFICATIONS
    assert result.output.confidence <= 80
    assert result.output.open_questions[0].question == "Refund to wallet or card?"


# ---------- prompts ----------
async def test_system_prompt_has_rules_and_example():
    _, llm = await run([reply()])
    system = llm.calls[0]["system"]
    assert "Never invent" in system
    assert "Never add amounts" in system
    assert EXAMPLE_REQUIREMENT in system


async def test_clarifications_are_included_and_marked_do_not_reask():
    llm = FakeLLM([reply()])
    agent = BusinessAnalystAgent(llm)
    await agent.run(
        {"requirement": LONG_REQ, "clarifications": [{"question": "Gateway?", "answer": "Stripe"}]}
    )
    assert "Q: Gateway?" in llm.calls[0]["user"]
    assert "A: Stripe" in llm.calls[0]["user"]
    assert "do not re-ask" in llm.calls[0]["user"]


# ---------- quality checks (BA) ----------
async def test_invented_numbers_trigger_retry_with_feedback():
    bad = reply(acceptance_criteria=["Given a booking, when cancelled, then refund within 3 business days"])
    result, llm = await run([bad, reply()])
    assert result.attempts == 2
    assert result.quality_issues == []
    assert "quality problems" in llm.calls[1]["user"]
    assert "3" in llm.calls[1]["user"]


async def test_number_given_in_requirement_is_allowed():
    req = LONG_REQ + " and the refund must arrive within 3 days of the cancellation request"
    ok = reply(acceptance_criteria=["Given a cancelled booking, then the refund arrives within 3 days of the cancellation"])
    result, _ = await run([ok], requirement=req)
    assert result.attempts == 1


async def test_acceptance_criteria_written_as_user_story_is_flagged():
    bad = reply(acceptance_criteria=["As a customer, I should see my booking"])
    result, llm = await run([bad, reply()])
    assert result.attempts == 2
    assert "Given/when/then" in llm.calls[1]["user"]


async def test_empty_summary_is_flagged():
    result, _ = await run([reply(summary=""), reply()])
    assert result.attempts == 2


async def test_short_requirement_without_questions_is_flagged():
    questions = ["Full or partial refund?", "Is there a cancellation deadline?"]
    result, llm = await run([reply(), reply(open_questions=questions)], requirement="Cancel booking")
    assert result.attempts == 2
    assert "open_questions is empty" in llm.calls[1]["user"]
    assert len(result.output.open_questions) == 2


async def test_short_requirement_ok_when_already_clarified():
    llm = FakeLLM([reply()])
    agent = BusinessAnalystAgent(llm)
    result = await agent.run(
        {"requirement": "Cancel booking", "clarifications": [{"question": "Q", "answer": "A"}]}
    )
    assert result.attempts == 1


async def test_unresolved_quality_issues_are_surfaced_not_hidden():
    bad = reply(summary="")
    result, _ = await run([bad, bad])
    assert result.quality_issues
    assert any("Quality check failed" in r for r in result.output.risks)
    assert result.output.confidence <= 60
    assert result.output.readiness == Readiness.READY_WITH_CLARIFICATIONS


async def test_valid_output_with_issues_survives_a_later_garbage_reply():
    result, _ = await run([reply(summary=""), "garbage"])
    assert result.output.user_stories
    assert result.quality_issues


def test_ba_example_is_valid_and_passes_its_own_checks():
    out = BAOutput.model_validate(EXAMPLE_OUTPUT)
    agent = BusinessAnalystAgent(FakeLLM([]))
    assert agent.check_quality(out, {"requirement": EXAMPLE_REQUIREMENT}) == []


# ---------- unambiguous time rules ----------
TIME_REQ = LONG_REQ + " and a full refund applies when the cancellation is at least 24 hours before the start"


async def test_ambiguous_time_rule_is_flagged():
    bad = reply(acceptance_criteria=["Given a booking, when they cancel within 24 hours, then a full refund is issued"])
    result, llm = await run([bad, reply()], requirement=TIME_REQ)
    assert result.attempts == 2
    assert "Ambiguous time rule" in llm.calls[1]["user"]


async def test_clear_time_rule_passes():
    ok = reply(acceptance_criteria=["Given a booking, when it is cancelled at least 24 hours before it starts, then a full refund is issued"])
    result, _ = await run([ok], requirement=TIME_REQ)
    assert result.attempts == 1


# ---------- prompt sections ----------
async def test_confirmed_facts_feedback_and_previous_output_reach_the_prompt():
    llm = FakeLLM([reply()])
    agent = BusinessAnalystAgent(llm)
    await agent.run({
        "requirement": LONG_REQ,
        "ba_clarifications": [{"question": "Refund method?", "answer": "Original payment method"}],
        "feedback": ["Remove the notification rule"],
        "previous_output": {"summary": "old answer"},
    })
    user = llm.calls[0]["user"]
    assert "Facts confirmed by the business" in user and "Original payment method" in user
    assert "Apply EVERY point" in user and "Remove the notification rule" in user
    assert "old answer" in user and "do not start from scratch" in user
