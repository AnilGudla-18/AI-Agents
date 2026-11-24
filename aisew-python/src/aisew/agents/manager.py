import json
import re

from ..domain.outputs import ManagerOutput
from .base import BaseAgent
from .context import format_analysis

EXAMPLE_REQUIREMENT = "Users can export their order history as a CSV file"
EXAMPLE_OUTPUT = {
    "readiness": "ReadyWithClarifications",
    "confidence": 70,
    "summary": "A short plan to deliver the CSV export of order history.",
    "open_questions": [
        {"question": "Which fields must the CSV contain?", "blocking": False}
    ],
    "risks": ["Very large order histories may need the export to run in the background."],
    "tasks": [
        {"title": "Design the export query", "role": "backend developer", "effort": "S", "depends_on": []},
        {"title": "Build the CSV export endpoint", "role": "backend developer", "effort": "M",
         "depends_on": ["Design the export query"]},
        {"title": "Add an export button to the order history page", "role": "frontend developer",
         "effort": "S", "depends_on": ["Build the CSV export endpoint"]},
        {"title": "Test the export end to end", "role": "QA engineer", "effort": "M",
         "depends_on": ["Build the CSV export endpoint"]},
    ],
    "milestones": ["Export works end to end", "Export is tested"],
}


_TESTING = re.compile(r"\b(test|tests|testing|qa)\b", re.I)


def _is_testing(task) -> bool:
    return bool(_TESTING.search(f"{task.role} {task.title}"))


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        if any(dep in graph and visit(dep) for dep in graph[node]):
            return True
        state[node] = 2
        return False

    return any(visit(n) for n in graph)


class ManagerAgent(BaseAgent[ManagerOutput]):
    name = "Manager"
    output_model = ManagerOutput
    profile = (
        "You are a senior Delivery Manager. You turn an approved business analysis "
        "into a short, ordered delivery plan."
    )
    core_rules = [
        "Plan only the work needed for the approved analysis. Do not add features it does not mention.",
        "Each task has a short title, a role, an effort of S, M or L, and depends_on.",
        "Never give effort in hours, days or money. Only S, M or L.",
        "depends_on must contain the exact titles of other tasks in your plan.",
        "Include only tasks the analysis needs. No notifications, reporting, admin screens or other extras unless the analysis mentions them.",
        "All implementation tasks come before any testing task. Testing tasks depend on implementation tasks, never the other way round.",
        "Order the work sensibly: data and backend first, then user interface, then testing and release, where relevant.",
        "Give 2 to 4 short milestone names.",
        "If an unanswered question changes the plan, raise it in open_questions instead of guessing.",
    ]
    examples = f"Requirement: {EXAMPLE_REQUIREMENT}\nAnswer:\n{json.dumps(EXAMPLE_OUTPUT, indent=2)}"

    def build_user_prompt(self, context: dict) -> str:
        base = super().build_user_prompt(context)
        return f"{base}\n\nApproved business analysis (plan the delivery of this):\n{format_analysis(context['ba_output'])}"

    def check_quality(self, output: ManagerOutput, context: dict) -> list[str]:
        if not output.tasks:
            return ["No tasks were planned."]
        issues: list[str] = []
        titles = [t.title for t in output.tasks]
        dupes = sorted({t for t in titles if titles.count(t) > 1})
        if dupes:
            issues.append(f"Task titles must be unique. Duplicates: {', '.join(dupes)}.")
        for t in output.tasks:
            for dep in t.depends_on:
                if dep == t.title:
                    issues.append(f"Task '{t.title}' depends on itself.")
                elif dep not in titles:
                    issues.append(f"Task '{t.title}' depends on '{dep}', which is not a task title in your plan.")
        by_title = {t.title: t for t in output.tasks}
        for t in output.tasks:
            if _is_testing(t):
                continue
            for dep in t.depends_on:
                if dep in by_title and _is_testing(by_title[dep]):
                    issues.append(
                        f"Implementation task '{t.title}' depends on testing task '{dep}'. "
                        "Testing must come after implementation."
                    )
        if _has_cycle({t.title: t.depends_on for t in output.tasks}):
            issues.append("The task dependencies contain a cycle. Remove the circular dependency.")
        return issues
