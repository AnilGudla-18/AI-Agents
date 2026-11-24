import json

from aisew.agents.dba import EXAMPLE_ANALYSIS, EXAMPLE_OUTPUT, DBAAgent
from aisew.domain.outputs import DBAOutput
from aisew.llm.fake import FakeLLM

BA = {
    "user_stories": [{"as_a": "customer", "i_want": "cancel", "so_that": "refund"}],
    "business_rules": ["A customer can create a booking"],
    "acceptance_criteria": ["Given a customer, when they book, then a booking exists"],
    "open_questions": [{"question": "Full or partial refund?", "blocking": False}],
}
CONTEXT = {"requirement": "Customers can cancel a booking", "ba_output": BA}


def col(name, type="text", pk=False, nullable=True, references=None):
    return {"name": name, "type": type, "primary_key": pk, "nullable": nullable, "references": references}


def reply(tables=None, **o) -> str:
    d = {
        "readiness": "Ready", "confidence": 80, "summary": "schema", "open_questions": [],
        "risks": [], "relationships": ["junk -> null"], "indexes": [],
        "tables": tables if tables is not None else [
            {"name": "customers", "purpose": "People", "columns": [col("id", "uuid", pk=True)]},
            {"name": "bookings", "purpose": "Bookings", "columns": [
                col("id", "uuid", pk=True), col("customer_id", "uuid", references="customers.id")]},
        ],
    }
    d.update(o)
    return json.dumps(d)


async def run(responses):
    llm = FakeLLM(responses)
    return await DBAAgent(llm).run(CONTEXT), llm


async def test_primary_keys_are_forced_non_nullable():
    result, _ = await run([reply()])
    for t in result.output.tables:
        for c in t.columns:
            if c.primary_key:
                assert c.nullable is False


async def test_relationships_are_derived_and_junk_dropped():
    result, _ = await run([reply()])
    assert result.output.relationships == ["bookings.customer_id -> customers.id"]


async def test_null_like_and_colon_references_are_normalised():
    tables = [
        {"name": "customers", "purpose": "P", "columns": [col("id", pk=True), col("x", references="null")]},
        {"name": "bookings", "purpose": "B", "columns": [col("id", pk=True), col("c", references="customers:id")]},
    ]
    result, _ = await run([reply(tables=tables)])
    cols = {c.name: c for t in result.output.tables for c in t.columns}
    assert cols["x"].references is None
    assert cols["c"].references == "customers.id"
    assert result.attempts == 1


async def test_dangling_reference_triggers_retry():
    bad = [{"name": "bookings", "purpose": "B", "columns": [col("id", pk=True), col("c", references="ghosts.id")]}]
    result, llm = await run([reply(tables=bad), reply()])
    assert result.attempts == 2
    assert "does not exist in your design" in llm.calls[1]["user"]


async def test_table_without_primary_key_is_flagged():
    bad = [{"name": "bookings", "purpose": "B", "columns": [col("name")]}]
    result, _ = await run([reply(tables=bad), reply(tables=bad)])
    assert any("no primary key" in i for i in result.quality_issues)
    assert result.output.confidence <= 60


async def test_missing_purpose_is_flagged():
    bad = [{"name": "bookings", "purpose": "", "columns": [col("id", pk=True)]}]
    result, llm = await run([reply(tables=bad), reply()])
    assert result.attempts == 2
    assert "no purpose" in llm.calls[1]["user"]


async def test_ba_open_questions_are_passed_on_to_dba():
    _, llm = await run([reply()])
    assert "Full or partial refund?" in llm.calls[0]["user"]
    assert "do NOT assume answers" in llm.calls[0]["user"]


def test_dba_example_is_valid_and_passes_its_own_checks():
    out = DBAOutput.model_validate(EXAMPLE_OUTPUT)
    assert DBAAgent(FakeLLM([])).check_quality(out, {}) == []
    assert out.relationships == ["orders.customer_id -> customers.id"]
    assert EXAMPLE_ANALYSIS in DBAAgent.examples


# ---------- schema hints: the schema must be able to hold what the rules talk about ----------
RULES_BA = {
    "user_stories": [],
    "business_rules": ["A booking can be cancelled at least 24 hours before it starts for a full refund"],
    "acceptance_criteria": [],
    "open_questions": [],
}
GOOD_TABLES = [
    {"name": "bookings", "purpose": "Bookings", "columns": [
        col("id", "uuid", pk=True), col("status"), col("start_time", "timestamp")]},
    {"name": "refunds", "purpose": "Refunds", "columns": [
        col("id", "uuid", pk=True), col("booking_id", "uuid", references="bookings.id")]},
]
BARE_TABLES = [{"name": "bookings", "purpose": "Bookings", "columns": [col("id", "uuid", pk=True)]}]


async def run_rules(responses):
    llm = FakeLLM(responses)
    return await DBAAgent(llm).run({"requirement": "x", "ba_output": RULES_BA}), llm


async def test_schema_missing_refunds_cancellation_and_time_is_retried():
    result, llm = await run_rules([reply(tables=BARE_TABLES), reply(tables=GOOD_TABLES)])
    assert result.attempts == 2
    feedback = llm.calls[1]["user"]
    assert "cancellation" in feedback and "refund" in feedback and "date/time column" in feedback


async def test_schema_that_covers_the_rules_passes_first_time():
    result, _ = await run_rules([reply(tables=GOOD_TABLES)])
    assert result.attempts == 1 and result.quality_issues == []


async def test_created_at_alone_does_not_satisfy_a_time_rule():
    only_created = [
        {"name": "bookings", "purpose": "B", "columns": [col("id", pk=True), col("status"), col("created_at")]},
        {"name": "refunds", "purpose": "R", "columns": [col("id", pk=True)]},
    ]
    result, _ = await run_rules([reply(tables=only_created), reply(tables=only_created)])
    assert any("date/time column" in i for i in result.quality_issues)
