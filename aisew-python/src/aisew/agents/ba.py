import json
import re

from ..domain.outputs import BAOutput
from .base import BaseAgent

SHORT_REQUIREMENT_WORDS = 20

# "within 24 hours" / "after 24 hours" without saying what the time is measured from.
AMBIGUOUS_TIME = re.compile(
    r"\b(?:after|within)\s+\d+\s+(?:hours?|days?|minutes?|weeks?)\b"
    r"(?!\s+(?:of|before|after|from|since)\b)",
    re.I,
)

EXAMPLE_REQUIREMENT = "Users can export their order history as a CSV file"
EXAMPLE_OUTPUT = {
    "readiness": "ReadyWithClarifications",
    "confidence": 70,
    "summary": "Users want to download their past orders as a CSV file.",
    "open_questions": [
        {"question": "Which fields must the CSV contain (for example order date, items, total)?", "blocking": False},
        {"question": "Can users filter the export by date range, or is it always the full history?", "blocking": False},
        {"question": "Is the export available to every user or only to certain account types?", "blocking": False},
    ],
    "risks": ["Very large order histories may make the export slow."],
    "user_stories": [
        {
            "as_a": "registered user",
            "i_want": "to export my order history as a CSV file",
            "so_that": "I can keep or analyse my past orders outside the application",
        }
    ],
    "business_rules": ["The export contains only the requesting user's own order history"],
    "acceptance_criteria": [
        "Given a user with past orders, when they request an export, then they receive a CSV file containing those orders",
        "Given a user with no orders, when they request an export, then they are told there is nothing to export",
    ],
}


def _source_text(context: dict) -> str:
    parts = [context["requirement"]]
    for c in context.get("clarifications") or []:
        parts += [c["question"], c["answer"]]
    return " ".join(parts)


class BusinessAnalystAgent(BaseAgent[BAOutput]):
    name = "BA"
    output_model = BAOutput
    profile = (
        "You are a senior Business Analyst. You turn a raw requirement into clear "
        "user stories, business rules and testable acceptance criteria. "
        "You are careful and you ask questions instead of guessing."
    )
    core_rules = [
        "Use ONLY facts stated in the requirement or in the answered clarifications.",
        "Never add amounts, percentages, time limits, deadlines, fees, quotas or any other numbers or policies that were not given.",
        "Anything not stated (amounts, time limits, who is allowed, exceptions, payment methods, notifications) belongs in open_questions, NOT in business_rules.",
        "A one-line requirement almost always has gaps: ask 2 to 4 specific, answerable questions.",
        "Gaps in details are blocking=false. Use blocking=true only when the core goal itself is unclear.",
        "business_rules: only rules that are stated or directly implied. If there are none, return an empty list.",
        "acceptance_criteria: testable statements in the form 'Given ..., when ..., then ...'. Never start them with 'As a'.",
        "user_stories: as_a, i_want, so_that, each with a clear benefit.",
        "summary: one sentence restating the requirement. It must not be empty.",
        "State time rules unambiguously: say what the time is measured from and in which direction, for example 'at least 24 hours before the booking starts' or 'less than 24 hours before it starts'. Never write just 'within 24 hours' or 'after 24 hours'.",
    ]
    examples = f"Requirement: {EXAMPLE_REQUIREMENT}\nAnswer:\n{json.dumps(EXAMPLE_OUTPUT, indent=2)}"

    def check_quality(self, output: BAOutput, context: dict) -> list[str]:
        issues: list[str] = []
        source = _source_text(context)

        if not output.summary.strip():
            issues.append("summary is empty; write one sentence restating the requirement.")
        if not output.user_stories:
            issues.append("There are no user_stories.")
        if not output.acceptance_criteria:
            issues.append("There are no acceptance_criteria.")

        bad = [c for c in output.acceptance_criteria if re.match(r"\s*as an?\b", c, re.I)]
        if bad:
            issues.append(
                "acceptance_criteria must be testable 'Given/when/then' statements, "
                f"not user stories. Rewrite: {bad[0]!r}"
            )

        allowed = set(re.findall(r"\d+", source))
        written = " ".join([*output.business_rules, *output.acceptance_criteria])
        invented = sorted(set(re.findall(r"\d+", written)) - allowed)
        if invented:
            issues.append(
                f"These numbers were not given in the requirement: {', '.join(invented)}. "
                "Remove them, or ask about them in open_questions."
            )

        vague = [t for t in [*output.business_rules, *output.acceptance_criteria] if AMBIGUOUS_TIME.search(t)]
        if vague:
            issues.append(
                "Ambiguous time rule: say what the time is measured from, for example "
                f"'at least 24 hours before the booking starts'. Rewrite: {vague[0]!r}"
            )

        short = len(context["requirement"].split()) < SHORT_REQUIREMENT_WORDS
        if short and not context.get("clarifications") and not output.open_questions:
            issues.append(
                "The requirement is short and certainly has gaps, but open_questions is empty. "
                "Ask 2 to 4 specific questions about what is not stated."
            )
        return issues
