import json

from ..domain.outputs import QAOutput
from .base import BaseAgent
from .context import format_analysis

EXAMPLE_OUTPUT = {
    "readiness": "ReadyWithClarifications",
    "confidence": 70,
    "summary": "Tests for exporting order history as a CSV file.",
    "open_questions": [
        {"question": "Which fields must the CSV contain?", "blocking": False}
    ],
    "risks": [],
    "test_cases": [
        {
            "title": "Export order history",
            "kind": "functional",
            "covers": [1],
            "steps": ["Sign in as a user with past orders", "Request an export of the order history"],
            "expected": "A CSV file containing the user's orders is downloaded",
        },
        {
            "title": "Export with no orders",
            "kind": "edge",
            "covers": [2],
            "steps": ["Sign in as a user with no orders", "Request an export of the order history"],
            "expected": "The user is told there is nothing to export",
        },
        {
            "title": "Export another user's history",
            "kind": "negative",
            "covers": [],
            "steps": ["Sign in as one user", "Request an export of a different user's order history"],
            "expected": "The request is refused and no file is produced",
        },
    ],
}
# The example refers to these two acceptance criteria (see agents/ba.py EXAMPLE_OUTPUT).
EXAMPLE_ACCEPTANCE_CRITERIA = [
    "Given a user with past orders, when they request an export, then they receive a CSV file containing those orders",
    "Given a user with no orders, when they request an export, then they are told there is nothing to export",
]


class QAAgent(BaseAgent[QAOutput]):
    name = "QA"
    output_model = QAOutput
    profile = (
        "You are a senior QA Engineer. You turn an approved business analysis into "
        "a clear, traceable test plan."
    )
    core_rules = [
        "Write test cases only for behaviour stated in the acceptance criteria and business rules.",
        "Never invent amounts, limits or dates. Describe test data in words, for example 'a valid booking'.",
        "Every acceptance criterion must be covered by at least one test case. List its number in covers (AC1 -> 1).",
        "Start with exactly one test case per acceptance criterion (AC1 -> covers [1], AC2 -> covers [2], ...). Then add negative and edge tests.",
        "Do not test behaviour the criteria do not describe (for example partial refunds or payment methods) unless a criterion mentions it.",
        "Include at least one negative test (kind=negative) and, where sensible, edge tests (kind=edge).",
        "Each test case needs a title, kind, covers, short imperative steps, and an expected result.",
        "If an unanswered question changes what the expected result should be, raise it in open_questions instead of guessing.",
    ]
    examples = (
        "Acceptance criteria:\n"
        + "\n".join(f"AC{i}: {c}" for i, c in enumerate(EXAMPLE_ACCEPTANCE_CRITERIA, 1))
        + f"\nAnswer:\n{json.dumps(EXAMPLE_OUTPUT, indent=2)}"
    )

    def build_user_prompt(self, context: dict) -> str:
        base = super().build_user_prompt(context)
        return f"{base}\n\nApproved business analysis (write the test plan for this):\n{format_analysis(context['ba_output'])}"

    def check_quality(self, output: QAOutput, context: dict) -> list[str]:
        if not output.test_cases:
            return ["No test cases were written."]
        issues: list[str] = []
        total = len(context["ba_output"].get("acceptance_criteria", []))

        covered = {n for t in output.test_cases for n in t.covers}
        out_of_range = sorted(n for n in covered if n < 1 or n > total)
        if out_of_range:
            issues.append(
                f"covers refers to acceptance criteria that do not exist: {out_of_range}. "
                f"Valid numbers are 1 to {total}."
            )
        missing = [n for n in range(1, total + 1) if n not in covered]
        if missing:
            issues.append(
                "These acceptance criteria are not covered by any test case: "
                + ", ".join(f"AC{n}" for n in missing)
                + ". Add test cases and list their numbers in covers."
            )
        if not any(t.kind == "negative" for t in output.test_cases):
            issues.append("Add at least one negative test (kind=negative): invalid or unexpected input.")
        incomplete = [t.title for t in output.test_cases if not t.steps or not t.expected.strip()]
        if incomplete:
            issues.append("These test cases need steps and an expected result: " + ", ".join(incomplete))
        return issues
