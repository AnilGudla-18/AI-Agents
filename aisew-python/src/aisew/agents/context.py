"""Shared formatting of the approved business analysis for downstream agents."""


def format_analysis(ba: dict) -> str:
    stories = "\n".join(
        f"- As {s.get('as_a', 'a user')}, I want {s.get('i_want', '')}, so that {s.get('so_that', '')}"
        for s in ba.get("user_stories", [])
    ) or "- (none)"
    rules = "\n".join(f"- {r}" for r in ba.get("business_rules", [])) or "- (none stated)"
    criteria = "\n".join(
        f"AC{i}: {c}" for i, c in enumerate(ba.get("acceptance_criteria", []), 1)
    ) or "(none)"
    text = (
        f"User stories:\n{stories}\n\nBusiness rules:\n{rules}\n\n"
        f"Acceptance criteria (refer to them by number):\n{criteria}"
    )
    questions = [q["question"] for q in ba.get("open_questions", [])]
    if questions:
        qs = "\n".join(f"- {q}" for q in questions)
        text += (
            "\n\nQuestions still unanswered in the analysis (do NOT assume answers; "
            f"if they matter, raise them as your own open_questions):\n{qs}"
        )
    return text


def format_schema(dba: dict) -> str:
    lines = []
    for t in dba.get("tables", []):
        cols = []
        for c in t.get("columns", []):
            bits = f"{c['name']} {c.get('type', 'text')}"
            if c.get("primary_key"):
                bits += " PK"
            elif not c.get("nullable", True):
                bits += " NOT NULL"
            if c.get("references"):
                bits += f" FK->{c['references']}"
            cols.append(bits)
        lines.append(f"- {t['name']}: " + "; ".join(cols))
    return "\n".join(lines) or "(none)"


def format_plan(manager: dict) -> str:
    return "\n".join(
        f"{i}. {t['title']} ({t.get('role', '')}, {t.get('effort', 'M')})"
        for i, t in enumerate(manager.get("tasks", []), 1)
    ) or "(none)"


def format_tests(qa: dict) -> str:
    return "\n".join(
        f"- [{t.get('kind', 'functional')}] {t['title']}: {t.get('expected', '')}"
        for t in qa.get("test_cases", [])
    ) or "(none)"
