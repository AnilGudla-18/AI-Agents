import json
import re

from ..domain.outputs import DBAOutput
from .base import BaseAgent

EXAMPLE_ANALYSIS = "Customers can place orders for products"
EXAMPLE_OUTPUT = {
    "readiness": "ReadyWithClarifications",
    "confidence": 75,
    "summary": "A minimal schema for customers and their orders.",
    "open_questions": [
        {"question": "Should the price be stored per order line or only as an order total?", "blocking": False}
    ],
    "risks": [],
    "tables": [
        {
            "name": "customers",
            "purpose": "People who place orders",
            "columns": [
                {"name": "id", "type": "uuid", "nullable": False, "primary_key": True, "references": None},
                {"name": "email", "type": "text", "nullable": False, "primary_key": False, "references": None},
                {"name": "created_at", "type": "timestamp", "nullable": False, "primary_key": False, "references": None},
            ],
        },
        {
            "name": "orders",
            "purpose": "One row per order placed by a customer",
            "columns": [
                {"name": "id", "type": "uuid", "nullable": False, "primary_key": True, "references": None},
                {"name": "customer_id", "type": "uuid", "nullable": False, "primary_key": False, "references": "customers.id"},
                {"name": "status", "type": "text", "nullable": False, "primary_key": False, "references": None},
                {"name": "created_at", "type": "timestamp", "nullable": False, "primary_key": False, "references": None},
            ],
        },
    ],
    "relationships": [],
    "indexes": ["orders(customer_id)"],
}


def _names(out: DBAOutput) -> str:
    return " ".join([t.name for t in out.tables] + [c.name for t in out.tables for c in t.columns])


def _has_time_column(out: DBAOutput) -> bool:
    return any(
        re.search(r"(time|date|deadline|expir|_at$)", c.name, re.I)
        and c.name not in ("created_at", "updated_at")
        for t in out.tables
        for c in t.columns
    )


# (pattern found in the analysis, test the schema must pass, message if it fails)
SCHEMA_HINTS = [
    (
        re.compile(r"\bcancel", re.I),
        lambda o: re.search(r"cancel|status|state", _names(o), re.I),
        "The analysis mentions cancellation, but nothing in the schema can record that something "
        "was cancelled (for example a status column).",
    ),
    (
        re.compile(r"\brefund", re.I),
        lambda o: re.search(r"refund", _names(o), re.I),
        "The analysis mentions refunds, but nothing in the schema records a refund "
        "(for example a refunds table).",
    ),
    (
        re.compile(r"\b(hours?|days?|minutes?|weeks?|deadline|expir\w*)\b", re.I),
        _has_time_column,
        "A business rule depends on time, but no table has a date/time column other than created_at "
        "(for example start_time).",
    ),
]


class DBAAgent(BaseAgent[DBAOutput]):
    name = "DBA"
    output_model = DBAOutput
    profile = (
        "You are a senior Database Architect. You design a clean relational schema "
        "from an approved business analysis. You design only what is needed."
    )
    core_rules = [
        "Use snake_case and plural table names. Every table needs a primary key, and primary keys are never nullable.",
        "Declare foreign keys in the column's references field as 'table.column'. The referenced table and column MUST exist in your design.",
        "Every table needs a one-sentence purpose.",
        "Use database-neutral types: integer, text, decimal, boolean, timestamp, uuid.",
        "Add created_at to every table.",
        "Index every foreign key column, written as 'table(column)' in indexes.",
        "Design ONLY what the approved analysis needs. Do not add tables or columns for features it does not mention.",
        "If the analysis leaves open a detail that affects the schema (for example partial or full refund, payment method), do not guess: add an open_question and keep the schema minimal.",
        "Leave relationships as an empty list; it is generated from the references.",
    ]
    examples = f"Analysis: {EXAMPLE_ANALYSIS}\nAnswer:\n{json.dumps(EXAMPLE_OUTPUT, indent=2)}"

    def build_user_prompt(self, context: dict) -> str:
        base = super().build_user_prompt(context)
        ba = context["ba_output"]
        analysis = {k: ba.get(k, []) for k in ("user_stories", "business_rules", "acceptance_criteria")}
        text = f"{base}\n\nApproved business analysis (design the database for this):\n{json.dumps(analysis, indent=2)}"
        questions = [q["question"] for q in ba.get("open_questions", [])]
        if questions:
            qs = "\n".join(f"- {q}" for q in questions)
            text += (
                "\n\nQuestions still unanswered in the analysis (do NOT assume answers; "
                f"if they affect the schema, raise them as your own open_questions):\n{qs}"
            )
        return text

    def check_quality(self, output: DBAOutput, context: dict) -> list[str]:
        issues: list[str] = []
        if not output.tables:
            return ["No tables were designed."]
        columns = {t.name: {c.name for c in t.columns} for t in output.tables}
        for t in output.tables:
            if not any(c.primary_key for c in t.columns):
                issues.append(f"Table '{t.name}' has no primary key column.")
            if not t.purpose.strip():
                issues.append(f"Table '{t.name}' has no purpose sentence.")
            for c in t.columns:
                if c.references:
                    table, _, col = c.references.partition(".")
                    if col not in columns.get(table, set()):
                        issues.append(
                            f"{t.name}.{c.name} references '{c.references}', which does not exist in your design."
                        )
        ba = context.get("ba_output") or {}
        analysis = " ".join([*ba.get("business_rules", []), *ba.get("acceptance_criteria", [])])
        for pattern, schema_ok, message in SCHEMA_HINTS:
            if pattern.search(analysis) and not schema_ok(output):
                issues.append(message)
        return issues
